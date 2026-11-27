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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.db import IntegrityError, transaction
from django.db import models as django_models
from rest_framework import serializers

from waldur_core.core.yaml_utils import json_size, too_large_message
from waldur_mastermind.marketplace import (
    agent_configuration as site_agent_configuration,
)
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    normalize_offering_type,
)
from waldur_mastermind.marketplace.import_fields import (
    COMPONENT_IMPORT_FIELDS,
    ENDPOINT_IMPORT_FIELDS,
    OFFERING_IMPORT_FIELDS,
    PLAN_COMPONENT_IMPORT_FIELDS,
    PLAN_IMPORT_FIELDS,
)
from waldur_mastermind.marketplace.utils import UsernameGenerationPolicy

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
    "choices",
    "pattern",
}
PARAMETER_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PARAMETER_LABEL_MAX_LENGTH = 150
PARAMETER_CHOICES_MAX = 100
PARAMETER_PATTERN_MAX_LENGTH = 500
# Longer than any field a parameter fills; also bounds the work a pattern
# does on a value.
PARAMETER_VALUE_MAX_LENGTH = 1000

# Component fields the import reads as a mapping, checking its content.
COMPONENT_MAPPING_FIELDS = frozenset({"spending_limit_periods"})
# What a parameter may fill in the sections the import copies field by field:
# anything else the import drops without a word. create_offering sets the
# offering's name and type itself, and a template drops its paused_reason.
PARAMETER_LEAVES = {
    "offering": frozenset(OFFERING_IMPORT_FIELDS) - {"name", "type", "paused_reason"},
    "components": frozenset(
        {"type", *COMPONENT_IMPORT_FIELDS, *COMPONENT_MAPPING_FIELDS}
    ),
    "plans": frozenset({"name", *PLAN_IMPORT_FIELDS}),
    "endpoints": frozenset({"name", *ENDPOINT_IMPORT_FIELDS}),
}
PLAN_COMPONENT_LEAVES = frozenset({"component_type", *PLAN_COMPONENT_IMPORT_FIELDS})

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
# Of those, the ones that are a site's convention rather than its identity:
# a template saved from an offering offers the offering's value as default.
CONVENTION_SETTINGS = frozenset(
    {"default_account", "customer_prefix", "project_prefix", "allocation_prefix"}
)
USERNAME_POLICY = "username_generation_policy"
USERNAME_POLICY_LABEL = "How usernames are generated"
USERNAME_POLICY_CHOICES = [policy.value for policy in UsernameGenerationPolicy]
# Offering fields a template never takes over from an offering: it creates a
# draft, and why an offering was paused is that offering's own.
DROPPED_OFFERING_FIELDS = ("state", "paused_reason")

# Every edit stores a full snapshot of the document. The shipped presets are
# a few kilobytes.
DOCUMENT_MAX_BYTES = 1024 * 1024
DOCUMENT_NAME = "offering template document"

SECRET_MESSAGE = (
    "This is a secret, or is named like one. Secrets stay with the offering "
    "they belong to."
)

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

        choices = _check_choices(parameter, kind, prefix)
        pattern = _check_pattern(parameter, kind, prefix)
        if default is not None:
            problem = _value_problem(default, kind, choices, pattern)
            if problem:
                raise _error(f"{prefix}.default", problem)

        paths = parameter.get("paths")
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, str) and path for path in paths)
        ):
            raise _error(f"{prefix}.paths", "Expected a non-empty list of paths.")
        for path in paths:
            _check_target(document, path, f"{prefix}.paths", claimed, key)

        normalized = {
            "key": key,
            "label": label,
            "description": description,
            "type": kind,
            "required": required,
            "default": default,
            "paths": list(paths),
        }
        # Only when declared, so that a document written before they existed
        # keeps its hash, and so its version.
        if choices is not None:
            normalized["choices"] = choices
        if pattern is not None:
            normalized["pattern"] = pattern
        result.append(normalized)
    return result


def _is_of_type(value, kind: str) -> bool:
    # bool is an int to Python, but not an integer or a number here.
    return isinstance(value, PARAMETER_TYPES[kind]) and (
        kind == "boolean" or not isinstance(value, bool)
    )


