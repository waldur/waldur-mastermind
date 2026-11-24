"""Offering templates: complete offering setups a new offering starts from.

A template's document is in the format export_offering writes -- the offering
block, options, plugin options, components, plans with prices, endpoints and
terms of service -- extended with two sections:

- ``agent_configuration``: the site agent's side of the offering, without
  secrets, as the offering stores it;
- ``parameters``: the values a provider supplies when an offering is created
  from the template. Each parameter names the document paths it fills, such
  as ``offering.backend_id`` or ``agent_configuration.backend_settings.
  default_account``; the document leaves those paths empty.

A document never carries a customer, secrets, resources or people: those
sections are refused on write. Staff author templates, save one from an
offering (``template_document``), or load one of the presets shipped here
(``load_presets``) -- a copy that keeps no link to the preset.
"""

import copy
import json
import re
from pathlib import Path
from typing import Any

from django.db import IntegrityError, transaction
from rest_framework import serializers

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    normalize_offering_type,
)
from waldur_mastermind.marketplace_site_agent import (
    agent_configuration as site_agent_configuration,
)

PRESETS_DIRECTORY = Path(__file__).resolve().parent / "presets"

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

# Only site-agent offerings for now; other types are later increments.
OFFERING_TYPES = (SITE_AGENT_OFFERING,)

PARAMETERS_KEY = "parameters"

# Sections a document may hold. Each is optional.
SECTIONS = (
    "schema_version",
    "offering",
    "resource_options",
    "plugin_options",
    "agent_configuration",
    "components",
    "plans",
    "endpoints",
    "terms_of_service",
    PARAMETERS_KEY,
)
MAPPING_SECTIONS = ("offering", "resource_options", "plugin_options")
LIST_SECTIONS = ("components", "plans", "endpoints", "terms_of_service")

# Sections an export may hold that a template does not, with the reason.
EXCLUDED_SECTIONS = {
    "secret_options": "Secrets stay with the offering they belong to.",
    "organization_groups": "Organization groups are this portal's own.",
    "screenshots": "Screenshots are not part of an offering setup.",
    "files": "Files are not part of an offering setup.",
    "customer": "A template belongs to no customer.",
    "resources": "A template carries no resources.",
    "users": "A template carries no people.",
    "offering_users": "A template carries no people.",
    "permissions": "A template carries no people.",
}

# Sections a parameter may fill.
PARAMETER_SECTIONS = (
    "offering",
    "resource_options",
    "plugin_options",
    "agent_configuration",
    "components",
    "plans",
    "endpoints",
)
PARAMETER_TYPES = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}
PARAMETER_FIELDS = {
    "key",
    "label",
    "description",
    "type",
    "required",
    "default",
    "paths",
}
PARAMETER_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PARAMETER_LABEL_MAX_LENGTH = 150

# What save_as_template turns into parameters: the values that tie an offering
# to one site. Backend settings are looked up in the agent configuration and,
# for offerings configured before agent configurations existed, in
# plugin_options.
CLUSTER_PARAMETER = "cluster_name"
SITE_SPECIFIC_SETTINGS = {
    "cluster_name": "SLURM cluster name",
    "hostname": "Cluster hostname",
    "default_account": "Default parent account",
    "customer_prefix": "Customer account prefix",
    "project_prefix": "Project account prefix",
    "allocation_prefix": "Allocation account prefix",
    "cscs_dwdi_api_url": "DWDI API URL",
    "cscs_dwdi_oidc_token_url": "DWDI OIDC token URL",
    "cscs_dwdi_client_id": "DWDI OIDC client ID",
    "cscs_dwdi_cluster": "DWDI cluster",
    "api_url": "Service API URL",
}
# Offering fields a template never takes over from an offering: it creates a
# draft, and why an offering was paused is that offering's own.
DROPPED_OFFERING_FIELDS = ("state", "paused_reason")

REQUIRED_SETTING_MESSAGE = "This setting is required."
NAME_TAKEN = "An offering template with this name already exists."


def _error(path: str, message: str):
    return serializers.ValidationError({path: [message]})


# Paths --------------------------------------------------------------------


def split_path(path: str) -> list[str | int]:
    """The segments of a dotted path; digits address list items."""
    return [int(part) if part.isdigit() else part for part in path.split(".")]


def _parent(document: dict, segments: list[str | int]):
    """The container that holds the path's last segment, or None."""
    node: Any = document
    for segment in segments[:-1]:
        if isinstance(node, dict) and isinstance(segment, str) and segment in node:
            node = node[segment]
        elif (
            isinstance(node, list) and isinstance(segment, int) and segment < len(node)
        ):
            node = node[segment]
        else:
            return None
    leaf = segments[-1]
    if isinstance(node, dict) and isinstance(leaf, str):
        return node
    if isinstance(node, list) and isinstance(leaf, int) and leaf < len(node):
        return node
    return None


