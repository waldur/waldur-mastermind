"""A site-agent offering's agent configuration, validated against the plugin catalogue.

Waldur holds everything the agent needs except its secrets: which backend fills
each role, the non-secret ``backend_settings``, and the per-component fields the
agent reads beyond the offering's own components (``unit_factor_reporting`` and
plugin-specific ones). The agent configuration file is generated from it, and the
secrets are added to that file on the agent host.

What a backend accepts comes from the plugin catalogue the site agent exports
(``waldur_site_export_plugin_catalogue``), vendored as
``plugin_catalogue/catalogue.json``; refresh it with
``scripts/update_site_agent_plugin_catalogue.sh``. This module imports nothing
from ``marketplace`` so that the marketplace serializers can use it.
"""

from __future__ import annotations

import copy
import functools
import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import jsonschema
import reversion
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

CATALOGUE_PATH = Path(__file__).parent / "plugin_catalogue" / "catalogue.json"
SUPPORTED_CATALOGUE_VERSIONS = (1,)

ORDER_PROCESSING = "order_processing"
MEMBERSHIP_SYNC = "membership_sync"
REPORTING = "reporting"
USERNAME_MANAGEMENT = "username_management"
ROLES = (ORDER_PROCESSING, MEMBERSHIP_SYNC, REPORTING, USERNAME_MANAGEMENT)

# Agent configuration key -> the role it assigns.
ROLE_FIELDS = {f"{role}_backend": role for role in ROLES}

# Component fields the agent takes from the offering's own components. Setting
# them in backend_components as well would let the two disagree. Both names of
# a field are listed: the agent's (accounting_type, label, limit) and the
# offering component's (billing_type, name, limit_amount), so neither spelling
# can store a second copy.
OFFERING_COMPONENT_FIELDS = frozenset(
    {
        "type",
        "name",
        "billing_type",
        "limit_amount",
        "backend_id",
        "product_code",
        "measured_unit",
        "unit_factor",
        "accounting_type",
        "label",
        "limit",
        "description",
        "min_value",
        "max_value",
        "max_available_limit",
        "default_limit",
        "limit_period",
        "article_code",
        "is_boolean",
        "is_prepaid",
        "min_prepaid_duration",
        "max_prepaid_duration",
        "prepaid_duration_step",
    }
)

# The site agent plugin the legacy settings below are for.
SLURM_BACKEND = "slurm"

# SLURM settings the agent configuration generator always writes, with these
# defaults where the offering sets none: an offering never lacks them.
SLURM_SETTING_DEFAULTS = {
    "default_account": "root",
    "customer_prefix": "",
    "project_prefix": "",
    "allocation_prefix": "",
    "qos_downscaled": "limited",
    "qos_paused": "paused",
    "qos_default": "normal",
}

# Settings the agent configuration generator used to read from plugin_options,
# where no serializer declares them. They belong in backend_settings.
LEGACY_PLUGIN_OPTIONS_SETTINGS = (
    "default_account",
    "customer_prefix",
    "project_prefix",
    "allocation_prefix",
    "qos_downscaled",
    "qos_paused",
    "qos_default",
    "hostname",
    "enable_user_homedir_account_creation",
)


@functools.cache
def load_catalogue() -> dict[str, Any]:
    with CATALOGUE_PATH.open(encoding="utf-8") as catalogue_file:
        catalogue = json.load(catalogue_file)
    if catalogue.get("catalogue_version") not in SUPPORTED_CATALOGUE_VERSIONS:
        raise ValueError(
            f"Unsupported site agent plugin catalogue version "
            f"{catalogue.get('catalogue_version')!r} in {CATALOGUE_PATH}"
        )
    return catalogue


def get_plugins(role: str | None = None) -> list[dict[str, Any]]:
    plugins = load_catalogue()["plugins"]
    if role:
        plugins = [plugin for plugin in plugins if role in plugin["roles"]]
    return plugins


def get_plugin(name: str) -> dict[str, Any] | None:
    return next((p for p in load_catalogue()["plugins"] if p["name"] == name), None)


# Words that make a setting name look like a credential, matched against its
# underscore-separated parts; the site agent's guard test uses the same list.
CREDENTIAL_WORDS = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "pwd",
        "token",
        "credential",
        "credentials",
        "key",
        "apikey",
        "private",
        "pem",
        "dsn",
    }
)


def looks_like_credential(name: str) -> bool:
    return bool(CREDENTIAL_WORDS & set(str(name).lower().split("_")))