def _check_choices(parameter: dict, kind: str, prefix: str) -> list | None:
    if "choices" not in parameter:
        return None
    choices = parameter["choices"]
    field = f"{prefix}.choices"
    if kind == "boolean":
        raise _error(field, "A boolean parameter has no choices.")
    if (
        not isinstance(choices, list)
        or not choices
        or len(choices) > PARAMETER_CHOICES_MAX
    ):
        raise _error(field, f"Expected a list of 1 to {PARAMETER_CHOICES_MAX} values.")
    if not all(_is_of_type(choice, kind) for choice in choices):
        raise _error(field, f"Every choice must be a {kind}.")
    if len(set(choices)) != len(choices):
        raise _error(field, "A choice is listed twice.")
    return list(choices)


def _check_pattern(parameter: dict, kind: str, prefix: str) -> str | None:
    if "pattern" not in parameter:
        return None
    pattern = parameter["pattern"]
    field = f"{prefix}.pattern"
    if kind != "string":
        raise _error(field, "Only a string parameter has a pattern.")
    if (
        not isinstance(pattern, str)
        or not pattern
        or len(pattern) > PARAMETER_PATTERN_MAX_LENGTH
    ):
        raise _error(
            field,
            f"Expected a regular expression of at most "
            f"{PARAMETER_PATTERN_MAX_LENGTH} characters.",
        )
    try:
        re.compile(pattern)
    except re.error as e:
        raise _error(field, f"Not a valid regular expression: {e}.")
    return pattern


def _value_problem(value, kind: str, choices, pattern) -> str | None:
    """Why a value does not fit a parameter's declaration, or None."""
    if not _is_of_type(value, kind):
        return f"Expected a {kind}."
    if isinstance(value, str) and len(value) > PARAMETER_VALUE_MAX_LENGTH:
        return f"At most {PARAMETER_VALUE_MAX_LENGTH} characters."
    if choices is not None and value not in choices:
        return "Expected one of: " + ", ".join(str(choice) for choice in choices) + "."
    if pattern is not None and not re.fullmatch(pattern, value):
        return f"Does not match the pattern {pattern}."
    return None


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
    leaves = _leaves(segments)
    if leaves is not None and segments[-1] not in leaves:
        raise _error(
            field,
            f"{path}: the offering import does not read {segments[-1]} there. "
            f"A parameter fills one of: {', '.join(sorted(leaves))}.",
        )
    if value_at(document, path) is not None:
        raise _error(
            field,
            f"{path} holds a value. Remove it: the parameter supplies it.",
        )


def _leaves(segments: list[str | int]) -> frozenset | None:
    """The fields a path may end in, or None where any key will do."""
    section = segments[0]
    if section not in PARAMETER_LEAVES:
        return None
    if section == "offering":
        return PARAMETER_LEAVES[section] if len(segments) == 2 else frozenset()
    if (
        section == "components"
        and len(segments) > 3
        and segments[2] in COMPONENT_MAPPING_FIELDS
    ):
        # The import checks what such a mapping holds.
        return None
    if len(segments) == 3 and isinstance(segments[1], int):
        return PARAMETER_LEAVES[section]
    if (
        section == "plans"
        and len(segments) == 5
        and isinstance(segments[1], int)
        and segments[2] == "components"
        and isinstance(segments[3], int)
    ):
        return PLAN_COMPONENT_LEAVES
    return frozenset()


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


def find_secrets(document: dict) -> list[str]:
    """Paths in the document whose key is a secret, or is named like one.

    Every section is searched by the agent configuration's rule, at any depth,
    except the parameters: their field names are fixed (one of them is
    ``key``), and check_parameters refuses a path that names a secret. The
    agent configuration's components are keyed by component type, and a type
    such as license_key names no secret, so only each component's fields are
    searched there, as the configuration's serializer does.
    """
    found: list[str] = []
    for section, value in document.items():
        if section == PARAMETERS_KEY:
            continue
        if section == "agent_configuration" and isinstance(value, dict):
            found.extend(
                site_agent_configuration.find_secret_settings(
                    {
                        key: item
                        for key, item in value.items()
                        if key != "backend_components"
                    },
                    section,
                )
            )
            components = value.get("backend_components")
            if isinstance(components, dict):
                for component_type, fields in components.items():
                    found.extend(
                        site_agent_configuration.find_secret_settings(
                            fields, f"{section}.backend_components.{component_type}"
                        )
                    )
            continue
        if section == "offering" and isinstance(value, dict):
            found.extend(
                site_agent_configuration.find_secret_settings(
                    {key: item for key, item in value.items() if key != "options"},
                    section,
                )
            )
            found.extend(_form_secrets(value.get("options"), "offering.options"))
            continue
        if section == "resource_options":
            found.extend(_form_secrets(value, section))
            continue
        found.extend(site_agent_configuration.find_secret_settings(value, section))
    return found


