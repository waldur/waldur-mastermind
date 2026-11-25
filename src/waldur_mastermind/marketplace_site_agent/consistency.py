"""Consistency rules for site-agent offerings, across the agent's plugins.

Some rules are known only from reading the agent: SLURM drops a limit whose
component is not a TRES name, DWDI reports only components named like
``node``, and DWDI multiplies node-hours by ``unit_factor``, which SLURM reads
as TRES-minutes per unit. These checks find such mistakes before an agent
runs into them.

They check one input, the offering template document (the export_offering
format plus ``agent_configuration``), so a template, a dry run and an
existing offering (``check_offering``) get the same findings. Each finding is
``{rule, severity, field, message}``; ``field`` is a dotted path into the
document, with list items by index.

Rules apply per role: they are chosen by the backend each role names, so a
SLURM-only offering gets no DWDI rule, and a custom backend gets none. An
offering that names no role backend runs SLURM for all three, as the config
generator does (``config_builder.DEFAULT_AGENT_BACKEND``).

A path that a template parameter fills is empty in the template; rules that
read a value skip such paths, because the parameter supplies it on creation.
"""

from collections.abc import Callable, Iterator
from typing import Any

from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace_site_agent import (
    agent_configuration as site_agent_configuration,
)

ERROR = "error"
WARNING = "warning"

SLURM_BACKEND = site_agent_configuration.SLURM_BACKEND
DWDI_COMPUTE_BACKEND = "cscs-dwdi-compute"

# Same default as config_builder.DEFAULT_AGENT_BACKEND, which imports models.
DEFAULT_BACKEND = SLURM_BACKEND
ROLE_FIELDS = site_agent_configuration.ROLE_FIELDS
# The roles that run with no backend named; username management is separate.
DEFAULT_ROLES = tuple(
    role
    for role in ROLE_FIELDS.values()
    if role != site_agent_configuration.USERNAME_MANAGEMENT
)

# SLURM's TRES types (AccountingStorageTRES): those without a name, and those
# written type/name, such as gres/gpu or license/matlab. sacctmgr lists them
# in lower case.
TRES_TYPES = frozenset({"billing", "cpu", "energy", "mem", "node", "pages", "vmem"})
NAMED_TRES_TYPES = frozenset({"bb", "fs", "gres", "ic", "license"})

# The billing types SLURM turns into a limit (``accounting_type`` in the
# agent): a usage component gets one from its configured limit, a limit or
# one-time component from the resource's limits. A fixed or on-plan-switch
# component is never sent to SLURM.
SLURM_LIMITED_BILLING_TYPES = frozenset(
    {BillingTypes.USAGE, BillingTypes.LIMIT, BillingTypes.ONE_TIME}
)

# DWDI's compute backend reports a component only when its type contains this
# (it is filled from DWDI's node-hours).
DWDI_NODE_MARKER = "node"

RULE_SLURM_TRES = "slurm_component_tres"
RULE_SLURM_BILLING_TYPE = "slurm_component_billing_type"
RULE_DWDI_NODE_COMPONENT = "dwdi_node_component"
RULE_DWDI_UNIT_FACTOR_REPORTING = "dwdi_unit_factor_reporting"
RULE_CLUSTER_NAME = "slurm_cluster_name"
RULE_REQUIRED_SETTING = "required_backend_setting"
RULE_PLAN_PRICES = "plan_component_price"

RULES = (
    RULE_SLURM_TRES,
    RULE_SLURM_BILLING_TYPE,
    RULE_DWDI_NODE_COMPONENT,
    RULE_DWDI_UNIT_FACTOR_REPORTING,
    RULE_CLUSTER_NAME,
    RULE_REQUIRED_SETTING,
    RULE_PLAN_PRICES,
)

BACKEND_ID_PATH = "offering.backend_id"
SETTINGS_PATH = "agent_configuration.backend_settings"
CLUSTER_NAME_PATH = f"{SETTINGS_PATH}.cluster_name"