def _schema_property_names(schema: Any) -> tuple[set[str], set[str]]:
    """The (secret, non-secret) property names anywhere in a JSON Schema."""
    secret: set[str] = set()
    plain: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        for name, prop in (node.get("properties") or {}).items():
            (secret if _is_secret(prop) else plain).add(name)
        for value in node.values():
            walk(value)

    walk(schema)
    return secret, plain


@functools.cache
def secret_setting_names() -> frozenset[str]:
    """The name of every setting a catalogue plugin, or the agent, keeps secret."""
    catalogue = load_catalogue()
    names = set(catalogue.get("offering_secret_fields") or [])
    for plugin in catalogue["plugins"]:
        for path in plugin.get("secret_settings") or []:
            leaf = path.split(".")[-1]
            names.add(leaf[:-2] if leaf.endswith("[]") else leaf)
        names |= _schema_property_names(plugin.get("settings_schema"))[0]
    names.discard("*")
    return frozenset(names)


@functools.cache
def declared_plain_setting_names() -> frozenset[str]:
    """Settings some catalogue plugin declares as not secret, such as token_url."""
    plain: set[str] = set()
    for plugin in load_catalogue()["plugins"]:
        plain |= _schema_property_names(plugin.get("settings_schema"))[1]
    return frozenset(plain - secret_setting_names())


def find_secret_settings(data: Any, prefix: str = "") -> list[str]:
    """Dotted paths in data whose key is a secret, or looks like one.

    Matched by name at any depth and through lists, so a secret is caught
    whatever shape it is sent in: under a block of another backend, inside a
    list, or under a key no schema declares. A name that looks like a
    credential is refused unless some plugin declares it as a plain setting.
    """
    found: list[str] = []
    if isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(find_secret_settings(item, f"{prefix}[{index}]"))
        return found
    if not isinstance(data, dict):
        return found
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if _names_a_secret(key):
            found.append(path)
            continue
        found.extend(find_secret_settings(value, path))
    return found


def _names_a_secret(key: Any) -> bool:
    """Whether a settings key is a secret: declared one, or named like one."""
    return key in secret_setting_names() or (
        looks_like_credential(key) and key not in declared_plain_setting_names()
    )


def configuration_secret_paths(configuration: dict[str, Any]) -> list[str]:
    """The secrets in an agent configuration, as paths from its top.

    Only values are searched: backend_settings, and the fields of each
    backend_components entry, whose keys are component types, not settings.
    """
    found = find_secret_settings(
        configuration.get("backend_settings") or {}, "backend_settings"
    )
    components = configuration.get("backend_components") or {}
    if isinstance(components, dict):
        for component_type, fields in components.items():
            found.extend(
                find_secret_settings(fields, f"backend_components.{component_type}")
            )
    return found


def takes_legacy_settings(agent_configuration: dict[str, Any]) -> bool:
    """Whether the legacy settings belong in this configuration.

    They are SLURM settings, so they do when every backend it names is SLURM,
    or when it names none: the generator then writes SLURM for every role.
    """
    named = {agent_configuration.get(field) for field in ROLE_FIELDS} - {None, ""}
    return named <= {SLURM_BACKEND}