# The keys of an order form (offering.options, resource_options) that hold its
# fields by name, and the list that orders them.
FORM_FIELDS_KEY = "options"
FORM_ORDER_KEY = "order"


def _form_secrets(form, prefix: str) -> list[str]:
    """Secrets in an order form.

    A form's fields are keyed by the name of what the customer is asked for:
    a field called ssh_public_key names an input, not a secret held in the
    template. So field names are not judged; what each field holds is.
    """
    if not isinstance(form, dict):
        return site_agent_configuration.find_secret_settings(form, prefix)
    found = site_agent_configuration.find_secret_settings(
        {
            key: item
            for key, item in form.items()
            if key not in (FORM_FIELDS_KEY, FORM_ORDER_KEY)
        },
        prefix,
    )
    fields = form.get(FORM_FIELDS_KEY)
    if isinstance(fields, dict):
        for name, definition in fields.items():
            found.extend(
                site_agent_configuration.find_secret_settings(
                    definition, f"{prefix}.{FORM_FIELDS_KEY}.{name}"
                )
            )
    elif fields is not None:
        found.extend(
            site_agent_configuration.find_secret_settings(
                fields, f"{prefix}.{FORM_FIELDS_KEY}"
            )
        )
    return found


def _strip_form(form):
    """An order form with every secret removed, keeping its field names."""
    if not isinstance(form, dict):
        return form
    stripped = site_agent_configuration.without_secrets(
        {
            key: item
            for key, item in form.items()
            if key not in (FORM_FIELDS_KEY, FORM_ORDER_KEY)
        }
    )
    fields = form.get(FORM_FIELDS_KEY)
    if isinstance(fields, dict):
        stripped[FORM_FIELDS_KEY] = {
            name: site_agent_configuration.without_secrets(definition)
            if isinstance(definition, dict)
            else definition
            for name, definition in fields.items()
        }
    elif FORM_FIELDS_KEY in form:
        stripped[FORM_FIELDS_KEY] = fields
    if FORM_ORDER_KEY in form:
        stripped[FORM_ORDER_KEY] = form[FORM_ORDER_KEY]
    return stripped


def check_document(document, offering_type: str) -> dict:
    """Refuse a document a template may not hold; return it normalised.

    It may be at most DOCUMENT_MAX_BYTES as JSON, its schema version must be
    supported, it may hold only known sections and none of the excluded ones,
    its offering block must be of the template's type, its agent configuration
    must be valid, its parameters well formed, and no section may hold a key
    named like a secret.
    """
    if not isinstance(document, dict):
        raise serializers.ValidationError("Expected a mapping.")
    if json_size(document) > DOCUMENT_MAX_BYTES:
        raise serializers.ValidationError(
            too_large_message(DOCUMENT_NAME, DOCUMENT_MAX_BYTES)
        )
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

    normalized = dict(document)
    parameters = check_parameters(document.get(PARAMETERS_KEY, []), document)
    normalized[PARAMETERS_KEY] = parameters
    if "agent_configuration" in document:
        if not isinstance(document["agent_configuration"], dict):
            raise _error("agent_configuration", "Expected a mapping.")
        _check_agent_configuration(
            document["agent_configuration"], parameter_targets(parameters)
        )
    # After the agent configuration's own check, which explains a secret in
    # its backend settings in its own words.
    secrets = find_secrets(document)
    if secrets:
        raise _error(secrets[0], SECRET_MESSAGE)
    return normalized


# Creating an offering -----------------------------------------------------

REQUIRED_PARAMETER_MESSAGE = "This parameter is required."


def resolve_parameters(parameters: list[dict], values) -> dict:
    """The value of every parameter: the one given, or its default.

    Refuses, per parameter, a value that is missing while required, of the
    wrong type, not among the choices or not matching the pattern, and a value
    for a parameter the template does not declare.
    """
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise serializers.ValidationError({PARAMETERS_KEY: ["Expected a mapping."]})
    errors: dict[str, list[str]] = {}
    declared = {parameter["key"] for parameter in parameters}
    for key in sorted(set(values) - declared):
        errors[key] = ["The template declares no such parameter."]
    resolved = {}
    for parameter in parameters:
        key = parameter["key"]
        value = values.get(key)
        # An empty string is no value: the form sends one for an untouched field.
        if value is None or value == "":
            value = parameter.get("default")
        if value is None:
            if parameter["required"]:
                errors[key] = [REQUIRED_PARAMETER_MESSAGE]
            resolved[key] = None
            continue
        problem = _value_problem(
            value,
            parameter["type"],
            parameter.get("choices"),
            parameter.get("pattern"),
        )
        if problem:
            errors[key] = [problem]
        resolved[key] = value
    if errors:
        raise serializers.ValidationError({PARAMETERS_KEY: errors})
    return resolved


