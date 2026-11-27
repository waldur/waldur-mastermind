"""Builds a site agent's offering configuration from what Waldur holds.

One build serves two readers: the YAML file a person downloads and fills in
(``generate_site_agent_config``), where each secret is a placeholder, and the
JSON the agent loads itself (``site_agent_config``), where a secret is left out
and only listed. The two cannot disagree because they share these builders.
"""

from __future__ import annotations

import hashlib
import json
import re
from secrets import token_hex
from typing import Any

import yaml
from django.utils import timezone

from waldur_mastermind.marketplace import (
    agent_configuration as site_agent_configuration,
)
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.policy.models import SlurmPeriodicUsagePolicy

# An offering whose agent configuration names no backend for any role runs
# SLURM for all three, as every generated configuration did before roles
# could be set. Once one role is set, an unset role stays off: the agent
# skips a role whose backend is empty.
DEFAULT_AGENT_BACKEND = "slurm"
# The roles an offering runs; username management is set on its own.
AGENT_ROLE_FIELDS = tuple(
    field
    for field, role in site_agent_configuration.ROLE_FIELDS.items()
    if role != site_agent_configuration.USERNAME_MANAGEMENT
)
# What an offering with no roles set gets, as before agent configurations.
LEGACY_API_TOKEN_PLACEHOLDER = "<YOUR_API_TOKEN_HERE>"
LEGACY_HOSTNAME_PLACEHOLDER = "<YOUR_SLURM_HOST>"

REQUIRED_SECRET = site_agent_configuration.REQUIRED

SECRET_REQUIREMENT_LABELS = {
    site_agent_configuration.REQUIRED: "",
    site_agent_configuration.REQUIRED_WITH_PARENT: ", required if its block is set",
    site_agent_configuration.OPTIONAL: ", optional",
}

# The fields only the file has: the agent needs them to call Waldur at all, so
# it cannot take them from Waldur.
BOOTSTRAP_FIELDS = ("waldur_api_url", "waldur_api_token", "waldur_offering_uuid")

# Where an offering's configuration comes from, as the agent's
# configuration_source names it: the file (local) or Waldur (waldur).
LOCAL_SOURCE = "local"
WALDUR_SOURCE = "waldur"

# Where the agent's token comes from: a site agent token bound to the offering,
# not the user's own API token, which reaches everything the user can.
API_TOKEN_HINT = (
    "Site agent token of the offering (offering -> Integration -> Credentials -> "
    "Actions -> Create agent token); needs waldur-site-agent 1.0.8-rc.13 or later"
)


def has_agent_roles(offering) -> bool:
    configuration = offering.agent_configuration or {}
    return any(configuration.get(field) for field in AGENT_ROLE_FIELDS)


def offering_backends(offering) -> dict[str, str]:
    configuration = offering.agent_configuration or {}
    if not has_agent_roles(offering):
        backends = dict.fromkeys(AGENT_ROLE_FIELDS, DEFAULT_AGENT_BACKEND)
    else:
        backends = {
            field: configuration[field]
            for field in AGENT_ROLE_FIELDS
            if configuration.get(field)
        }
    if configuration.get("username_management_backend"):
        backends["username_management_backend"] = configuration[
            "username_management_backend"
        ]
    return backends