def move_legacy_plugin_options(
    plugin_options: dict[str, Any], agent_configuration: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move the legacy SLURM settings out of plugin_options into backend_settings.

    A value already in backend_settings wins. When the configuration uses
    another backend, the settings are taken out of plugin_options but not
    moved: that backend's schema would refuse them. Returns new dicts; the
    inputs are left untouched.
    """
    plugin_options = dict(plugin_options or {})
    agent_configuration = copy.deepcopy(agent_configuration or {})
    legacy = {
        key: plugin_options.pop(key)
        for key in LEGACY_PLUGIN_OPTIONS_SETTINGS
        if key in plugin_options
    }
    if legacy and takes_legacy_settings(agent_configuration):
        settings = agent_configuration.setdefault("backend_settings", {})
        for key, value in legacy.items():
            settings.setdefault(key, value)
    return plugin_options, agent_configuration


def _strip_secrets(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop the secret properties from a JSON Schema, and from what it requires.

    Secrets are filled in on the agent host, so a configuration held in Waldur is
    complete without them.
    """
    schema = copy.deepcopy(schema)

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            secret = [name for name, prop in properties.items() if _is_secret(prop)]
            for name in secret:
                del properties[name]
            if secret and isinstance(node.get("required"), list):
                node["required"] = [n for n in node["required"] if n not in secret]
        for value in node.values():
            walk(value)

    walk(schema)
    return schema


def _is_secret(prop: object) -> bool:
    return isinstance(prop, dict) and prop.get("secret") is True


def _schema_errors(schema: dict[str, Any], data: dict[str, Any]) -> dict[str, list]:
    errors: dict[str, list] = {}
    validator = jsonschema.Draft202012Validator(schema)
    for error in validator.iter_errors(data):
        base = ".".join(str(part) for part in error.absolute_path)
        if error.validator == "required":
            # jsonschema reports each missing property as an error of its own,
            # each carrying the whole required list: name each field once.
            for name in error.validator_value:
                if isinstance(error.instance, dict) and name not in error.instance:
                    field = f"{base}.{name}" if base else name
                    messages = errors.setdefault(field, [])
                    if _("This setting is required.") not in messages:
                        messages.append(_("This setting is required."))
        elif error.validator == "additionalProperties" and isinstance(
            error.instance, dict
        ):
            allowed = set(error.schema.get("properties", {}))
            for name in error.instance:
                if name not in allowed:
                    field = f"{base}.{name}" if base else name
                    errors.setdefault(field, []).append(_("Unknown setting."))
        else:
            # error.message quotes the rejected value, which may be a secret
            # sent in the wrong shape; answer with a fixed text instead.
            errors.setdefault(base or "non_field_errors", []).append(
                _schema_error_text(error)
            )
    return errors


def _schema_error_text(error: jsonschema.ValidationError) -> str:
    if error.validator == "type":
        expected = error.validator_value
        if isinstance(expected, list):
            expected = " or ".join(str(item) for item in expected)
        return _("Expected %(type)s.") % {"type": expected}
    if error.validator in ("anyOf", "oneOf", "allOf"):
        return _("Does not match any allowed form of this setting.")
    if error.validator in ("enum", "const"):
        return _("Not an allowed value.")
    if error.validator in (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    ):
        return _("Out of the allowed range (%(rule)s %(limit)s).") % {
            "rule": error.validator,
            "limit": error.validator_value,
        }
    return _("Invalid value.")


def _check_shared_data(
    data: dict[str, Any],
    schemas: dict[str, dict[str, Any]],
    any_unchecked_backend: bool,
) -> dict[str, list]:
    """Validate data that several backends read, the way the agent does.

    A strict schema (``additionalProperties: false``) is shown only the keys it
    declares whenever another backend reads the same data. A key no schema
    declares is refused only when every backend in use has a strict schema: a
    custom backend, or one with an open schema, may read any key.
    """
    errors: dict[str, list] = {}
    sharing = len(schemas) > 1 or any_unchecked_backend
    for name, schema in schemas.items():
        strict = schema.get("additionalProperties") is False
        subset = data
        if sharing and strict:
            declared = set(schema.get("properties", {}))
            subset = {key: value for key, value in data.items() if key in declared}
        for field, messages in _schema_errors(schema, subset).items():
            errors.setdefault(field, []).extend(f"{name}: {m}" for m in messages)
    if sharing and not any_unchecked_backend and schemas:
        if all(
            schema.get("additionalProperties") is False for schema in schemas.values()
        ):
            known = set()
            for schema in schemas.values():
                known.update(schema.get("properties", {}))
            for key in data:
                if key not in known:
                    errors.setdefault(key, []).append(_("Unknown setting."))
    return errors


class EventSettingsSerializer(serializers.Serializer):
    """How the agent receives events from Waldur instead of polling for them.

    The same fields as an offering of the agent's configuration file. Waldur
    does not derive them: the broker address it uses itself is often not the
    one an agent outside reaches.
    """

    stomp_enabled = serializers.BooleanField(required=False)
    stomp_membership_sync_enabled = serializers.BooleanField(
        required=False,
        allow_null=True,
        help_text=_(
            "Use STOMP for membership sync; the agent follows stomp_enabled when "
            "this is unset."
        ),
    )
    stomp_ws_host = serializers.CharField(required=False, max_length=255)
    stomp_ws_port = serializers.IntegerField(
        required=False, min_value=1, max_value=65535
    )
    stomp_ws_path = serializers.CharField(required=False, max_length=255)
    websocket_use_tls = serializers.BooleanField(required=False)

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {key: [_("Unknown event setting.")] for key in unknown}
                )
        return dict(super().to_internal_value(data))


class StrictBooleanField(serializers.BooleanField):
    """A boolean sent as one: "yes", 1 or "false" are refused, not coerced."""

    def to_internal_value(self, data):
        if not isinstance(data, bool):
            self.fail("invalid", input=data)
        return data


