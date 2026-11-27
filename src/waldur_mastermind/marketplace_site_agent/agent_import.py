"""Read an existing site agent configuration file into an offering's agent configuration.

The inverse of config_builder: one offering block of the agent's YAML file
becomes the agent configuration Waldur holds, minus what Waldur does not hold.
That is the secrets (kept on the agent host), how the agent reaches Waldur, and
the component fields the offering's own components hold, whose differences
from the offering are reported rather than imported.
"""

from __future__ import annotations

import copy
import datetime
import math
import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_mastermind.marketplace import (
    agent_configuration as site_agent_configuration,
)
from waldur_mastermind.marketplace.agent_import_serializers import (
    COMPARED_COMPONENT_FIELDS,
)
from waldur_mastermind.marketplace_site_agent import config_builder

# The most values the file may expand to: YAML aliases let a small file stand
# for a very large structure.
MAX_VALUES = 100_000
# How deep the file may nest; agent configurations need a handful of levels.
MAX_DEPTH = 50

ROLE_KEYS = tuple(site_agent_configuration.ROLE_FIELDS)
EVENT_SETTING_KEYS = tuple(site_agent_configuration.EventSettingsSerializer().fields)
AGENT_SETTING_KEYS = tuple(site_agent_configuration.AgentSettingsSerializer().fields)


REACHES_WALDUR = _(
    "How the agent reaches Waldur; it stays in the file on the agent host."
)
NOT_HELD = _(
    "Waldur does not hold this setting; it stays in the file on the agent host."
)
IGNORED_KEYS = {
    "name": _("Waldur uses the offering's own name."),
    "configuration_source": REACHES_WALDUR,
    "waldur_api_url": REACHES_WALDUR,
    "waldur_offering_uuid": REACHES_WALDUR,
    "oidc_token_url": REACHES_WALDUR,
    "oidc_client_id": REACHES_WALDUR,
    "verify_ssl": REACHES_WALDUR,
}
BACKEND_TYPE_REASON = _("Waldur writes it from the role backends.")
OFFERING_COMPONENT_REASON = _("Held by the offering's component.")
NO_COMPONENT_REASON = _("The offering has no component of this type.")
NOT_A_MAPPING_REASON = _("Not a mapping.")
WHOLE_AGENT_REASON = _(
    "Applies to the whole agent process, not to one offering; it stays in the "
    "file on the agent host."
)
NOT_A_POLICY_SETTING_REASON = _(
    "Not a setting of the SLURM periodic usage policy Waldur holds."
)

# The fields of the offering's SLURM periodic usage policy that policy_settings
# holds, as config_builder.build_policy_settings writes them.
POLICY_FIELDS = (
    "limit_type",
    "tres_billing_enabled",
    "tres_billing_weights",
    "carryover_factor",
    "grace_ratio",
    "carryover_enabled",
    "raw_usage_reset",
    "qos_strategy",
)

# A value the agent host fills in, such as ${DWDI_CLIENT_SECRET}.
PLACEHOLDER = re.compile(r"\$\{[^{}]+\}")


def is_secret_name(key: Any) -> bool:
    key = str(key)
    return key in site_agent_configuration.secret_setting_names() or (
        site_agent_configuration.looks_like_credential(key)
        and key not in site_agent_configuration.declared_plain_setting_names()
    )


def is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(PLACEHOLDER.fullmatch(value.strip()))


def placeholder_name(value: str) -> str:
    return value.strip()[2:-1]


def normalise_uuid(value: Any) -> str:
    return str(value or "").replace("-", "").strip().lower()


@dataclass
class ImportedConfiguration:
    offerings: list[dict[str, Any]]
    selected_index: int
    agent_configuration: dict[str, Any]
    dropped_secrets: list[str] = field(default_factory=list)
    unresolved: list[dict[str, str]] = field(default_factory=list)
    ignored: list[dict[str, str]] = field(default_factory=list)
    component_differences: list[dict[str, Any]] = field(default_factory=list)
    # The block's policy_settings, as far as they are policy fields; None when
    # it has none.
    policy_settings: dict[str, Any] | None = None

    def ignore(self, path: str, reason) -> None:
        self.ignored.append({"path": path, "reason": str(reason)})

    def leave_unresolved(self, path: str, value: str) -> None:
        self.unresolved.append({"path": path, "placeholder": placeholder_name(value)})