def build_offering_config(
    offering, waldur_api_url, include_policy_settings, generated
) -> dict[str, Any]:
    """Build configuration for a single offering.

    An offering with no agent roles set is generated as before agent
    configurations existed, apart from backend_type, which the agent
    requires: the same placeholders, hostname and SLURM settings. With roles
    set, the configuration follows the agent configuration, catalogue
    defaults fill in what it leaves out, and secrets take secret_format.

    With ListedSecrets as ``generated`` there are no placeholders and no
    bootstrap fields: secrets are only listed, and waldur_api_url is unused.
    """
    legacy = not has_agent_roles(offering)
    backends = offering_backends(offering)
    secrets = site_agent_configuration.secret_settings(dict.fromkeys(backends.values()))
    configuration = offering.agent_configuration or {}

    offering_config: dict[str, Any] = {"name": offering.name}
    if generated.with_placeholders:
        if legacy and generated.secret_format == GeneratedAgentConfig.COMMENT:
            api_token = LEGACY_API_TOKEN_PLACEHOLDER
            generated.add_secret(
                offering, "waldur_api_token", None, REQUIRED_SECRET, in_file=True
            )
        else:
            api_token = generated.placeholder(
                offering,
                "waldur_api_token",
                None,
                REQUIRED_SECRET,
                API_TOKEN_HINT,
            )
        offering_config.update(
            {
                "waldur_api_url": waldur_api_url,
                "waldur_api_token": api_token,
                "waldur_offering_uuid": str(offering.uuid),
            }
        )
    offering_config.update(
        {
            # Required by the agent; generated configurations used to omit it
            # and so failed to load. It names the first backend in role order.
            "backend_type": next(
                backends[field] for field in AGENT_ROLE_FIELDS if field in backends
            ),
            **backends,
            **event_settings(configuration),
            **agent_settings(configuration),
            "backend_settings": build_backend_settings(
                offering, backends, secrets, legacy, generated
            ),
            "backend_components": build_backend_components(offering),
        }
    )
    if legacy:
        generated.legacy_offerings = True

    if include_policy_settings:
        policy_settings = build_policy_settings(offering)
        if policy_settings:
            offering_config["policy_settings"] = policy_settings

    return offering_config


def build_bootstrap_offering_config(
    offering, waldur_api_url, generated
) -> dict[str, Any]:
    """The block of an offering that takes its configuration from Waldur.

    The agent reads everything else from site_agent_config when it starts, and
    refuses any other key here: the block holds how to reach Waldur and the
    secrets of the configuration Waldur serves. A secret gets a placeholder
    where it would in a local configuration, that is where its block is
    configured and it is not optional; the others are only listed. An empty
    placeholder of an optional secret would set it to "" once the agent merges
    the file in, and one of a block Waldur does not serve fails the load.
    """
    listed = ListedSecrets()
    served = build_offering_config(offering, None, True, listed)
    served_settings = served["backend_settings"]

    offering_config: dict[str, Any] = {
        "name": offering.name,
        "configuration_source": WALDUR_SOURCE,
        "waldur_api_url": waldur_api_url,
        "waldur_api_token": generated.placeholder(
            offering,
            "waldur_api_token",
            None,
            REQUIRED_SECRET,
            API_TOKEN_HINT,
        ),
        "waldur_offering_uuid": str(offering.uuid),
    }
    secret_settings: dict[str, Any] = {}
    for secret in listed.secrets:
        path = secret["field"].removeprefix("backend_settings.")
        parts = path.split(".")
        if secret["requirement"] != site_agent_configuration.OPTIONAL and (
            _served_block(served_settings, parts)
        ):
            node = secret_settings
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            label = SECRET_REQUIREMENT_LABELS[secret["requirement"]]
            node[parts[-1]] = generated.placeholder(
                offering,
                secret["field"],
                secret["backend"],
                secret["requirement"],
                f"{secret['backend']}{label}",
            )
        else:
            generated.add_secret(
                offering, secret["field"], secret["backend"], secret["requirement"]
            )
    if secret_settings:
        offering_config["backend_settings"] = secret_settings
    return offering_config


def _served_block(settings, parts) -> bool:
    """Whether the block of a secret at these path parts is in the settings.

    A secret inside a mapping or list of entries has no single place to go.
    """
    if any(part == "*" or part.endswith("[]") for part in parts):
        return False
    node = settings
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
    return isinstance(node, dict)


def event_settings(configuration) -> dict[str, Any]:
    """The offering's event settings that are set, in the order the agent documents them.

    A null is left out like a missing key: the agent reads both as unset, and
    saving a configuration back as it was read must not change its version.
    """
    stored = configuration.get("event_settings") or {}
    return {
        name: stored[name]
        for name in site_agent_configuration.EventSettingsSerializer().fields
        if stored.get(name) is not None
    }