def fill_document(document: dict, values: dict) -> dict:
    """The document with every parameter's paths filled with its value, and
    without the sections an offering does not take: what import_offering
    reads. An optional parameter left without a value leaves its paths out.
    """
    filled = copy.deepcopy(document)
    parameters = filled.pop(PARAMETERS_KEY, None) or []
    filled.pop("schema_version", None)
    for parameter in parameters:
        value = values.get(parameter["key"])
        for path in parameter["paths"]:
            segments = split_path(path)
            parent = _parent(filled, segments)
            if parent is None:
                continue
            leaf = segments[-1]
            if value is not None:
                parent[leaf] = copy.deepcopy(value)
            elif isinstance(parent, dict):
                parent.pop(leaf, None)
    return filled


def with_agent_roles(agent_configuration: dict, roles: dict) -> dict:
    """The agent configuration with the role backends chosen for the new
    offering in place of the template's. A role left out keeps its backend, a
    blank one names none; custom backends are added to the template's."""
    configuration = copy.deepcopy(agent_configuration)
    for field in site_agent_configuration.ROLE_FIELDS:
        if field not in roles:
            continue
        if roles[field]:
            configuration[field] = roles[field]
        else:
            configuration.pop(field, None)
    custom = roles.get("custom_backends")
    if custom:
        configuration["custom_backends"] = list(
            dict.fromkeys([*(configuration.get("custom_backends") or []), *custom])
        )
    return configuration


def with_backend_settings(agent_configuration: dict, settings: dict) -> dict:
    """The agent configuration with the backend settings given for the new
    offering over the template's: a nested mapping is merged key by key, any
    other value replaces the template's."""
    configuration = copy.deepcopy(agent_configuration)
    configuration["backend_settings"] = _merged(
        configuration.get("backend_settings") or {}, settings
    )
    return configuration