# Agent settings whose change decides what the agent imports into Waldur,
# renames, or leaves with access on the backend.
TRUST_SENSITIVE_AGENT_SETTINGS = (
    "resource_import_enabled",
    "username_reconciliation_enabled",
    "preserve_unmanaged_backend_users",
)


class AgentSettingsSerializer(serializers.Serializer):
    """The agent's per-offering switches, written at the top of its offering.

    The same names as in the agent's configuration file.
    """

    resource_import_enabled = StrictBooleanField(
        required=False,
        help_text=_("Import resources found on the backend into Waldur."),
    )
    username_reconciliation_enabled = StrictBooleanField(
        required=False,
        help_text=_("Periodically reconcile usernames from the backend."),
    )
    preserve_unmanaged_backend_users = StrictBooleanField(
        required=False,
        help_text=_(
            "Membership sync keeps backend users Waldur has never known as "
            "offering users, instead of removing every user not on the "
            "resource team."
        ),
    )
    omit_anomalous_usage_components = StrictBooleanField(
        required=False,
        help_text=_(
            "Report usage without a decreasing component instead of withholding "
            "the whole report."
        ),
    )

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {key: [_("Unknown agent setting.")] for key in unknown}
                )
        return dict(super().to_internal_value(data))


class AgentConfigurationSerializer(serializers.Serializer):
    """The agent side of a site-agent offering, minus its secrets.

    Writing it replaces the whole configuration. ``backend_components`` is keyed
    by offering component type and holds only what the agent reads beyond the
    offering's own components.
    """

    order_processing_backend = serializers.CharField(
        required=False, allow_blank=True, max_length=100
    )
    membership_sync_backend = serializers.CharField(
        required=False, allow_blank=True, max_length=100
    )
    reporting_backend = serializers.CharField(
        required=False, allow_blank=True, max_length=100
    )
    username_management_backend = serializers.CharField(
        required=False, allow_blank=True, max_length=100
    )
    custom_backends = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        help_text=_(
            "Backends that are not in the plugin catalog, such as a site's own "
            "plugin. Their settings and components are not validated."
        ),
    )
    backend_settings = serializers.DictField(required=False)
    backend_components = serializers.DictField(
        child=serializers.DictField(), required=False
    )
    event_settings = EventSettingsSerializer(required=False)
    agent_settings = AgentSettingsSerializer(required=False)

    def to_internal_value(self, data):
        # A key the agent would read but this serializer does not declare, such
        # as a credential sent at the top level, is refused rather than dropped.
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {key: [_("Unknown agent configuration key.")] for key in unknown}
                )
        return super().to_internal_value(data)

    def validate(self, attrs):
        errors: dict[str, Any] = {}
        custom = set(attrs.get("custom_backends") or [])
        shadowing = sorted(name for name in custom if get_plugin(name) is not None)
        if shadowing:
            raise serializers.ValidationError(
                {
                    "custom_backends": [
                        _(
                            "%(name)s is in the plugin catalog; name it as a "
                            "backend directly."
                        )
                        % {"name": name}
                        for name in shadowing
                    ]
                }
            )
        plugins: dict[str, dict[str, Any]] = {}
        unchecked = False

        for field, role in ROLE_FIELDS.items():
            name = attrs.get(field)
            if not name:
                continue
            if name in custom:
                unchecked = True
                continue
            plugin = get_plugin(name)
            if plugin is None:
                errors[field] = [
                    _(
                        "Unknown backend %(name)s. Add it to custom_backends if it "
                        "is a plugin of your own."
                    )
                    % {"name": name}
                ]
            elif role not in plugin["roles"]:
                errors[field] = [
                    _("Backend %(name)s cannot be used for %(role)s.")
                    % {"name": name, "role": role.replace("_", " ")}
                ]
            else:
                plugins[name] = plugin
        if errors:
            raise serializers.ValidationError(errors)

        settings = attrs.get("backend_settings") or {}
        settings_errors = self._secret_errors(settings)
        if not settings_errors:
            schemas = {
                name: _strip_secrets(plugin["settings_schema"])
                for name, plugin in plugins.items()
                if plugin.get("settings_schema")
            }
            open_backend = unchecked or len(schemas) < len(plugins)
            settings_errors = _check_shared_data(settings, schemas, open_backend)
        if settings_errors:
            errors["backend_settings"] = settings_errors

        components = attrs.get("backend_components") or {}
        component_errors = self._component_errors(components, plugins, unchecked)
        # The keys of backend_components are component types, not settings: a
        # component named license_key is not a secret, so only its fields are
        # searched.
        for component_type, fields in components.items():
            for path in find_secret_settings(fields):
                component_errors.setdefault(component_type, {}).setdefault(
                    path, []
                ).append(
                    _("Secrets stay in the agent configuration file on the agent host.")
                )
        if component_errors:
            errors["backend_components"] = component_errors

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    @staticmethod
    def _secret_errors(settings: dict[str, Any]) -> dict[str, list]:
        return {
            path: [
                _(
                    "This is a secret, or is named like one. Keep it in the agent "
                    "configuration file on the agent host; Waldur does not store it."
                )
            ]
            for path in find_secret_settings(settings)
        }

    @staticmethod
    def _component_errors(
        components: dict[str, dict[str, Any]],
        plugins: dict[str, dict[str, Any]],
        unchecked: bool,
    ) -> dict[str, Any]:
        errors: dict[str, Any] = {}
        schemas = {
            name: plugin["component_schema"]
            for name, plugin in plugins.items()
            if plugin.get("component_schema")
        }
        # With no backend named there is nothing to check fields against, as
        # with backend_settings: accept them rather than refuse every one.
        open_backend = unchecked or not plugins or len(schemas) < len(plugins)
        for component_type, fields in components.items():
            component_errors: dict[str, list] = {}
            for name in fields:
                if name in OFFERING_COMPONENT_FIELDS:
                    component_errors[name] = [
                        _("Set this on the offering component itself.")
                    ]
            reporting_factor = fields.get("unit_factor_reporting")
            if reporting_factor is not None and (
                isinstance(reporting_factor, bool)
                or not isinstance(reporting_factor, int | float)
                or not math.isfinite(reporting_factor)
                or reporting_factor <= 0
            ):
                component_errors["unit_factor_reporting"] = [
                    _("Must be a positive number.")
                ]
            plugin_fields = {
                key: value
                for key, value in fields.items()
                if key not in OFFERING_COMPONENT_FIELDS
                and key != "unit_factor_reporting"
            }
            if plugin_fields:
                for field, messages in _check_shared_data(
                    plugin_fields, schemas, open_backend
                ).items():
                    component_errors.setdefault(field, []).extend(messages)
                if not schemas and not open_backend:
                    for field in plugin_fields:
                        component_errors.setdefault(field, []).append(
                            _("Unknown component setting.")
                        )
            if component_errors:
                errors[component_type] = component_errors
        return errors