class SelectionError(serializers.ValidationError):
    """No single offering of the file to import; answered with the file's offerings.

    They go next to the error, so that a client can offer the choice. They are
    added after DRF has turned the message into error details, which would
    have turned their numbers and flags into text as well.
    """

    def __init__(self, message, offerings: list[dict]):
        super().__init__({"offering_index": [message]})
        self.offerings = offerings
        self.detail = {**self.detail, "offerings": offerings}


def _configuration_error(message) -> serializers.ValidationError:
    return serializers.ValidationError({"configuration": [message]})


def _plain(data: Any, budget: list[int], ancestors: tuple[int, ...] = ()) -> Any:
    """The document as JSON data, refusing one that expands to too many values,
    nests too deeply or holds a value inside itself.

    YAML reads dates and timestamps as such, which JSON cannot hold: they are
    kept as the text the agent would have read. Keys become strings, as JSON
    would make them. An alias may name the same value twice; one that names a
    value it is inside (``a: &a [*a]``) would never end.
    """
    budget[0] -= 1
    if budget[0] < 0:
        raise _configuration_error(_("The file holds too many values."))
    if isinstance(data, dict | list):
        if id(data) in ancestors:
            raise _configuration_error(_("A value in the file refers to itself."))
        if len(ancestors) >= MAX_DEPTH:
            raise _configuration_error(_("The file nests too deeply."))
        inside = (*ancestors, id(data))
        if isinstance(data, dict):
            return {
                str(key): _plain(value, budget, inside) for key, value in data.items()
            }
        return [_plain(item, budget, inside) for item in data]
    if isinstance(data, datetime.date | datetime.datetime):
        return data.isoformat()
    if isinstance(data, float) and not math.isfinite(data):
        # .nan and .inf: JSON has no such numbers.
        raise _configuration_error(
            _("The file holds a number that is not finite (.nan or .inf).")
        )
    if isinstance(data, str | int | float | bool) or data is None:
        return data
    raise _configuration_error(
        _("The file holds a value that is not text, a number or a boolean.")
    )


def parse_configuration(text: str) -> tuple[dict[str, Any], list[Any]]:
    """The file as a mapping, and its offerings list.

    Error messages name where the file fails, never what it holds: it holds
    secrets.
    """
    try:
        data = yaml.safe_load(text)
    except RecursionError:
        # The parser recurses once per level of nesting.
        raise _configuration_error(_("The file nests too deeply."))
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        if mark is not None:
            raise _configuration_error(
                _("Not valid YAML (line %(line)s, column %(column)s).")
                % {"line": mark.line + 1, "column": mark.column + 1}
            )
        raise _configuration_error(_("Not valid YAML."))
    if not isinstance(data, dict):
        raise _configuration_error(
            _("Expected a site agent configuration: a mapping with an offerings list.")
        )
    data = _plain(data, [MAX_VALUES])
    offerings = data.get("offerings")
    if not isinstance(offerings, list) or not offerings:
        raise _configuration_error(_("The file has no offerings list."))
    for index, block in enumerate(offerings):
        if not isinstance(block, dict):
            raise _configuration_error(
                _("Entry %(index)s of offerings is not a mapping.") % {"index": index}
            )
    return data, offerings


def describe_blocks(offerings: list[dict[str, Any]], offering_uuid) -> list[dict]:
    described = []
    for index, block in enumerate(offerings):
        block_uuid = block.get("waldur_offering_uuid")
        name = block.get("name")
        described.append(
            {
                "index": index,
                "name": None if name is None else str(name),
                "waldur_offering_uuid": None if block_uuid is None else str(block_uuid),
                "matches_offering": block_uuid is not None
                and normalise_uuid(block_uuid) == offering_uuid.hex,
            }
        )
    return described