def value_at(document: dict, path: str):
    segments = split_path(path)
    parent = _parent(document, segments)
    if parent is None:
        return None
    leaf = segments[-1]
    if isinstance(parent, dict):
        return parent.get(leaf)
    return parent[leaf]


def parameter_targets(parameters: list[dict]) -> set[str]:
    return {path for parameter in parameters for path in parameter["paths"]}


# Checks -------------------------------------------------------------------


def check_parameters(parameters, document: dict) -> list[dict]:
    """Refuse malformed parameters; return them with their defaults filled in.

    Every path must start at a section a parameter may fill, go through
    containers the document has, name no secret, be claimed by one parameter
    only and hold no value: the parameter supplies it.
    """
    if not isinstance(parameters, list):
        raise _error(PARAMETERS_KEY, "Expected a list.")
    result = []
    keys: set[str] = set()
    claimed: dict[str, str] = {}
    for index, parameter in enumerate(parameters):
        prefix = f"{PARAMETERS_KEY}.{index}"
        if not isinstance(parameter, dict):
            raise _error(prefix, "Expected a mapping.")
        unknown = sorted(set(parameter) - PARAMETER_FIELDS)
        if unknown:
            raise _error(prefix, f"Unknown fields: {', '.join(unknown)}.")

        key = parameter.get("key")
        if not isinstance(key, str) or not PARAMETER_KEY_PATTERN.match(key):
            raise _error(
                f"{prefix}.key",
                "Use lowercase letters, digits and underscores, starting with "
                "a letter, at most 64 characters.",
            )
        if key in keys:
            raise _error(f"{prefix}.key", f"Parameter {key} is declared twice.")
        keys.add(key)

        label = parameter.get("label")
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > PARAMETER_LABEL_MAX_LENGTH
        ):
            raise _error(
                f"{prefix}.label",
                f"A label of at most {PARAMETER_LABEL_MAX_LENGTH} characters is "
                "required.",
            )
        description = parameter.get("description", "")
        if not isinstance(description, str):
            raise _error(f"{prefix}.description", "Expected a string.")

        kind = parameter.get("type", "string")
        if kind not in PARAMETER_TYPES:
            raise _error(
                f"{prefix}.type", f"Expected one of: {', '.join(PARAMETER_TYPES)}."
            )
        required = parameter.get("required", True)
        if not isinstance(required, bool):
            raise _error(f"{prefix}.required", "Expected a boolean.")
        default = parameter.get("default")
        if default is not None and (
            not isinstance(default, PARAMETER_TYPES[kind])
            or (kind != "boolean" and isinstance(default, bool))
        ):
            raise _error(f"{prefix}.default", f"Expected a {kind}.")

        paths = parameter.get("paths")
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, str) and path for path in paths)
        ):
            raise _error(f"{prefix}.paths", "Expected a non-empty list of paths.")
        for path in paths:
            _check_target(document, path, f"{prefix}.paths", claimed, key)

        result.append(
            {
                "key": key,
                "label": label,
                "description": description,
                "type": kind,
                "required": required,
                "default": default,
                "paths": list(paths),
            }
        )
    return result


def _names_a_secret(segment) -> bool:
    """Whether a path segment names a secret, by the rule the agent
    configuration uses: declared secret, or named like a credential and
    declared plain by no plugin (such as a token URL)."""
    if not isinstance(segment, str):
        return False
    return segment in site_agent_configuration.secret_setting_names() or (
        site_agent_configuration.looks_like_credential(segment)
        and segment not in site_agent_configuration.declared_plain_setting_names()
    )


def _check_target(document, path, field, claimed, key):
    segments = split_path(path)
    if segments[0] not in PARAMETER_SECTIONS:
        raise _error(
            field,
            f"{path}: a parameter fills one of {', '.join(PARAMETER_SECTIONS)}.",
        )
    if path in claimed:
        raise _error(field, f"{path} is filled by parameter {claimed[path]} already.")
    claimed[path] = key
    if any(_names_a_secret(segment) for segment in segments[1:]):
        raise _error(
            field,
            f"{path} is a secret. Secrets stay in the agent configuration file "
            "on the agent host; a template does not ask for them.",
        )
    if len(segments) < 2 or _parent(document, segments) is None:
        raise _error(field, f"{path} does not lead into the document.")
    if value_at(document, path) is not None:
        raise _error(
            field,
            f"{path} holds a value. Remove it: the parameter supplies it.",
        )