def follow_component_changes(offering, changes: dict[str, str | None]) -> None:
    """Keep backend_components in step with the offering's components.

    ``changes`` maps a component type to its new type, or to None when the
    component is removed, so that the stored configuration never names a
    component the offering no longer has, which the next save would refuse.

    The configuration is re-read under a row lock rather than taken from the
    caller's copy, which may predate a concurrent update_integration, and is
    written once, only when an entry moves.
    """
    model = type(offering)
    with transaction.atomic():
        configuration = dict(
            model.objects.select_for_update()
            .only("agent_configuration")
            .get(pk=offering.pk)
            .agent_configuration
            or {}
        )
        components = dict(configuration.get("backend_components") or {})
        moved = {
            old_type: components.pop(old_type)
            for old_type in changes
            if old_type in components
        }
        if moved:
            for old_type, fields in moved.items():
                if changes[old_type] is not None:
                    components[changes[old_type]] = fields
            if components:
                configuration["backend_components"] = components
            else:
                configuration.pop("backend_components", None)
            model.objects.filter(pk=offering.pk).update(
                agent_configuration=configuration
            )
    offering.agent_configuration = configuration


def validate_component_types(
    agent_configuration: dict[str, Any], component_types: Iterable[str]
) -> None:
    """Refuse backend_components entries for components the offering does not have."""
    known = set(component_types)
    unknown = sorted(set(agent_configuration.get("backend_components") or {}) - known)
    if unknown:
        raise serializers.ValidationError(
            {
                "agent_configuration": {
                    "backend_components": {
                        component_type: [_("The offering has no such component.")]
                        for component_type in unknown
                    }
                }
            }
        )


# How much a backend needs one of its secrets.
REQUIRED = "required"
# Required by the block it sits in, which is itself optional: needed only when
# that block is configured (ldap.bind_password once there is an ldap block).
REQUIRED_WITH_PARENT = "required_with_parent"
OPTIONAL = "optional"