def agent_settings(configuration) -> dict[str, Any]:
    """The agent settings that are set, at the top level where the agent reads them."""
    stored = configuration.get("agent_settings") or {}
    return {
        name: stored[name]
        for name in site_agent_configuration.AgentSettingsSerializer().fields
        if stored.get(name) is not None
    }


def build_backend_settings(
    offering, backends, secrets, legacy, generated
) -> dict[str, Any]:
    """Build backend_settings from the offering's agent configuration.

    The settings used to live in plugin_options; an offering written there
    since the data migration (by an import of an old export, say) is still
    read from it.
    """
    configuration = offering.agent_configuration or {}
    configured = site_agent_configuration.without_secrets(
        configuration.get("backend_settings") or {}
    )
    settings = {**(offering.plugin_options or {}), **configured}

    backend_settings = {}
    # Membership sync and reporting read the SLURM settings too, so they
    # come with any role SLURM fills, not just order processing.
    uses_slurm = any(
        backends.get(field) == DEFAULT_AGENT_BACKEND for field in AGENT_ROLE_FIELDS
    )
    if uses_slurm:
        backend_settings = slurm_backend_settings(
            settings, legacy, generated.with_placeholders
        )
    for key, value in configured.items():
        backend_settings.setdefault(key, value)
    if uses_slurm:
        periodic_limits = periodic_limits_settings(
            offering, backend_settings.get(PERIODIC_LIMITS_SETTING)
        )
        if periodic_limits:
            backend_settings[PERIODIC_LIMITS_SETTING] = periodic_limits
    if not legacy:
        # What the configuration leaves out, the agent would take from its
        # plugins' defaults anyway; written out, the file says what runs.
        for key, value in site_agent_configuration.setting_defaults(
            dict.fromkeys(backends.values())
        ).items():
            backend_settings.setdefault(key, value)
    for backend, path, requirement in secrets:
        add_secret_placeholder(
            backend_settings, offering, backend, path, requirement, generated
        )
    return backend_settings


def add_secret_placeholder(settings, offering, backend, path, requirement, generated):
    """Put a placeholder where the operator has to add a secret.

    A required secret gets one where its block is configured; a block is
    never created just to hold one, as it would then lack its other
    settings and read as complete once the secret is filled in. Optional
    secrets, and those whose block is missing, are only listed. Without
    placeholders every secret is only listed.
    """
    field = f"backend_settings.{path}"
    parts = path.split(".")
    node = settings
    if (
        generated.with_placeholders
        and requirement != site_agent_configuration.OPTIONAL
        and not any(part == "*" or part.endswith("[]") for part in parts)
    ):
        for part in parts[:-1]:
            node = node.get(part)
            if not isinstance(node, dict):
                break
        else:
            if parts[-1] not in node:
                label = SECRET_REQUIREMENT_LABELS[requirement]
                node[parts[-1]] = generated.placeholder(
                    offering,
                    field,
                    backend,
                    requirement,
                    f"{backend}{label}",
                )
                return
    generated.add_secret(offering, field, backend, requirement)


def slurm_backend_settings(settings, legacy, with_placeholders=True) -> dict[str, Any]:
    """The SLURM settings every generated configuration has, with defaults.

    hostname is not a SLURM setting and the agent never reads it. An
    offering with agent roles set has it only where it set one; one without
    keeps the placeholder generated configurations always had, unless there
    are no placeholders.
    """
    backend_settings = {
        key: settings.get(key, default)
        for key, default in site_agent_configuration.SLURM_SETTING_DEFAULTS.items()
    }
    if legacy:
        if "hostname" in settings:
            backend_settings["hostname"] = settings["hostname"]
        elif with_placeholders:
            backend_settings["hostname"] = LEGACY_HOSTNAME_PLACEHOLDER
    elif settings.get("hostname"):
        backend_settings["hostname"] = settings["hostname"]
    backend_settings["enable_user_homedir_account_creation"] = settings.get(
        "enable_user_homedir_account_creation", False
    )
    return backend_settings