def _check_agent_configuration(configuration, targets: set[str]) -> dict:
    """Validate the agent configuration as an offering's would be.

    A setting a parameter fills is missing from the document by design, so
    the catalogue's "required" does not apply to it.
    """
    serializer = site_agent_configuration.AgentConfigurationSerializer(
        data=configuration
    )
    if serializer.is_valid():
        return serializer.validated_data
    errors = copy.deepcopy(serializer.errors)
    settings_errors = errors.get("backend_settings")
    if isinstance(settings_errors, dict):
        for name in list(settings_errors):
            if f"agent_configuration.backend_settings.{name}" not in targets:
                continue
            remaining = [
                message
                for message in settings_errors[name]
                if not str(message).endswith(REQUIRED_SETTING_MESSAGE)
            ]
            if remaining:
                settings_errors[name] = remaining
            else:
                del settings_errors[name]
        if not settings_errors:
            del errors["backend_settings"]
    if errors:
        raise serializers.ValidationError({"agent_configuration": errors})
    return configuration


def check_document(document, offering_type: str) -> dict:
    """Refuse a document a template may not hold; return it normalised.

    Its schema version must be supported, it may hold only known sections and
    none of the excluded ones, its offering block must be of the template's
    type, its agent configuration must be valid and free of secrets, and its
    parameters well formed.
    """
    if not isinstance(document, dict):
        raise serializers.ValidationError("Expected a mapping.")
    if offering_type not in OFFERING_TYPES:
        raise _error(
            "offering_type",
            f"Templates support these offering types: {', '.join(OFFERING_TYPES)}.",
        )
    version = document.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise _error("schema_version", f"Unsupported schema version: {version!r}.")
    for key in document:
        if key in EXCLUDED_SECTIONS:
            raise _error(
                key,
                f"An offering template may not hold this section. "
                f"{EXCLUDED_SECTIONS[key]}",
            )
        if key not in SECTIONS:
            raise _error(key, "Unknown section.")
    for key in MAPPING_SECTIONS:
        if key in document and not isinstance(document[key], dict):
            raise _error(key, "Expected a mapping.")
    for key in LIST_SECTIONS:
        if key in document and not (
            isinstance(document[key], list)
            and all(isinstance(item, dict) for item in document[key])
        ):
            raise _error(key, "Expected a list of mappings.")

    offering = document.get("offering") or {}
    if offering.get("type") and (
        normalize_offering_type(offering["type"]) != offering_type
    ):
        raise _error(
            "offering.type",
            f"The template is for {offering_type} offerings.",
        )

    secrets = site_agent_configuration.find_secret_settings(
        document.get("plugin_options") or {}, "plugin_options"
    )
    if secrets:
        raise _error(
            secrets[0],
            "This is a secret, or is named like one. Secrets stay with the "
            "offering they belong to.",
        )

    normalized = dict(document)
    parameters = check_parameters(document.get(PARAMETERS_KEY, []), document)
    normalized[PARAMETERS_KEY] = parameters
    if "agent_configuration" in document:
        if not isinstance(document["agent_configuration"], dict):
            raise _error("agent_configuration", "Expected a mapping.")
        _check_agent_configuration(
            document["agent_configuration"], parameter_targets(parameters)
        )
    return normalized


# What non-staff see -------------------------------------------------------


def public_document(document: dict) -> dict:
    """The part of a document an offering creator sees before using it:
    what the offering is, what it sells and what they will be asked for."""
    offering = document.get("offering") or {}
    return {
        "schema_version": document.get("schema_version"),
        "offering": {key: offering.get(key) for key in ("name", "description", "type")},
        "components": [
            {
                key: component.get(key)
                for key in (
                    "type",
                    "name",
                    "description",
                    "measured_unit",
                    "billing_type",
                )
            }
            for component in document.get("components") or []
        ],
        "plans": [
            {key: plan.get(key) for key in ("name", "description")}
            for plan in document.get("plans") or []
        ],
        PARAMETERS_KEY: document.get(PARAMETERS_KEY) or [],
    }


# Saving an offering as a template ----------------------------------------