def finding(rule: str, severity: str, field: str, message: str) -> dict[str, str]:
    return {"rule": rule, "severity": severity, "field": field, "message": message}


def has_errors(findings: list[dict]) -> bool:
    return any(item["severity"] == ERROR for item in findings)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _sequence(value: Any) -> list:
    return value if isinstance(value, list) else []


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


class _Document:
    """What the rules read from a document, tolerant of missing parts."""

    def __init__(self, document: dict):
        self.offering = _mapping(document.get("offering"))
        self.plugin_options = _mapping(document.get("plugin_options"))
        self.configuration = _mapping(document.get("agent_configuration"))
        self.components = [
            (index, component)
            for index, component in enumerate(_sequence(document.get("components")))
            if isinstance(component, dict)
        ]
        self.plans = [
            (index, plan)
            for index, plan in enumerate(_sequence(document.get("plans")))
            if isinstance(plan, dict)
        ]
        self.targets = {
            path
            for parameter in _sequence(document.get("parameters"))
            if isinstance(parameter, dict)
            for path in _sequence(parameter.get("paths"))
            if isinstance(path, str)
        }
        self.parameter_paths = [
            set(_sequence(parameter.get("paths")))
            for parameter in _sequence(document.get("parameters"))
            if isinstance(parameter, dict)
        ]
        self.catalogue = {
            plugin["name"]: plugin for plugin in site_agent_configuration.get_plugins()
        }
        self.custom = set(_sequence(self.configuration.get("custom_backends")))
        self.roles = self._roles()
        self.settings = self._settings()
        self.backend_components = _mapping(self.configuration.get("backend_components"))

    def _roles(self) -> dict[str, str]:
        named = {
            role: self.configuration[field]
            for field, role in ROLE_FIELDS.items()
            if isinstance(self.configuration.get(field), str)
            and self.configuration[field]
        }
        if not any(role in named for role in DEFAULT_ROLES):
            named.update(dict.fromkeys(DEFAULT_ROLES, DEFAULT_BACKEND))
        return named

    def _settings(self) -> dict[str, tuple[Any, str]]:
        """Each backend setting with the path it is read from.

        Offerings set up before agent configurations keep SLURM's settings in
        plugin_options; the config generator still reads them from there when
        SLURM is the only backend, and backend_settings wins.
        """
        settings: dict[str, tuple[Any, str]] = {}
        if site_agent_configuration.takes_legacy_settings(self.configuration):
            for key in site_agent_configuration.LEGACY_PLUGIN_OPTIONS_SETTINGS:
                if key in self.plugin_options:
                    settings[key] = (
                        self.plugin_options[key],
                        f"plugin_options.{key}",
                    )
        for key, value in _mapping(self.configuration.get("backend_settings")).items():
            settings[key] = (value, f"{SETTINGS_PATH}.{key}")
        return settings

    def backend(self, role: str) -> str | None:
        """The catalogue backend that runs a role; None for a custom one."""
        name = self.roles.get(role)
        if not name or name in self.custom:
            return None
        return name

    def runs(self, backend: str, *roles: str) -> bool:
        roles = roles or tuple(ROLE_FIELDS.values())
        return any(self.backend(role) == backend for role in roles)

    def catalogue_backends(self) -> list[str]:
        names = []
        for role in ROLE_FIELDS.values():
            name = self.backend(role)
            if name and name in self.catalogue and name not in names:
                names.append(name)
        return names

    def filled_by_parameter(self, path: str) -> bool:
        return path in self.targets

    def setting(self, name: str) -> tuple[Any, str]:
        return self.settings.get(name, (None, f"{SETTINGS_PATH}.{name}"))


def _tres_parts(component_type: str) -> tuple[str, str | None]:
    kind, _, name = component_type.partition("/")
    return kind, (name or None)


def _is_tres(component_type: str) -> bool:
    kind, name = _tres_parts(component_type.lower())
    if name is None:
        return kind in TRES_TYPES
    return kind in NAMED_TRES_TYPES


def _component_type(component: dict) -> str | None:
    value = component.get("type")
    return value if isinstance(value, str) and value else None