def _resolve(schema: dict[str, Any], node: Any) -> dict[str, Any]:
    """Follow $ref and single-schema wrappers to the object schema."""
    for _depth in range(10):
        if not isinstance(node, dict):
            return {}
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            node = schema.get("$defs", {}).get(ref.rsplit("/", 1)[-1], {})
            continue
        if "properties" not in node:
            # A wrapper around one schema: Optional[...] is anyOf with null,
            # and pydantic puts a described $ref under allOf.
            options = [
                option
                for key in ("anyOf", "oneOf", "allOf")
                for option in node.get(key) or []
                if isinstance(option, dict) and option.get("type") != "null"
            ]
            if len(options) == 1:
                node = options[0]
                continue
        return node
    return {}


def secret_requirement(schema: dict[str, Any], path: str) -> str:
    """Whether a backend's settings need the secret at a dotted path."""
    node = schema
    parents_required = True
    for index, segment in enumerate(path.split(".")):
        node = _resolve(schema, node)
        if segment == "*" or segment.endswith("[]"):
            # A secret inside a mapping or list of entries is needed per entry.
            return REQUIRED_WITH_PARENT
        required = segment in (node.get("required") or [])
        if index == len(path.split(".")) - 1:
            if not required:
                return OPTIONAL
            return REQUIRED if parents_required else REQUIRED_WITH_PARENT
        parents_required = parents_required and required
        node = (node.get("properties") or {}).get(segment, {})
    return OPTIONAL


# Stricter first: what a path needs when backends disagree.
_REQUIREMENT_ORDER = (REQUIRED, REQUIRED_WITH_PARENT, OPTIONAL)


def secret_settings(backend_names: Iterable[str]) -> list[tuple[str, str, str]]:
    """The secret settings of these backends as (backend, path, requirement).

    These are what the operator adds to the generated configuration on the
    agent host. A path is listed once, under the first backend that has it,
    and needed as much as the backend that needs it most: two backends can
    share a block (ldap) under schemas that disagree on what it requires.
    """
    found: dict[str, tuple[str, str]] = {}
    for name in backend_names:
        plugin = get_plugin(name)
        if plugin is None:
            continue
        schema = plugin.get("settings_schema") or {}
        for path in plugin.get("secret_settings") or []:
            requirement = secret_requirement(schema, path)
            if path not in found:
                found[path] = (name, requirement)
                continue
            listed_under, current = found[path]
            if _REQUIREMENT_ORDER.index(requirement) < _REQUIREMENT_ORDER.index(
                current
            ):
                found[path] = (listed_under, requirement)
    return [(name, path, requirement) for path, (name, requirement) in found.items()]


def without_secrets(settings: dict[str, Any]) -> dict[str, Any]:
    """A copy of backend_settings with every secret, in any shape, removed.

    The serializer refuses secrets, so this only matters for data written some
    other way; it keeps the generated configuration free of them regardless,
    matching them by the same rule as find_secret_settings.
    """
    return _drop_secrets(settings or {})


def _drop_secrets(data: Any) -> Any:
    # Built anew rather than removed by path, so no key or nesting can make
    # a path ambiguous (a key with a dot in it, a list inside a list).
    if isinstance(data, list):
        return [_drop_secrets(item) for item in data]
    if isinstance(data, dict):
        return {
            key: _drop_secrets(value)
            for key, value in data.items()
            if not _names_a_secret(key)
        }
    return copy.deepcopy(data)


def setting_defaults(backend_names: Iterable[str]) -> dict[str, Any]:
    """The defaults the catalogue declares for these backends' settings.

    Top-level, non-secret settings with a default other than null, in backend
    order; the first backend to declare a setting gives its default.
    """
    defaults: dict[str, Any] = {}
    for name in backend_names:
        plugin = get_plugin(name)
        if plugin is None:
            continue
        properties = (plugin.get("settings_schema") or {}).get("properties") or {}
        for key, prop in properties.items():
            if (
                key in defaults
                or not isinstance(prop, dict)
                or _is_secret(prop)
                or prop.get("default") is None
            ):
                continue
            defaults[key] = copy.deepcopy(prop["default"])
    return defaults


# The comment of the offering revision that records an agent configuration
# change: the offering's history then says who changed it and when. The
# history endpoint leaves the configuration itself out (REDACTED_VERSION_FIELDS).
CHANGE_REVISION_COMMENT = "Agent configuration changed"


def record_change(offering, user) -> None:
    """Record who changed an offering's agent configuration, and when.

    Where the agent connects and what it runs now come from this
    configuration, so a change to it should be traceable to a person.
    """
    with reversion.create_revision():
        reversion.add_to_revision(offering)
        reversion.set_user(user)
        reversion.set_comment(CHANGE_REVISION_COMMENT)