def _parameter_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def template_document(export_data: dict) -> dict:
    """A template document from an offering's export.

    Secrets, and sections a template does not hold, are dropped. The values
    that tie the offering to its site -- its backend ID (the SLURM cluster
    name), access URL, endpoint URLs and the site-specific backend settings --
    are removed and declared as required parameters instead.
    """
    document = {
        key: copy.deepcopy(value)
        for key, value in export_data.items()
        if key in SECTIONS and key != PARAMETERS_KEY
    }
    document["schema_version"] = SCHEMA_VERSION
    parameters: dict[str, dict] = {}

    def declare(key, label, path, value=None):
        parameter = parameters.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "description": "",
                "type": _parameter_type(value),
                "required": True,
                "default": None,
                "paths": [],
            },
        )
        parameter["paths"].append(path)

    offering = document.setdefault("offering", {})
    for field in DROPPED_OFFERING_FIELDS:
        offering.pop(field, None)
    offering.pop("backend_id", None)
    declare(
        CLUSTER_PARAMETER,
        "Cluster name (the offering's backend ID)",
        "offering.backend_id",
    )
    if offering.get("access_url"):
        offering["access_url"] = None
        declare("access_url", "Access URL", "offering.access_url")

    for index, endpoint in enumerate(document.get("endpoints") or []):
        if endpoint.get("url"):
            endpoint["url"] = None
            name = endpoint.get("name") or str(index + 1)
            declare(
                f"endpoint_{index + 1}_url",
                f"URL of the endpoint {name}"[:PARAMETER_LABEL_MAX_LENGTH],
                f"endpoints.{index}.url",
            )

    configuration = document.get("agent_configuration")
    if isinstance(configuration, dict):
        configuration["backend_settings"] = site_agent_configuration.without_secrets(
            configuration.get("backend_settings") or {}
        )
        # Components may carry settings named like a secret; drop them too.
        components = configuration.get("backend_components") or {}
        configuration["backend_components"] = {
            component_type: site_agent_configuration.without_secrets(fields)
            for component_type, fields in components.items()
        }
    else:
        document.pop("agent_configuration", None)

    if isinstance(document.get("plugin_options"), dict):
        document["plugin_options"] = site_agent_configuration.without_secrets(
            document["plugin_options"]
        )

    sources = (
        (
            "agent_configuration.backend_settings",
            (configuration or {}).get("backend_settings"),
        ),
        ("plugin_options", document.get("plugin_options")),
    )
    for prefix, settings in sources:
        if not isinstance(settings, dict):
            continue
        for name, label in SITE_SPECIFIC_SETTINGS.items():
            value = settings.get(name)
            if value is None or isinstance(value, dict | list):
                continue
            del settings[name]
            key = CLUSTER_PARAMETER if name == "cluster_name" else name
            declare(key, label, f"{prefix}.{name}", value)
            if key == CLUSTER_PARAMETER:
                parameters[key]["type"] = _parameter_type(value)

    document[PARAMETERS_KEY] = list(parameters.values())
    return document


# Saving -------------------------------------------------------------------


def save_unique(save):
    """Run ``save`` in a savepoint. Validation checks that a name is free, but
    a concurrent save can take it first; the database then refuses the row,
    and that is reported as the field error validation would have given."""
    try:
        with transaction.atomic():
            return save()
    except IntegrityError as e:
        if "name" not in str(e):
            raise
        raise serializers.ValidationError({"name": [NAME_TAKEN]})


# Presets ------------------------------------------------------------------


def names() -> list[str]:
    """The keys of the presets shipped with the marketplace."""
    return sorted(path.stem for path in PRESETS_DIRECTORY.glob("*.json"))


def load(key: str) -> dict:
    """A preset: its name, description, offering type and document."""
    path = PRESETS_DIRECTORY / f"{key}.json"
    if key not in names():
        raise KeyError(f"No offering template preset named '{key}'.")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def presets() -> list[dict]:
    """Every preset, with whether a template already holds its name."""
    taken = set(models.OfferingTemplate.objects.values_list("name", flat=True))
    result = []
    for key in names():
        preset = load(key)
        result.append(
            {
                "key": key,
                "name": preset["name"],
                "description": preset.get("description", ""),
                "offering_type": preset["offering_type"],
                "parameters": check_parameters(
                    preset["document"].get(PARAMETERS_KEY, []), preset["document"]
                ),
                "name_taken": preset["name"] in taken,
            }
        )
    return result


def load_presets(keys: list[str], publish: bool, user) -> list[str]:
    """Create a template from each preset; all of them or, on any error, none.

    A preset whose name a template already holds is refused rather than
    overwritten: once loaded, a template lives its own life.
    """
    created = []
    with transaction.atomic():
        for key in keys:
            preset = load(key)
            if models.OfferingTemplate.objects.filter(name=preset["name"]).exists():
                raise _error(
                    "presets",
                    f"An offering template named '{preset['name']}' already "
                    "exists. Rename or delete it first to load this preset again.",
                )
            document = check_document(preset["document"], preset["offering_type"])
            template = models.OfferingTemplate(
                name=preset["name"],
                description=preset.get("description", ""),
                offering_type=preset["offering_type"],
                document=document,
                schema_version=document["schema_version"],
                is_published=publish,
                created_by=user,
            )
            save_unique(template.save)
            created.append(template.name)
    return created