# Rules ----------------------------------------------------------------------


def _slurm_tres(doc: _Document) -> Iterator[dict]:
    """SLURM sets limits on, and reports usage of, TRES only.

    Error: the agent drops a limit whose key is not a TRES the cluster knows
    ("Dropping unknown TRES keys from allocation limits") and reports no usage
    for it, so such a component is neither limited nor billed. Warning: a TRES
    name not in lower case, as sacctmgr lists them; it only matches where the
    agent compares names without case.

    A component mapped to other TRES (``target_components`` in its
    backend_components entry) is checked by its targets instead. Fixed and
    on-plan-switch components never reach SLURM, so any name will do.
    """
    if not doc.runs(
        SLURM_BACKEND,
        site_agent_configuration.ORDER_PROCESSING,
        site_agent_configuration.REPORTING,
    ):
        return
    for index, component in doc.components:
        component_type = _component_type(component)
        if (
            component_type is None
            or component.get("billing_type") not in SLURM_LIMITED_BILLING_TYPES
        ):
            continue
        targets = _mapping(
            _mapping(doc.backend_components.get(component_type)).get(
                "target_components"
            )
        )
        if targets:
            names = [
                (
                    name,
                    f"agent_configuration.backend_components.{component_type}."
                    f"target_components.{name}",
                )
                for name in targets
            ]
        else:
            names = [(component_type, f"components.{index}.type")]
        for name, field in names:
            if not _is_tres(name):
                yield finding(
                    RULE_SLURM_TRES,
                    ERROR,
                    field,
                    f"{name} is not a SLURM TRES name, such as cpu, mem, node or "
                    "gres/gpu. SLURM sets no limit on it and reports no usage "
                    "for it.",
                )
            elif name != name.lower():
                yield finding(
                    RULE_SLURM_TRES,
                    WARNING,
                    field,
                    f"SLURM names TRES in lower case; write {name} as {name.lower()}.",
                )


def _slurm_billing_type(doc: _Document) -> Iterator[dict]:
    """A TRES component billed fixed or on plan switch gets no SLURM limit.

    Warning: the agent sends SLURM limits only for usage, limit and one-time
    components, so jobs may use such a TRES without bound. A fixed fee for a
    TRES can be intended, so this does not block.
    """
    if not doc.runs(SLURM_BACKEND, site_agent_configuration.ORDER_PROCESSING):
        return
    for index, component in doc.components:
        component_type = _component_type(component)
        billing_type = component.get("billing_type")
        if (
            component_type is None
            or billing_type is None
            or billing_type in SLURM_LIMITED_BILLING_TYPES
            or not _is_tres(component_type)
        ):
            continue
        yield finding(
            RULE_SLURM_BILLING_TYPE,
            WARNING,
            f"components.{index}.billing_type",
            f"SLURM sets limits for usage, limit and one-time components only; "
            f"{component_type} is billed {billing_type}, so its use on the "
            "cluster is not limited.",
        )


def _dwdi_node_component(doc: _Document) -> Iterator[dict]:
    """DWDI compute reporting fills only components whose type contains node.

    Error when no component does: the backend reports nothing at all.
    Warning for a usage component without node: it never gets usage, so it is
    never billed, which may be intended for a component priced at zero.
    """
    if not doc.runs(DWDI_COMPUTE_BACKEND, site_agent_configuration.REPORTING):
        return
    typed = [
        (index, component, component_type)
        for index, component in doc.components
        if (component_type := _component_type(component))
    ]
    if not any(DWDI_NODE_MARKER in component_type for _, _, component_type in typed):
        yield finding(
            RULE_DWDI_NODE_COMPONENT,
            ERROR,
            "components",
            "DWDI compute reporting reports node-hours, into components whose "
            "type contains node; this offering has none, so no usage is reported.",
        )
        return
    for index, component, component_type in typed:
        if (
            DWDI_NODE_MARKER not in component_type
            and component.get("billing_type") == BillingTypes.USAGE
        ):
            yield finding(
                RULE_DWDI_NODE_COMPONENT,
                WARNING,
                f"components.{index}.type",
                f"DWDI compute reporting reports no usage for {component_type}: "
                "only components whose type contains node get usage.",
            )