def build_backend_components(offering) -> dict[str, dict[str, Any]]:
    """Convert offering components to backend_components format.

    Agent-only fields from the agent configuration, such as
    unit_factor_reporting, are added to the component they name; they
    never replace the fields that come from the offering component.
    """
    agent_fields = (offering.agent_configuration or {}).get("backend_components") or {}
    components = {}
    for component in offering.components.all():
        extra = agent_fields.get(component.type)
        # The serializer refuses secrets here; data written some other way is
        # stripped of them all the same, as backend_settings is.
        extra = (
            site_agent_configuration.without_secrets(extra)
            if isinstance(extra, dict)
            else {}
        )
        components[component.type] = {
            **{
                key: value
                for key, value in extra.items()
                if key not in site_agent_configuration.OFFERING_COMPONENT_FIELDS
            },
            "measured_unit": component.measured_unit or "",
            "unit_factor": component.unit_factor or 1,
            # The agent knows usage, limit and one, the billing types
            # waldur_site_load_components maps back; the SLURM backend
            # multiplies a prepaid (one) limit by the subscription's months.
            "accounting_type": {
                BillingTypes.USAGE: "usage",
                BillingTypes.ONE_TIME: "one",
            }.get(component.billing_type, "limit"),
            "label": component.name,
        }
        # The agent's limit is the component's limit_amount, as
        # waldur_site_load_components maps it; for a usage component SLURM
        # caps every allocation at limit * unit_factor.
        if component.limit_amount is not None:
            # A Decimal would be dumped as a Python object tag.
            limit = component.limit_amount
            components[component.type]["limit"] = (
                int(limit) if limit == int(limit) else float(limit)
            )
    return components


PERIODIC_LIMITS_SETTING = "periodic_limits"


def periodic_limits_settings(offering, configured=None) -> dict[str, Any] | None:
    """Switch periodic limits on for an offering with a periodic usage policy.

    The agent applies periodic limits only when
    ``backend_settings.periodic_limits.enabled`` is set: that is what makes it
    subscribe to the periodic limits events and run apply_periodic_settings.
    The policy's parameters reach it in those events, and policy_settings is
    informational, so without this block a generated configuration leaves
    periodic limits off for every offering. limit_type is the agent's fallback
    for an event that omits it.

    A periodic_limits block of the offering's own agent configuration is kept
    as set; what it leaves out or sets to null, enabled included, comes from
    the policy. Only
    the SLURM backend applies periodic limits, so the caller adds the block
    only when SLURM fills one of the offering's roles.
    """
    policy = SlurmPeriodicUsagePolicy.objects.filter(scope=offering).first()
    if policy is None:
        return configured
    stored = configured if isinstance(configured, dict) else {}
    return {
        "enabled": True,
        "limit_type": policy.limit_type,
        # A null in the stored block means unset, as elsewhere in this builder.
        **{key: value for key, value in stored.items() if value is not None},
    }


def build_policy_settings(offering) -> dict[str, Any] | None:
    """Build policy settings from SlurmPeriodicUsagePolicy if exists."""
    try:
        policy = SlurmPeriodicUsagePolicy.objects.get(scope=offering)
        return {
            "limit_type": policy.limit_type,
            "tres_billing_enabled": policy.tres_billing_enabled,
            "tres_billing_weights": policy.tres_billing_weights or {},
            "carryover_factor": policy.carryover_factor,
            "grace_ratio": float(policy.grace_ratio),
            "carryover_enabled": policy.carryover_enabled,
            "raw_usage_reset": policy.raw_usage_reset,
            "qos_strategy": policy.qos_strategy,
        }
    except SlurmPeriodicUsagePolicy.DoesNotExist:
        return None


class _SecretList:
    """The secrets a configuration leaves out, as the operator adds them."""

    with_placeholders = True

    def __init__(self):
        self.secrets = []
        self.legacy_offerings = False

    def add_secret(
        self, offering, field, backend, requirement, in_file=False, env_var=None
    ):
        self.secrets.append(
            {
                "offering_uuid": offering.uuid,
                "offering_name": offering.name,
                "field": field,
                "backend": backend,
                "requirement": requirement,
                "in_file": in_file,
                "env_var": env_var,
            }
        )