# Parts of the configuration that decide which code the agent runs, where its
# events come from and what it imports or removes. Changing them, a setting the
# catalogue marks sensitive,
# or a backend setting the backends in use do not declare, needs
# OFFERING.UPDATE_AGENT_CONFIGURATION.
BACKEND_FIELDS = (*ROLE_FIELDS, "custom_backends")
# agent_settings only in part: TRUST_SENSITIVE_AGENT_SETTINGS.
TRUST_SENSITIVE_FIELDS = (*BACKEND_FIELDS, "event_settings", "agent_settings")

# Stands for a key that is not there, so that a value of "" or null is a change
# from it: the agent reads an absent setting as its default, an empty one as is.
_ABSENT = object()


@functools.cache
def sensitive_setting_paths() -> tuple[str, ...]:
    """The backend_settings paths any catalogue plugin marks sensitive.

    Every plugin's, not only those a configuration names: backend_settings are
    read by every backend an offering uses, and a change of backend is guarded
    anyway.
    """
    paths: set[str] = set()
    for plugin in load_catalogue()["plugins"]:
        paths.update(plugin.get("sensitive_settings") or [])
    return tuple(sorted(paths))


@functools.cache
def sensitive_component_paths() -> tuple[str, ...]:
    """The backend_components field paths any catalogue plugin marks sensitive."""
    paths: set[str] = set()
    for plugin in load_catalogue()["plugins"]:
        paths.update(plugin.get("sensitive_component_settings") or [])
    return tuple(sorted(paths))


def _values_at(data: Any, segments: list[str], prefix: str) -> dict[str, Any]:
    """The values at a catalogue path in data, by concrete dotted path.

    ``*`` stands for every key of a mapping and a ``[]`` suffix for every item
    of a list, as in the catalogue's paths. A path that is not there is left
    out.
    """
    if not segments:
        return {prefix: data}
    if not isinstance(data, dict):
        return {}
    segment, rest = segments[0], segments[1:]
    if segment == "*":
        found: dict[str, Any] = {}
        for key, value in data.items():
            found.update(_values_at(value, rest, f"{prefix}.{key}"))
        return found
    is_list = segment.endswith("[]")
    name = segment[:-2] if is_list else segment
    if name not in data:
        return {}
    value = data[name]
    path = f"{prefix}.{name}"
    if not is_list:
        return _values_at(value, rest, path)
    if not isinstance(value, list):
        # Not the shape the schema allows; compare the value as a whole.
        return {path: value}
    found = {}
    for index, item in enumerate(value):
        found.update(_values_at(item, rest, f"{path}[{index}]"))
    return found


def _changed_at(old: Any, new: Any, paths: Iterable[str], prefix: str) -> set[str]:
    """Concrete paths, of the given catalogue paths, whose value differs.

    Exact: a key that appears, disappears or turns empty is a change.
    """
    changed: set[str] = set()
    for path in paths:
        segments = path.split(".")
        before = _values_at(old, segments, prefix)
        after = _values_at(new, segments, prefix)
        for concrete in before.keys() | after.keys():
            if before.get(concrete, _ABSENT) != after.get(concrete, _ABSENT):
                changed.add(concrete)
    return changed


def _plugins_in_use(*configurations: dict[str, Any]) -> list[dict[str, Any]]:
    """The catalogue plugins the configurations name for a role.

    With no backend named the generator writes SLURM for every role, so it is
    SLURM's settings that are read. A custom backend has no catalogue entry.
    """
    names = {
        configuration.get(field)
        for configuration in configurations
        for field in ROLE_FIELDS
    } - {None, ""}
    if not names:
        names = {SLURM_BACKEND}
    return [plugin for name in sorted(names) if (plugin := get_plugin(name))]


def _undeclared_keys(
    old: dict[str, Any],
    new: dict[str, Any],
    models: list[tuple[dict[str, Any], dict[str, Any]]],
    prefix: str,
) -> list[str]:
    """Changed keys of a mapping that none of the object schemas declares.

    ``models`` are (root schema, resolved object schema) pairs, one per
    plugin that declares this mapping; a declared key is followed into its
    value.
    """
    found: list[str] = []
    for key in old.keys() | new.keys():
        children = []
        for root, node in models:
            properties = node.get("properties") or {}
            additional = node.get("additionalProperties")
            if key in properties:
                children.append((root, properties[key]))
            elif isinstance(additional, dict):
                children.append((root, additional))
        before, after = old.get(key, _ABSENT), new.get(key, _ABSENT)
        path = f"{prefix}.{key}"
        if not children:
            if before != after:
                found.append(path)
        else:
            found.extend(_undeclared_changes(before, after, children, path))
    return found