def _merged(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merged(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


# Drift --------------------------------------------------------------------

# How the items of a list section are told apart, so that a reordered list is
# no drift and a changed item is reported under its own name.
ITEM_KEYS = {
    "components": "type",
    "plans": "name",
    "endpoints": "name",
    "terms_of_service": "version",
}
PLAN_COMPONENT_KEY = "component_type"
# The import's numeric model fields, which an export may write as strings.
NUMERIC_FIELDS = frozenset(
    field.name
    for model in (
        models.Offering,
        models.OfferingComponent,
        models.Plan,
        models.PlanComponent,
    )
    for field in model._meta.get_fields()
    if isinstance(
        field,
        django_models.DecimalField
        | django_models.IntegerField
        | django_models.FloatField,
    )
)
# The name and category are given when the offering is created, not by the
# template.
UNCOMPARED_PATHS = {"offering.name", "offering.category_name"}


def _number(value) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            number = Decimal(value)
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None


def _same(expected, actual, field) -> bool:
    """Numbers compared by value: an export writes 60000 as 60000.0, and a
    price as a decimal string. Two strings are text unless the field is a
    number, so a prefix 01 is not 1."""
    if expected == actual:
        return True
    if (
        isinstance(expected, str)
        and isinstance(actual, str)
        and field not in NUMERIC_FIELDS
    ):
        return False
    expected_number, actual_number = _number(expected), _number(actual)
    return expected_number is not None and expected_number == actual_number


def _item_key(path: str) -> str | None:
    if path in ITEM_KEYS:
        return ITEM_KEYS[path]
    if path.startswith("plans[") and path.endswith("].components"):
        return PLAN_COMPONENT_KEY
    return None


def _diff(expected, actual, path: str, differences: list[dict]) -> None:
    if path in UNCOMPARED_PATHS:
        return
    if isinstance(expected, dict):
        # A list item the offering does not have is one difference, not one
        # per field.
        missing_item = actual is None and path.endswith("]")
        if missing_item or (actual is not None and not isinstance(actual, dict)):
            differences.append({"path": path, "template": expected, "offering": actual})
            return
        actual_map = actual or {}
        # Only what the template says: an offering holds more keys than a
        # template, such as defaults its serializers add.
        for key, value in expected.items():
            _diff(
                value,
                actual_map.get(key),
                f"{path}.{key}" if path else key,
                differences,
            )
        return
    item_key = _item_key(path)
    if item_key and isinstance(expected, list):
        if not isinstance(actual, list):
            differences.append({"path": path, "template": expected, "offering": actual})
            return
        actual_items = {}
        for item in actual:
            if not isinstance(item, dict):
                continue
            name = item.get(item_key)
            # An archived plan can share its name with an active one, which
            # is what the template describes.
            if name not in actual_items or actual_items[name].get("archived"):
                actual_items[name] = item
        expected_names = set()
        for item in expected:
            if not isinstance(item, dict):
                continue
            name = item.get(item_key)
            expected_names.add(name)
            _diff(item, actual_items.get(name), f"{path}[{name}]", differences)
        for name, item in actual_items.items():
            if name not in expected_names:
                differences.append(
                    {"path": f"{path}[{name}]", "template": None, "offering": item}
                )
        return
    if expected is None:
        # The template leaves it open: an optional parameter without a value.
        return
    if not _same(expected, actual, path.rsplit(".", 1)[-1]):
        differences.append({"path": path, "template": expected, "offering": actual})


def document_diff(expected: dict, actual: dict) -> list[dict]:
    """Where an offering differs from what its template says it should be.

    ``expected`` is a filled-in template document, ``actual`` the offering's
    export. Each difference is ``{path, template, offering}``, where list items
    are named by their key, as in ``components[cpu].unit_factor``. Only what
    the template sets is compared, and a section the export left out (such as
    an agent configuration the caller may not see) is skipped.
    """
    differences: list[dict] = []
    for section, value in expected.items():
        if section not in actual:
            continue
        _diff(value, actual[section], section, differences)
    return differences


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

    def declare(key, label, path, value=None, default=None):
        parameter = parameters.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "description": "",
                "type": _parameter_type(value),
                "required": True,
                "default": default,
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

    # Secrets are dropped from every section by the rule check_document
    # refuses them by, so an offering whose options hold one still saves.
    configuration = document.get("agent_configuration")
    if isinstance(configuration, dict):
        # Components are keyed by type, which names no secret; their fields
        # may carry settings named like one.
        components = configuration.get("backend_components") or {}
        configuration = site_agent_configuration.without_secrets(
            {
                key: value
                for key, value in configuration.items()
                if key != "backend_components"
            }
        )
        configuration["backend_settings"] = configuration.get("backend_settings") or {}
        configuration["backend_components"] = {
            component_type: site_agent_configuration.without_secrets(fields)
            for component_type, fields in components.items()
        }
        document["agent_configuration"] = configuration
    else:
        document.pop("agent_configuration", None)

    for section in SECTIONS:
        value = document.get(section)
        if section in (PARAMETERS_KEY, "agent_configuration"):
            continue
        if section == "offering" and isinstance(value, dict):
            options = value.get(FORM_FIELDS_KEY)
            document[section] = site_agent_configuration.without_secrets(
                {key: item for key, item in value.items() if key != FORM_FIELDS_KEY}
            )
            if FORM_FIELDS_KEY in value:
                document[section][FORM_FIELDS_KEY] = _strip_form(options)
        elif section == "resource_options":
            document[section] = _strip_form(value)
        elif isinstance(value, dict):
            document[section] = site_agent_configuration.without_secrets(value)
        elif isinstance(value, list):
            document[section] = [
                site_agent_configuration.without_secrets(item)
                if isinstance(item, dict)
                else item
                for item in value
            ]
    offering = document["offering"]

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
            declare(
                key,
                label,
                f"{prefix}.{name}",
                value,
                default=value if name in CONVENTION_SETTINGS else None,
            )
            if key == CLUSTER_PARAMETER:
                parameters[key]["type"] = _parameter_type(value)

    # Asked for as the shipped presets ask for it, the offering's as default.
    plugin_options = document.get("plugin_options")
    if (
        isinstance(plugin_options, dict)
        and plugin_options.get(USERNAME_POLICY) in USERNAME_POLICY_CHOICES
    ):
        value = plugin_options.pop(USERNAME_POLICY)
        declare(
            USERNAME_POLICY,
            USERNAME_POLICY_LABEL,
            f"plugin_options.{USERNAME_POLICY}",
            value,
            default=value,
        )
        parameters[USERNAME_POLICY]["choices"] = USERNAME_POLICY_CHOICES

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