class ListedSecrets(_SecretList):
    """No placeholders: each secret is left out of the settings and listed.

    For the configuration the agent loads itself, which keeps its secrets in
    its own local configuration.
    """

    with_placeholders = False
    secret_format = None


class GeneratedAgentConfig(_SecretList):
    """A site agent configuration being generated, and the secrets it leaves out.

    A secret's placeholder is a unique token while the configuration is built,
    so that it can be written out as YAML cannot: an empty value with a comment
    beside it, or a ${VARIABLE} reference.

    The token carries a random part made for this file, and is replaced only
    where it is a whole value. Settings, names and labels come from people who
    may edit the offering but not the agent host: text of theirs that named a
    token could otherwise put a secret into a setting the agent sends elsewhere,
    such as a URL, once the operator fills the secrets in.
    """

    COMMENT = "comment"
    ENV = "env"

    def __init__(self, secret_format, configuration_source=LOCAL_SOURCE):
        super().__init__()
        self.secret_format = secret_format
        self.configuration_source = configuration_source
        self.config = {"sentry_dsn": "", "timezone": "UTC", "offerings": []}
        self._placeholders = []
        self._env_prefixes = {}
        self._token_prefix = f"__waldur_agent_secret_{token_hex(16)}_"
        self._token = re.compile(
            rf"(?<=: ){re.escape(self._token_prefix)}(\d+)__$", re.MULTILINE
        )

    def placeholder(self, offering, field, backend, requirement, description):
        """A placeholder for a secret, recorded as one the file asks for."""
        env_var = (
            self._env_var(offering, field) if self.secret_format == self.ENV else None
        )
        self.add_secret(
            offering, field, backend, requirement, in_file=True, env_var=env_var
        )
        # Comments end at a line break: keep the description on one line.
        description = " ".join(str(description).split())
        # Single quotes keep a filled-in secret as written: unquoted, " #"
        # would end it and a leading "*" or "%" or a ": " would not load; in
        # double quotes a backslash would start an escape. Only a "'" in the
        # secret needs doubling, and fails to load rather than change.
        rendered = (
            f"'${{{env_var}}}'"
            if self.secret_format == self.ENV
            else f"''  # SECRET: {description}"
        )
        self._placeholders.append(rendered)
        return f"{self._token_prefix}{len(self._placeholders) - 1}__"

    @staticmethod
    def _env_name(text):
        name = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
        return f"_{name}" if name[:1].isdigit() else name

    def _env_var(self, offering, field):
        """HPC_CLUSTER_WALDUR_API_TOKEN, HPC_CLUSTER_LDAP_BIND_PASSWORD, ...

        Named after the offering, so a file of several offerings keeps their
        secrets apart; two offerings of one name get their UUIDs to tell them
        apart.
        """
        prefix = self._env_prefixes.get(offering.uuid)
        if prefix is None:
            prefix = self._env_name(offering.name)
            if prefix in self._env_prefixes.values():
                prefix = f"{prefix}_{offering.uuid.hex[:8].upper()}"
            self._env_prefixes[offering.uuid] = prefix
        setting = field.removeprefix("backend_settings.")
        return f"{prefix}_{self._env_name(setting)}"

    def checklist(self):
        """Header lines naming each backend secret the operator must add.

        Two lists: the secrets to add now, and those of a block the offering
        does not set, which a configuration_source: waldur agent refuses until
        Waldur serves the block.
        """
        lines, unset_block_lines = [], []
        for secret in self.secrets:
            if secret["backend"] is None:
                continue
            # Names go into '#' comment lines: a line break would end the comment.
            name = " ".join(secret["offering_name"].split())
            if self._block_unset(secret):
                unset_block_lines.append(
                    f"{name}: {secret['field']} ({secret['backend']})"
                )
                continue
            line = (
                f"{name}: {secret['field']} ({secret['backend']}"
                f"{SECRET_REQUIREMENT_LABELS[secret['requirement']]})"
            )
            if secret["env_var"]:
                line += f" as ${{{secret['env_var']}}}"
            lines.append(line)
        return lines, unset_block_lines

    @staticmethod
    def _block_unset(secret):
        """A needed secret left without a placeholder: its block is not set.

        One inside a mapping or list of entries is needed per entry, which the
        file cannot show, so it stays with the secrets to add.
        """
        parts = secret["field"].split(".")
        return (
            not secret["in_file"]
            and secret["requirement"] != site_agent_configuration.OPTIONAL
            and not any(part == "*" or part.endswith("[]") for part in parts)
        )

    def yaml_content(self):
        """The configuration as YAML, with instructional header comments."""
        hostname_hint = (
            "#   - hostname: Your SLURM cluster hostname (if shown as placeholder)\n"
            if self.legacy_offerings
            else ""
        )
        header = (
            "# Waldur Site Agent Configuration\n"
            f"# Generated: {timezone.now().isoformat()}\n"
            "#\n"
            "# IMPORTANT - SECRETS TO CONFIGURE:\n"
            "#   - waldur_api_token: a site agent token bound to the offering\n"
            "#     (offering -> Integration -> Credentials -> Actions ->\n"
            "#     Create agent token), not your own API token. A personal\n"
            "#     access token needs waldur-site-agent 1.0.8-rc.13 or later.\n"
            f"{hostname_hint}"
            "#\n"
            "# Documentation: https://docs.waldur.com/latest/admin-guide/providers/site-agent/\n"
            "#\n"
            "# Place this file at /etc/waldur/waldur-site-agent-config.yaml\n"
            "# and start the site agent service.\n"
            "\n"
        )
        if self.configuration_source == WALDUR_SOURCE:
            header += (
                "# configuration_source: waldur - the agent reads each offering's\n"
                "# roles, settings and components from Waldur when it starts, so\n"
                "# this file holds only how to reach Waldur and the secrets. Any\n"
                "# other key in an offering fails the load. Needs\n"
                "# waldur-site-agent 1.0.8-rc.16 or later: an older agent ignores\n"
                "# configuration_source without a warning.\n#\n"
            )
        if self.secret_format == self.ENV:
            header += (
                "# Secrets are '${VARIABLE}' references: fill them in before starting\n"
                "# the agent, for example with envsubst. The agent does not expand\n"
                "# variables itself. A ' in a secret must be written twice ('').\n#\n"
            )
        # A secret of a block the offering does not configure is not needed
        # (the agent would refuse it), so the file does not mention it; it is
        # needed, and listed, once the block is set in Waldur.
        checklist, _unset_block_checklist = self.checklist()
        if checklist:
            # Waldur never stores these, so the file cannot carry them; required
            # ones are present as placeholders, the others only listed.
            lines = "".join(f"#   - {line}\n" for line in checklist)
            header += (
                "# BACKEND SECRETS TO CONFIGURE (kept on the agent host only):\n"
                f"{lines}#\n"
            )

        yaml_content = yaml.dump(
            self.config, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        yaml_content = self._token.sub(
            lambda match: self._placeholders[int(match.group(1))], yaml_content
        )
        return header + yaml_content


def config_version(offering_config: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of an offering's configuration."""
    canonical = json.dumps(offering_config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def offering_agent_config(offering) -> dict[str, Any]:
    """The configuration a site agent loads for one offering, without secrets.

    ``offering`` is the offering block generate_site_agent_config writes, minus
    the bootstrap fields and every secret placeholder; ``secrets`` lists the
    backend secrets the agent keeps in its own configuration.
    """
    listed = ListedSecrets()
    offering_config = build_offering_config(offering, None, True, listed)
    return {
        "offering": offering_config,
        "secrets": [
            {
                "field": secret["field"],
                "backend": secret["backend"],
                "requirement": secret["requirement"],
            }
            for secret in listed.secrets
        ],
        "version": config_version(offering_config),
    }