def _dwdi_unit_factor_reporting(doc: _Document) -> Iterator[dict]:
    """A node component SLURM limits and DWDI reports needs unit_factor_reporting.

    Error: SLURM reads unit_factor as TRES-minutes per unit, while DWDI
    multiplies its node-hours by unit_factor unless unit_factor_reporting is
    set, so usage would be reported in the wrong unit, typically 60 times
    too much.
    """
    if not (
        doc.runs(SLURM_BACKEND, site_agent_configuration.ORDER_PROCESSING)
        and doc.runs(DWDI_COMPUTE_BACKEND, site_agent_configuration.REPORTING)
    ):
        return
    for _, component in doc.components:
        component_type = _component_type(component)
        if component_type is None or DWDI_NODE_MARKER not in component_type:
            continue
        field = (
            f"agent_configuration.backend_components.{component_type}."
            "unit_factor_reporting"
        )
        fields = _mapping(doc.backend_components.get(component_type))
        if fields.get("unit_factor_reporting") is None and not (
            doc.filled_by_parameter(field)
        ):
            yield finding(
                RULE_DWDI_UNIT_FACTOR_REPORTING,
                ERROR,
                field,
                f"SLURM limits {component_type} in TRES-minutes by unit_factor, "
                "and DWDI reports node-hours: set unit_factor_reporting to the "
                "units per node-hour, or DWDI multiplies its node-hours by "
                "unit_factor.",
            )


def _cluster_name(doc: _Document) -> Iterator[dict]:
    """The offering's backend ID and SLURM's cluster_name are the same name.

    The agent scopes sacctmgr by cluster_name, DWDI filters usage by the
    offering's backend ID, and diagnostics match one against the other.

    Error: they differ; the backend ID is empty while DWDI reports usage (its
    cluster filter matches nothing); cluster_name is empty in REST mode, where
    the agent refuses to start. Warning: cluster_name is empty otherwise, as
    the agent warns that sacctmgr runs unscoped; one of the two is filled by a
    parameter and the other is fixed, which a template cannot check.
    """
    if not doc.runs(SLURM_BACKEND):
        return
    backend_id = doc.offering.get("backend_id")
    cluster_name, cluster_path = doc.setting("cluster_name")
    backend_id_by_parameter = doc.filled_by_parameter(BACKEND_ID_PATH)
    cluster_by_parameter = doc.filled_by_parameter(cluster_path)
    if backend_id_by_parameter or cluster_by_parameter:
        same_parameter = any(
            {BACKEND_ID_PATH, cluster_path} <= paths for paths in doc.parameter_paths
        )
        if not same_parameter:
            yield finding(
                RULE_CLUSTER_NAME,
                WARNING,
                BACKEND_ID_PATH if not backend_id_by_parameter else cluster_path,
                "The offering's backend ID and SLURM's cluster_name must be the "
                "same name; fill both from one parameter.",
            )
        return
    rest_mode = doc.setting("execution_mode")[0] == "rest"
    if _empty(cluster_name):
        yield finding(
            RULE_CLUSTER_NAME,
            ERROR if rest_mode else WARNING,
            cluster_path,
            "The agent refuses to start in REST mode without cluster_name."
            if rest_mode
            else "Without cluster_name the agent runs sacctmgr for every cluster "
            "SLURM knows. Set it to the offering's backend ID.",
        )
    if _empty(backend_id):
        dwdi = doc.runs(DWDI_COMPUTE_BACKEND, site_agent_configuration.REPORTING)
        yield finding(
            RULE_CLUSTER_NAME,
            ERROR if dwdi else WARNING,
            BACKEND_ID_PATH,
            "DWDI reporting filters usage by the offering's backend ID, so with "
            "none it reports nothing. Set it to SLURM's cluster_name."
            if dwdi
            else "Set the offering's backend ID to SLURM's cluster_name: "
            "diagnostics and usage reporting match accounts by it.",
        )
    if (
        not _empty(cluster_name)
        and not _empty(backend_id)
        and str(cluster_name) != str(backend_id)
    ):
        yield finding(
            RULE_CLUSTER_NAME,
            ERROR,
            BACKEND_ID_PATH,
            f"The offering's backend ID {backend_id} differs from SLURM's "
            f"cluster_name {cluster_name}; they must be the same name.",
        )