def _blocks_text(described: list[dict]) -> str:
    return "; ".join(
        f"{block['index']}: {block['name'] or '-'} ({block['waldur_offering_uuid'] or '-'})"
        for block in described
    )


def select_block(described: list[dict], offering_index: int | None) -> int:
    if offering_index is not None:
        if offering_index >= len(described):
            raise SelectionError(
                _("The file has %(count)s offerings, numbered from 0: %(blocks)s.")
                % {"count": len(described), "blocks": _blocks_text(described)},
                described,
            )
        return offering_index
    matching = [block["index"] for block in described if block["matches_offering"]]
    if len(matching) == 1:
        return matching[0]
    if not matching and len(described) == 1:
        return 0
    raise SelectionError(
        _(
            "Choose which offering of the file to import with offering_index: %(blocks)s."
        )
        % {"blocks": _blocks_text(described)},
        described,
    )


def _list_placeholders(data: Any) -> list[str]:
    """The names of the placeholders anywhere in data, but under a secret's key.

    A secret's placeholder is dropped with the secret, so it leaves nothing
    unresolved.
    """
    if is_placeholder(data):
        return [placeholder_name(data)]
    names: list[str] = []
    if isinstance(data, list):
        for item in data:
            names.extend(_list_placeholders(item))
    elif isinstance(data, dict):
        for key, value in data.items():
            if not is_secret_name(key):
                names.extend(_list_placeholders(value))
    return list(dict.fromkeys(names))


def _without_secrets(data: Any, path: str, result: ImportedConfiguration) -> Any:
    """A copy of data without secrets or ${...} placeholders, listing their paths.

    A placeholder of a setting that is not a secret is left unresolved: its
    value is in the agent host's environment, but it is not secret.
    """
    if isinstance(data, list):
        return [
            _without_secrets(item, f"{path}[{index}]", result)
            for index, item in enumerate(data)
        ]
    if isinstance(data, dict):
        kept = {}
        for key, value in data.items():
            item_path = f"{path}.{key}"
            if is_secret_name(key):
                result.dropped_secrets.append(item_path)
                continue
            if is_placeholder(value):
                result.leave_unresolved(item_path, value)
                continue
            names = _list_placeholders(value) if isinstance(value, list) else []
            if names:
                # A list is given whole: a placeholder in it leaves it all to
                # values, rather than importing ${...} as text or a list
                # with an item missing.
                result.unresolved.append(
                    {"path": item_path, "placeholder": ", ".join(names)}
                )
                continue
            kept[key] = _without_secrets(value, item_path, result)
        return kept
    return copy.deepcopy(data)


def _map_components(offering, components: Any, result: ImportedConfiguration):
    if not isinstance(components, dict):
        result.ignore("backend_components", NOT_A_MAPPING_REASON)
        return {}
    offering_components = config_builder.build_backend_components(offering)
    imported: dict[str, dict[str, Any]] = {}
    for component_type, fields in components.items():
        component_type = str(component_type)
        path = f"backend_components.{component_type}"
        if not isinstance(fields, dict):
            result.ignore(path, NOT_A_MAPPING_REASON)
            continue
        current = offering_components.get(component_type)
        for name in COMPARED_COMPONENT_FIELDS:
            if name not in fields:
                continue
            offering_value = None if current is None else current.get(name)
            if current is None or offering_value != fields[name]:
                result.component_differences.append(
                    {
                        "component": component_type,
                        "field": name,
                        "offering": offering_value,
                        "imported": fields[name],
                    }
                )
        if current is None:
            result.ignore(path, NO_COMPONENT_REASON)
            continue
        agent_fields = {}
        for name, value in fields.items():
            if name in site_agent_configuration.OFFERING_COMPONENT_FIELDS:
                if name not in COMPARED_COMPONENT_FIELDS:
                    result.ignore(f"{path}.{name}", OFFERING_COMPONENT_REASON)
                continue
            agent_fields[name] = value
        agent_fields = _without_secrets(agent_fields, path, result)
        if agent_fields:
            imported[component_type] = agent_fields
    return imported