def _undeclared_changes(
    old: Any,
    new: Any,
    entries: list[tuple[dict[str, Any], Any]],
    prefix: str,
) -> list[str]:
    """Changes in a declared value under keys its schemas do not declare.

    A value declared as a model, or as a mapping or list of them, is followed;
    any other value (a string, a free-form mapping) is a declared leaf.
    """
    nodes = [(root, _resolve(root, node)) for root, node in entries]
    models = [
        (root, node)
        for root, node in nodes
        if isinstance(node.get("properties"), dict)
        or isinstance(node.get("additionalProperties"), dict)
    ]
    if models and (isinstance(old, dict) or isinstance(new, dict)):
        return _undeclared_keys(
            old if isinstance(old, dict) else {},
            new if isinstance(new, dict) else {},
            models,
            prefix,
        )
    items = [
        (root, node["items"])
        for root, node in nodes
        if isinstance(node.get("items"), dict)
    ]
    if items and (isinstance(old, list) or isinstance(new, list)):
        old_items = old if isinstance(old, list) else []
        new_items = new if isinstance(new, list) else []
        found: list[str] = []
        for index in range(max(len(old_items), len(new_items))):
            found.extend(
                _undeclared_changes(
                    old_items[index] if index < len(old_items) else _ABSENT,
                    new_items[index] if index < len(new_items) else _ABSENT,
                    items,
                    f"{prefix}[{index}]",
                )
            )
        return found
    return []


def _unset(value: Any) -> Any:
    # A role left blank names no backend, as one left out does.
    return None if value in (None, "", [], {}) else value


def _set_values(value: Any) -> dict[str, Any]:
    # Rendered with null for a field that was never set, so null is unset.
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if item is not None}


def _changed_keys(
    old: Any, new: Any, prefix: str, keys: Iterable[str] | None = None
) -> set[str]:
    """The keys of a settings block whose value is set, unset or changed."""
    old_values, new_values = _set_values(old), _set_values(new)
    compared = old_values.keys() | new_values.keys()
    if keys is not None:
        compared &= set(keys)
    return {
        f"{prefix}.{key}"
        for key in compared
        if old_values.get(key, _ABSENT) != new_values.get(key, _ABSENT)
    }


def trust_sensitive_changes(
    old: dict[str, Any] | None, new: dict[str, Any] | None
) -> list[str]:
    """The trust-sensitive parts that differ between two agent configurations.

    As paths from the top of the configuration:

    - role backends and custom backends;
    - each event setting;
    - each agent setting in TRUST_SENSITIVE_AGENT_SETTINGS;
    - each backend setting or component field at a path the plugin catalogue
      marks sensitive;
    - each backend setting that the plugins in use do not declare, at any
      depth: an open schema or a custom backend lets it through validation,
      and the agent may read it. Fail closed rather than guess.
    """
    old, new = old or {}, new or {}
    changed = {
        field
        for field in BACKEND_FIELDS
        if _unset(old.get(field)) != _unset(new.get(field))
    }

    changed |= _changed_keys(
        old.get("event_settings"), new.get("event_settings"), "event_settings"
    )
    changed |= _changed_keys(
        old.get("agent_settings"),
        new.get("agent_settings"),
        "agent_settings",
        TRUST_SENSITIVE_AGENT_SETTINGS,
    )

    old_settings = old.get("backend_settings") or {}
    new_settings = new.get("backend_settings") or {}
    changed |= _changed_at(
        old_settings, new_settings, sensitive_setting_paths(), "backend_settings"
    )
    schemas = [
        (plugin["settings_schema"], plugin["settings_schema"])
        for plugin in _plugins_in_use(old, new)
        if plugin.get("settings_schema")
    ]
    if isinstance(old_settings, dict) and isinstance(new_settings, dict):
        changed.update(
            _undeclared_keys(old_settings, new_settings, schemas, "backend_settings")
        )

    old_components = old.get("backend_components") or {}
    new_components = new.get("backend_components") or {}
    component_paths = sensitive_component_paths()
    if component_paths:
        for component_type in old_components.keys() | new_components.keys():
            changed |= _changed_at(
                old_components.get(component_type) or {},
                new_components.get(component_type) or {},
                component_paths,
                f"backend_components.{component_type}",
            )
    return sorted(changed)