def _required_settings(doc: _Document) -> Iterator[dict]:
    """Every backend's required settings, other than secrets, are set.

    Error: the agent validates its settings on start and refuses missing ones.
    Saving an agent configuration already enforces this; the rule covers
    offerings stored before that check and the legacy plugin_options
    settings. Secrets are left out, as they come from the agent's own file.
    """
    reported: set[str] = set()
    for name in doc.catalogue_backends():
        plugin = doc.catalogue[name]
        schema = _mapping(plugin.get("settings_schema"))
        secrets = {
            path.split(".")[0] for path in _sequence(plugin.get("secret_settings"))
        }
        for setting in _sequence(schema.get("required")):
            if setting in secrets or setting in reported:
                continue
            value, path = doc.setting(setting)
            if not _empty(value) or doc.filled_by_parameter(path):
                continue
            reported.add(setting)
            yield finding(
                RULE_REQUIRED_SETTING,
                ERROR,
                path,
                f"The {name} backend requires {setting}.",
            )


def _plan_prices(doc: _Document) -> Iterator[dict]:
    """Every active plan prices every component.

    Error: an offering cannot be activated without a plan, and a component a
    plan leaves out is not billed on usage and fails a limit change, which
    looks the plan's price up.
    """
    component_types = [
        component_type
        for _, component in doc.components
        if (component_type := _component_type(component))
    ]
    plans = [(index, plan) for index, plan in doc.plans if not plan.get("archived")]
    if not plans:
        yield finding(
            RULE_PLAN_PRICES,
            ERROR,
            "plans",
            "The offering has no active plan, so it cannot be activated or ordered.",
        )
        return
    for index, plan in plans:
        priced = {
            item.get("component_type")
            for item in _sequence(plan.get("components"))
            if isinstance(item, dict)
        }
        for component_type in component_types:
            if component_type not in priced:
                yield finding(
                    RULE_PLAN_PRICES,
                    ERROR,
                    f"plans.{index}.components",
                    f"Plan {plan.get('name') or index} does not price "
                    f"{component_type}.",
                )


_CHECKS: tuple[Callable[[_Document], Iterator[dict]], ...] = (
    _slurm_tres,
    _slurm_billing_type,
    _dwdi_node_component,
    _dwdi_unit_factor_reporting,
    _cluster_name,
    _required_settings,
    _plan_prices,
)


def check_document(document: dict) -> list[dict]:
    """The findings for an offering template document, errors first."""
    if not isinstance(document, dict):
        return []
    doc = _Document(document)
    findings = [item for check in _CHECKS for item in check(doc)]
    return sorted(findings, key=lambda item: item["severity"] != ERROR)


def offering_document(offering) -> dict:
    """The parts of an offering the rules read, in the template document format."""
    return {
        "offering": {"type": offering.type, "backend_id": offering.backend_id},
        "plugin_options": offering.plugin_options or {},
        "agent_configuration": offering.agent_configuration or {},
        "components": [
            {
                "type": component.type,
                "billing_type": component.billing_type,
                "unit_factor": component.unit_factor,
            }
            for component in offering.components.all()
        ],
        "plans": [
            {
                "name": plan.name,
                "archived": plan.archived,
                "components": [
                    {"component_type": plan_component.component.type}
                    for plan_component in plan.components.all()
                    if plan_component.component
                ],
            }
            for plan in offering.plans.all()
        ],
    }


def check_offering(offering) -> list[dict]:
    return check_document(offering_document(offering))