def _map_roles(block: dict[str, Any], configuration: dict[str, Any], result):
    for key in ROLE_KEYS:
        if block.get(key):
            configuration[key] = block[key]
    backend_type = block.get("backend_type")
    if not backend_type:
        return
    if any(configuration.get(key) for key in config_builder.AGENT_ROLE_FIELDS):
        result.ignore("backend_type", BACKEND_TYPE_REASON)
        return
    # The agent's legacy single backend: it fills every role an offering runs.
    for key in config_builder.AGENT_ROLE_FIELDS:
        configuration[key] = backend_type


def _settings_block(block: dict[str, Any], keys, group, result) -> dict[str, Any]:
    settings = {}
    for key in keys:
        value = block.get(key)
        if is_placeholder(value):
            result.leave_unresolved(f"{group}.{key}", value)
        elif value is not None:
            # A null is unset, as config_builder leaves it out.
            settings[key] = value
    return settings


def _map_policy_settings(settings: Any, result: ImportedConfiguration) -> None:
    if not isinstance(settings, dict):
        result.ignore("policy_settings", NOT_A_MAPPING_REASON)
        return
    result.policy_settings = {}
    for key, value in settings.items():
        if key in POLICY_FIELDS:
            result.policy_settings[key] = value
        else:
            result.ignore(f"policy_settings.{key}", NOT_A_POLICY_SETTING_REASON)


def map_block(offering, block: dict[str, Any], result: ImportedConfiguration):
    configuration: dict[str, Any] = {}
    _map_roles(block, configuration, result)

    named = {configuration.get(key) for key in ROLE_KEYS if configuration.get(key)}
    custom = sorted(
        str(name)
        for name in named
        if isinstance(name, str) and site_agent_configuration.get_plugin(name) is None
    )
    if custom:
        # The file runs them, so they are plugins installed on the agent host.
        configuration["custom_backends"] = custom

    if "backend_settings" in block:
        settings = block["backend_settings"]
        if isinstance(settings, dict):
            settings = _without_secrets(settings, "backend_settings", result)
        configuration["backend_settings"] = settings

    if "backend_components" in block:
        components = _map_components(offering, block["backend_components"], result)
        if components:
            configuration["backend_components"] = components

    event_settings = _settings_block(
        block, EVENT_SETTING_KEYS, "event_settings", result
    )
    if event_settings:
        configuration["event_settings"] = event_settings
    agent_settings = _settings_block(
        block, AGENT_SETTING_KEYS, "agent_settings", result
    )
    if agent_settings:
        configuration["agent_settings"] = agent_settings

    if "policy_settings" in block:
        _map_policy_settings(block["policy_settings"], result)

    handled = {
        "policy_settings",
        *ROLE_KEYS,
        "backend_type",
        "backend_settings",
        "backend_components",
        *EVENT_SETTING_KEYS,
        *AGENT_SETTING_KEYS,
    }
    for key in block:
        key_text = str(key)
        if key_text in handled:
            continue
        if is_secret_name(key_text):
            result.dropped_secrets.append(key_text)
        else:
            result.ignore(key_text, IGNORED_KEYS.get(key_text, NOT_HELD))
    result.agent_configuration = configuration


_GROUP_KEYS = {
    "event_settings": EVENT_SETTING_KEYS,
    "agent_settings": AGENT_SETTING_KEYS,
}


def _value_error(
    path: str, value: Any, configuration: dict[str, Any], unresolved: set[str]
):
    """Why a value cannot be given for this path, or None when it can.

    The messages never quote the value.
    """
    parts = path.split(".")
    if "[" in path or "]" in path or not all(parts):
        return _("Only a setting inside a mapping can be given.")
    if any(is_secret_name(part) for part in parts):
        return _(
            "This is a secret, or is named like one. Keep it in the agent "
            "configuration file on the agent host; Waldur does not store it."
        )
    group = parts[0]
    if group in _GROUP_KEYS:
        if len(parts) != 2 or parts[1] not in _GROUP_KEYS[group]:
            return _("Unknown setting.")
    elif group == "backend_components":
        if path not in unresolved:
            return _("Only a component field the file leaves unresolved can be given.")
    elif group != "backend_settings" or len(parts) < 2:
        return _(
            "Only backend_settings, event_settings and agent_settings can be given."
        )
    if is_placeholder(value):
        return _("Give the value itself, not a placeholder.")
    if site_agent_configuration.find_secret_settings(value):
        return _("The value holds a secret, or a setting named like one.")
    node: Any = configuration
    for part in parts[:-1]:
        node = node.get(part, {}) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return _("Only a setting inside a mapping can be given.")
    if parts[-1] in node and path not in unresolved:
        return _("The file sets this setting.")
    return None


def apply_values(result: ImportedConfiguration, values: dict[str, Any]) -> None:
    """Fill in values the file leaves to the agent host's environment, or lacks."""
    unresolved = {item["path"] for item in result.unresolved}
    configuration = result.agent_configuration
    errors = {
        path: [message]
        for path, value in values.items()
        if (message := _value_error(path, value, configuration, unresolved))
    }
    if errors:
        raise serializers.ValidationError({"values": errors})
    for path, value in values.items():
        *parents, leaf = path.split(".")
        node = configuration
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = copy.deepcopy(value)
    result.unresolved = [
        item for item in result.unresolved if item["path"] not in values
    ]


def import_configuration(
    offering,
    text: str,
    offering_index: int | None = None,
    values: dict[str, Any] | None = None,
) -> ImportedConfiguration:
    """Map one offering block of an agent configuration file.

    The configuration returned is not validated: the caller validates it as
    update_integration does.
    """
    data, offerings = parse_configuration(text)
    described = describe_blocks(offerings, offering.uuid)
    selected = select_block(described, offering_index)
    result = ImportedConfiguration(
        offerings=described, selected_index=selected, agent_configuration={}
    )
    map_block(offering, offerings[selected], result)
    for key in data:
        if str(key) != "offerings":
            result.ignore(str(key), WHOLE_AGENT_REASON)
    if values:
        apply_values(result, values)
    return result


def configuration_errors(errors: dict[str, Any]) -> dict[str, Any]:
    """OfferingIntegrationUpdateSerializer's errors, as its agent_configuration holds them."""
    errors = dict(errors)
    inner = errors.pop("agent_configuration", {})
    if not isinstance(inner, dict):
        inner = {"non_field_errors": inner}
    else:
        inner = dict(inner)
    for key, messages in errors.items():
        inner.setdefault("non_field_errors", []).extend(
            messages if isinstance(messages, list) else [messages]
        )
    return inner


def sanitized(configuration: dict[str, Any] | None) -> dict[str, Any]:
    """A stored agent configuration with any secret in it left out."""
    configuration = copy.deepcopy(configuration or {})
    if isinstance(configuration.get("backend_settings"), dict):
        configuration["backend_settings"] = site_agent_configuration.without_secrets(
            configuration["backend_settings"]
        )
    components = configuration.get("backend_components")
    if isinstance(components, dict):
        configuration["backend_components"] = {
            component_type: site_agent_configuration.without_secrets(fields)
            if isinstance(fields, dict)
            else fields
            for component_type, fields in components.items()
        }
    return configuration


def _leaves(data: Any, prefix: str = "") -> dict[str, Any]:
    """Dotted paths to the values of nested mappings; lists are values."""
    if isinstance(data, dict) and data:
        found: dict[str, Any] = {}
        for key, value in data.items():
            found.update(_leaves(value, f"{prefix}.{key}" if prefix else str(key)))
        return found
    return {prefix: data} if prefix else {}


def configuration_changes(
    current: dict[str, Any], proposed: dict[str, Any]
) -> list[dict[str, Any]]:
    """The paths whose value an import sets, changes or removes."""
    before, after = _leaves(current), _leaves(proposed)
    return [
        {"path": path, "current": before.get(path), "imported": after.get(path)}
        for path in sorted(before.keys() | after.keys())
        if before.get(path) != after.get(path) or (path in before) != (path in after)
    ]
