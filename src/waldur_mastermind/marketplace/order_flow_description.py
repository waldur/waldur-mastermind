"""How orders for an offering move from placement to done.

``describe`` says, for one kind of order and one kind of orderer, which steps
an order goes through, who acts at each, which rule decides whether a review
step runs and which setting each rule reads. The review steps are decided by
walking ``order_rules`` -- the rule lists the engine routes real orders by --
over what is known about the orderer, so the description and the engine
cannot disagree. A fact that depends on the order (whether a purchase order is
attached, whether the project belongs to the provider) is left open and the
step is drawn as a branch.

The description names rules and settings, never people. ``with_stats`` adds
how many of the offering's orders are in each state and how long each step
has taken.
"""

import dataclasses
import datetime
import functools
import statistics

from django.db.models import Count
from django.utils import timezone

from waldur_core.configuration import config
from waldur_core.core.models import Feature
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.models import Role, RolePermission
from waldur_mastermind.marketplace import models, order_rules, plugins
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    REMOTE_OFFERING,
    SCRIPT_OFFERING,
    SITE_AGENT_OFFERING,
    SUPPORT_OFFERING,
    OrderStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.order_rules import Source

BOOKING_OFFERING = "Marketplace.Booking"

STATS_WINDOW_DAYS = 90
STATS_SAMPLE = 500
ORDER_STATE_NAMES = dict(OrderStates.CHOICES)


@dataclasses.dataclass(frozen=True)
class OrderKind:
    key: str
    label: str
    order_type: int
    processor: str


ORDER_KINDS = (
    OrderKind("create", "Create a resource", OrderTypes.CREATE, "create"),
    OrderKind("update_limits", "Change limits", OrderTypes.UPDATE, "update"),
    OrderKind("switch_plan", "Switch plan", OrderTypes.UPDATE, "update"),
    OrderKind("update_options", "Change resource options", OrderTypes.UPDATE, "update"),
    OrderKind("renew", "Renew a prepaid resource", OrderTypes.UPDATE, "update"),
    OrderKind("terminate", "Terminate", OrderTypes.TERMINATE, "delete"),
    OrderKind("restore", "Restore a terminated resource", OrderTypes.RESTORE, "create"),
    OrderKind(
        "auto_terminate",
        "Terminate automatically (end date or project end)",
        OrderTypes.TERMINATE,
        "delete",
    ),
)
ORDER_KINDS_MAP = {kind.key: kind for kind in ORDER_KINDS}

ORDERER_ANY = "any"
ORDERER_STAFF = "staff"
ORDERER_ROLE = "role"
ORDERER_PROVIDER_OWNER = "provider_owner"
ORDERER_PROPOSAL = "proposal_allocation"
ORDERER_SYSTEM = "system"
ORDERER_KINDS = (
    ORDERER_ANY,
    ORDERER_STAFF,
    ORDERER_ROLE,
    ORDERER_PROVIDER_OWNER,
    ORDERER_PROPOSAL,
)
ORDERER_LABELS = {
    ORDERER_ANY: "Anyone (all branches)",
    ORDERER_STAFF: "Staff",
    ORDERER_PROVIDER_OWNER: "Owner of the provider's organization",
    ORDERER_PROPOSAL: "Proposal allocation",
    ORDERER_SYSTEM: "Waldur (automatic)",
}


@dataclasses.dataclass(frozen=True)
class Orderer:
    kind: str
    role: str | None = None
    # Permissions of the role, when the orderer is described by one.
    permissions: frozenset = frozenset()

    def has(self, permission) -> bool | None:
        if self.kind == ORDERER_ANY:
            return None
        return permission.value in self.permissions


def _role_permissions(name: str) -> frozenset:
    return frozenset(
        RolePermission.objects.filter(role__name=name, role__is_active=True)
        .values_list("permission", flat=True)
        .distinct()
    )


def make_orderer(kind: str, role: str | None = None) -> Orderer:
    if kind == ORDERER_ROLE and role:
        return Orderer(kind, role, _role_permissions(role))
    if kind == ORDERER_PROVIDER_OWNER:
        owner = RoleEnum.CUSTOMER_OWNER.value
        return Orderer(kind, owner, _role_permissions(owner))
    return Orderer(kind)


# The call-management screens -- calls, proposals and the allocations they
# place -- are shown only while this feature is on.
CALL_MANAGEMENT_FEATURE = "marketplace.show_call_management_functionality"


def proposals_enabled() -> bool:
    return Feature.objects.filter(key=CALL_MANAGEMENT_FEATURE, value=True).exists()


def orderer_kinds() -> list[str]:
    """The orderers worth describing on this deployment."""
    return [
        kind
        for kind in ORDERER_KINDS
        if kind != ORDERER_PROPOSAL or proposals_enabled()
    ]


def orderer_roles(offering: models.Offering):
    """Roles someone placing an order on ``offering`` can hold: active
    project and organization roles that may create orders, and only the
    offering's restricted roles when it names some."""
    roles = Role.objects.filter(
        is_active=True,
        content_type__model__in=("customer", "project"),
        permissions__permission=PermissionEnum.CREATE_ORDER.value,
    )
    restricted = (offering.plugin_options or {}).get("restricted_to_roles") or []
    if restricted:
        roles = roles.filter(name__in=restricted)
    return roles.distinct().order_by("content_type__model", "name")


class SimulatedConsumerFacts:
    """``order_rules.CONSUMER_RULES`` facts for an orderer who has not ordered."""

    def __init__(self, offering, kind: OrderKind, orderer: Orderer):
        self.offering = offering
        self.kind = kind
        self.orderer = orderer
        self.options = offering.plugin_options or {}

    @property
    def _terminate(self):
        return self.kind.order_type == OrderTypes.TERMINATE

    @property
    def _any(self):
        return self.orderer.kind == ORDERER_ANY

    @property
    def held_for_purchase_order(self):
        if self._terminate or not self.options.get("require_purchase_order_upload"):
            return False
        # Until one is attached.
        return None

    @property
    def placed_automatically_with_review(self):
        if self._any:
            return None
        return self.orderer.kind == ORDERER_PROPOSAL

    @property
    def creator_is_staff(self):
        if self._any:
            return None
        return self.orderer.kind in (ORDERER_STAFF, ORDERER_SYSTEM)

    @property
    def autoapprove_disabled(self):
        return bool(not self._terminate and self.options.get("disable_autoapprove"))

    @property
    def offering_is_private(self):
        return self.offering.is_private

    @property
    def creator_can_approve_private_order(self):
        return self.orderer.has(PermissionEnum.APPROVE_PRIVATE_ORDER)

    @property
    def service_provider_project_autoapprove(self):
        if not (
            self.offering.shared
            and self.options.get("auto_approve_in_service_provider_projects") is True
        ):
            return False
        if self.orderer.kind == ORDERER_PROVIDER_OWNER:
            return True
        # Whether the project belongs to the provider's organization.
        return None

    @property
    def termination_by_provider_owner(self):
        if not self._terminate:
            return False
        if self.orderer.kind == ORDERER_PROVIDER_OWNER:
            return True
        if self.orderer.kind == ORDERER_PROPOSAL:
            return False
        # Whoever holds a role on the consumer's side may also own the
        # provider's organization.
        return None

    @property
    def creator_holds_auto_approve_role(self):
        roles = self.options.get("auto_approve_for_roles") or []
        if not roles:
            return False
        if self._any:
            return None
        return self.orderer.role in roles

    @property
    def creator_can_approve_order(self):
        return self.orderer.has(PermissionEnum.APPROVE_ORDER)


class SimulatedProviderFacts:
    """``order_rules.PROVIDER_RULES`` facts. The approver is the orderer when
    consumer review is skipped, and an unknown consumer approver otherwise."""

    def __init__(self, offering, orderer: Orderer, consumer_verdict: str):
        self.offering = offering
        self.offering_type = offering.type
        self.orderer = orderer
        self.orderer_approves = consumer_verdict == "skipped"
        options = offering.plugin_options or {}
        self.auto_approve_remote_orders = bool(
            options.get("auto_approve_remote_orders")
        )
        self.auto_approve_marketplace_script = options.get(
            "auto_approve_marketplace_script", True
        )

    @property
    def approver_owns_provider(self):
        # Staff count as owners of every organization.
        if self.orderer_approves and self.orderer.kind in (
            ORDERER_STAFF,
            ORDERER_PROVIDER_OWNER,
        ):
            return True
        # Anyone else -- a consumer approver, whoever accepted a proposal,
        # the user who asked for an end date that terminates the resource,
        # or someone described by a consumer role -- may or may not own the
        # provider's organization too.
        return None

    @property
    def approver_manages_offering(self):
        # A service manager of the provider who is also a user of the
        # offering: never certain from the orderer alone.
        return None


@dataclasses.dataclass(frozen=True)
class Execution:
    actor_label: str
    text: str
    # "sync": the order is done when processing returns; "callback": it waits
    # for someone or something outside Waldur to report back.
    mode: str


EXECUTION = {
    BASIC_OFFERING: Execution(
        "Waldur",
        "Nothing is provisioned automatically: the order is done as soon as "
        "the provider approves it.",
        "sync",
    ),
    SITE_AGENT_OFFERING: Execution(
        "Site agent",
        "The site agent applies the order on the provider's system and "
        "reports it done or erred.",
        "callback",
    ),
    SCRIPT_OFFERING: Execution(
        "Script",
        "Waldur runs the offering's script for this kind of order; the order "
        "is done when the script succeeds.",
        "sync",
    ),
    REMOTE_OFFERING: Execution(
        "Remote Waldur",
        "Waldur places the order on the remote Waldur and follows it there "
        "until it is done, erred or canceled.",
        "callback",
    ),
    SUPPORT_OFFERING: Execution(
        "Helpdesk",
        "A helpdesk ticket is opened; the order is done when the ticket is resolved.",
        "callback",
    ),
    BOOKING_OFFERING: Execution(
        "Provider",
        "The provider accepts the booking (done) or rejects it (canceled).",
        "callback",
    ),
}
DEFAULT_EXECUTION = Execution(
    "Backend",
    "Waldur asks the backend to do the work; the order is done when the "
    "backend reports success.",
    "callback",
)

SCRIPT_HOOKS = {
    OrderTypes.CREATE: "create",
    OrderTypes.RESTORE: "create",
    OrderTypes.UPDATE: "update",
    OrderTypes.TERMINATE: "terminate",
}


class _Describer:
    def __init__(self, offering, kind: OrderKind, orderer: Orderer, with_stats):
        self.offering = offering
        self.kind = kind
        self.orderer = orderer
        self.with_stats = with_stats
        self.options = offering.plugin_options or {}
        self.type = offering.type

    # References

    def source(self, source: Source) -> dict:
        return {
            "kind": source.kind,
            "field": source.field,
            "editable": source.editable,
            "value": self._value(source),
        }

    def _value(self, source: Source):
        if source.kind == "plugin_option":
            return self.options.get(source.field)
        if source.kind == "constance":
            return getattr(config, source.field)
        if source.kind == "offering" and source.field in ("type", "shared", "billable"):
            return getattr(self.offering, source.field)
        return None

    def sources(self, *sources: Source) -> list[dict]:
        return [self.source(s) for s in sources]

    # Order kinds

    def processor(self, kind: OrderKind):
        return plugins.manager.get_processor(
            self.type, f"{kind.processor}_resource_processor"
        )

    def kind_support(self, kind: OrderKind) -> tuple[bool, str, list[Source]]:
        type_source = Source("offering", "type")
        if not self.processor(kind):
            return (
                False,
                "This offering type does not process such orders.",
                [type_source],
            )
        if kind.key == "update_limits" and not plugins.manager.can_update_limits(
            self.type
        ):
            return False, "This offering type has no user-set limits.", [type_source]
        if kind.key == "update_options" and not self.options.get(
            "create_orders_on_resource_option_change"
        ):
            return (
                False,
                "Resource options are changed directly, without an order.",
                [Source("plugin_option", "create_orders_on_resource_option_change")],
            )
        if kind.key == "renew" and not self.has_prepaid_components:
            return (
                False,
                "Only resources with prepaid components are renewed.",
                [Source("offering", "components.is_prepaid")],
            )
        if kind.key == "restore" and not self.options.get("can_restore_resource"):
            return (
                False,
                "Restoring terminated resources is not allowed.",
                [Source("plugin_option", "can_restore_resource")],
            )
        return True, "", []

    @functools.cached_property
    def has_prepaid_components(self):
        return self.offering.components.filter(is_prepaid=True).exists()

    def order_kinds(self) -> list[dict]:
        result = []
        for kind in ORDER_KINDS:
            supported, reason, sources = self.kind_support(kind)
            result.append(
                {
                    "key": kind.key,
                    "label": kind.label,
                    "supported": supported,
                    "reason": reason,
                    "sources": self.sources(*sources),
                }
            )
        return result

    # Checks made when the order is placed

    def checks(self) -> list[dict]:
        kind = self.kind
        create = kind.order_type == OrderTypes.CREATE
        terminate = kind.order_type == OrderTypes.TERMINATE
        result = []

        def add(key, text, *sources, active: object = True):
            result.append(
                {
                    "key": key,
                    "text": text,
                    "active": bool(active),
                    "sources": self.sources(*sources),
                }
            )

        if create:
            add(
                "offering_active",
                "New resources can be ordered only while the offering is active.",
                Source("offering", "state"),
            )
        elif not terminate:
            add(
                "offering_active_or_paused",
                "Changes can be ordered while the offering is active or paused.",
                Source("offering", "state"),
            )
        if not terminate and kind.key != "auto_terminate":
            add(
                "project_not_expired",
                "A project past its end date can only terminate resources.",
                Source("project", "end_date"),
            )
        add(
            "restricted_to_roles",
            "Only holders of the listed roles can see and order the offering.",
            Source("plugin_option", "restricted_to_roles"),
            active=self.options.get("restricted_to_roles"),
        )
        if create:
            add(
                "organization_groups",
                "Only organizations in the offering's access policies can order it.",
                Source("offering", "organization_groups"),
                active=self.offering.organization_groups.exists(),
            )
            for key, text in (
                (
                    "minimal_team_count_for_provisioning",
                    "The project needs at least this many members.",
                ),
                (
                    "required_team_role_for_provisioning",
                    "Someone in the project must hold this role.",
                ),
                (
                    "maximal_resource_count_per_project",
                    "A project may hold at most this many resources of the offering.",
                ),
                (
                    "unique_resource_per_attribute",
                    "A project may hold one resource per value of this attribute.",
                ),
                (
                    "is_resource_termination_date_required",
                    "Every resource gets a termination date.",
                ),
                (
                    "max_resource_termination_offset_in_days",
                    "The termination date may be at most this many days ahead.",
                ),
                (
                    "latest_date_for_resource_termination",
                    "No resource may run past this date.",
                ),
            ):
                add(
                    key,
                    text,
                    Source("plugin_option", key),
                    active=self.options.get(key),
                )
            add(
                "user_consent",
                "The orderer must accept the offering's terms of service.",
                Source("constance", "ENFORCE_USER_CONSENT_FOR_OFFERINGS"),
                active=config.ENFORCE_USER_CONSENT_FOR_OFFERINGS,
            )
        if kind.key == "update_limits":
            add(
                "limit_increase_only",
                "Consumers can raise, not lower, components marked increase-only.",
                Source("offering", "components.limit_increase_only"),
                active=self.offering.components.filter(
                    limit_increase_only=True
                ).exists(),
            )
        return result

    # Steps

    def rule_outcomes(self, simulation: order_rules.Simulation) -> list[dict]:
        return [
            {
                "key": outcome.rule.key,
                "text": outcome.rule.text,
                "holds": outcome.holds,
                "skips": outcome.skips,
                "reached": outcome.reached,
                "sources": self.sources(*outcome.rule.sources),
            }
            for outcome in simulation.outcomes
        ]

    def consumer_step(self, consumer_facts) -> tuple[dict, str]:
        if self.kind.key == "auto_terminate":
            return (
                self.step(
                    "consumer_review",
                    "Consumer review",
                    "consumer",
                    "skipped",
                    "Waldur places and approves automatic terminations itself.",
                    sources=[Source("engine", "automatic_termination")],
                ),
                "skipped",
            )
        simulation = order_rules.simulate(order_rules.CONSUMER_RULES, consumer_facts)
        rules = self.rule_outcomes(simulation)
        verdict = simulation.verdict
        if (
            verdict != "skipped"
            and consumer_facts.held_for_purchase_order is not True
            and not consumer_facts.autoapprove_disabled
        ):
            # Placed right after the order, outside the rule list: a project's
            # own auto-approval rule may clear the step.
            rules.append(
                {
                    "key": "project_auto_approval",
                    "text": "The project's auto-approval rule approves orders "
                    "under its monthly cost limit, unless the plan has a "
                    "usage-based component.",
                    "holds": None,
                    "skips": True,
                    "reached": True,
                    "sources": self.sources(Source("project", "order_auto_approval")),
                }
            )
            verdict = "depends"
        status = {"skipped": "skipped", "required": "runs", "depends": "conditional"}
        return (
            self.step(
                "consumer_review",
                "Consumer review",
                "consumer",
                status[verdict],
                "Someone who may approve orders in the project approves or "
                "rejects the order.",
                rules=rules,
                can_reject=True,
                notes=self.approval_notes(),
            ),
            verdict,
        )

    def approval_notes(self):
        notes = []
        if config.NOTIFY_STAFF_ABOUT_APPROVALS:
            notes.append("Staff are notified about orders awaiting approval.")
        return notes

    def provider_step(self, consumer_verdict) -> dict:
        facts = SimulatedProviderFacts(self.offering, self.orderer, consumer_verdict)
        simulation = order_rules.simulate(order_rules.PROVIDER_RULES, facts)
        status = {"skipped": "skipped", "required": "runs", "depends": "conditional"}
        notes = []
        if self.type == SITE_AGENT_OFFERING:
            notes.append("The site agent usually approves as offering manager.")
        if self.options.get("enable_provider_consumer_messaging"):
            notes.append(
                "Provider and consumer can exchange messages on the pending order."
            )
        if self.type == SUPPORT_OFFERING:
            notes.append(
                "Not reviewed here, but a person at the provider fulfills the "
                "order through its helpdesk ticket."
            )
        return self.step(
            "provider_review",
            "Provider review",
            "provider",
            status[simulation.verdict],
            "Someone at the provider approves or rejects the order.",
            rules=self.rule_outcomes(simulation),
            can_reject=True,
            notes=notes,
        )

    def project_start_step(self) -> dict:
        return self.step(
            "project_start",
            "Waiting for the project to start",
            "scheduler",
            "conditional",
            "An order in a project that has not started yet waits until it does.",
            sources=[Source("project", "start_date")],
        )

    def start_date_step(self) -> dict:
        enabled = config.ENABLE_ORDER_START_DATE
        sources = [Source("constance", "ENABLE_ORDER_START_DATE")]
        if not enabled:
            return self.step(
                "start_date",
                "Waiting for the order's start date",
                "scheduler",
                "skipped",
                "Orders cannot ask for a start date on this deployment.",
                sources=sources,
            )
        if self.kind.order_type != OrderTypes.CREATE:
            return self.step(
                "start_date",
                "Waiting for the order's start date",
                "scheduler",
                "skipped",
                "Only orders for new resources carry a start date.",
                sources=sources,
            )
        return self.step(
            "start_date",
            "Waiting for the order's start date",
            "scheduler",
            "conditional",
            "An order placed with a start date in the future waits for it.",
            sources=[*sources, Source("order", "start_date")],
        )

    def execution(self) -> Execution:
        return EXECUTION.get(self.type, DEFAULT_EXECUTION)

    def execution_step(self) -> dict:
        execution = self.execution()
        sources = [Source("offering", "type")]
        notes = []
        if self.type == SCRIPT_OFFERING:
            sources.append(
                Source(
                    "offering", f"secret_options.{SCRIPT_HOOKS[self.kind.order_type]}"
                )
            )
        if self.is_interruptible:
            notes.append("An order still executing after 2 hours is marked erred.")
        if self.can_retry:
            notes.append("The provider can retry an erred order.")
        return self.step(
            "execution",
            "Execution",
            "backend",
            "runs",
            execution.text,
            sources=sources,
            notes=notes,
            mode=execution.mode,
        )

    @property
    def is_interruptible(self):
        return self.type in plugins.manager.list_interruptible_offerings()

    @property
    def can_retry(self):
        return plugins.manager.supports_order_retry(self.type)

    @property
    def can_cancel(self):
        return plugins.manager.can_cancel_order(self.type)

    def step(
        self,
        key,
        label,
        actor,
        status,
        summary,
        rules=None,
        sources=None,
        notes=None,
        can_reject=False,
        mode=None,
    ) -> dict:
        return {
            "key": key,
            "label": label,
            "actor": actor,
            "status": status,
            "summary": summary,
            "rules": rules or [],
            "sources": self.sources(*(sources or [])),
            "notes": notes or [],
            "can_reject": can_reject and status != "skipped",
            "mode": mode,
        }

    def terminals(self, steps) -> list[dict]:
        reached = [s["key"] for s in steps if s["status"] != "skipped"]
        rejected_from = [
            s["key"] for s in steps if s["can_reject"] and s["status"] != "skipped"
        ]
        result = []
        if rejected_from:
            result.append(
                {"key": "rejected", "from_steps": rejected_from, "retry": False}
            )
        canceled_from = []
        if self.can_cancel:
            canceled_from = [
                key
                for key in reached
                if key
                in ("consumer_review", "provider_review", "start_date", "execution")
            ]
        if self.type == BOOKING_OFFERING:
            canceled_from = sorted(set(canceled_from) | {"execution"})
        if canceled_from:
            result.append(
                {"key": "canceled", "from_steps": canceled_from, "retry": False}
            )
        result.append(
            {"key": "erred", "from_steps": ["execution"], "retry": self.can_retry}
        )
        return result

    def actors(self) -> list[dict]:
        return [
            {"key": "consumer", "label": "Consumer"},
            {"key": "provider", "label": "Service provider"},
            {"key": "scheduler", "label": "Waldur scheduler"},
            {"key": "backend", "label": self.execution().actor_label},
        ]

    def orderers(self) -> list[dict]:
        result = [
            {"kind": kind, "role": None, "label": ORDERER_LABELS[kind]}
            for kind in orderer_kinds()
            if kind != ORDERER_ROLE
        ]
        for role in orderer_roles(self.offering):
            result.append(
                {
                    "kind": ORDERER_ROLE,
                    "role": role.name,
                    "label": role.description or role.name,
                }
            )
        return result

    # Statistics

    def stats(self) -> dict:
        orders = models.Order.objects.filter(
            offering=self.offering, type=self.kind.order_type
        )
        states = [
            {"state": ORDER_STATE_NAMES[row["state"]], "count": row["count"]}
            for row in orders.values("state").annotate(count=Count("id")).order_by()
        ]
        since = timezone.now() - datetime.timedelta(days=STATS_WINDOW_DAYS)
        recent = list(
            orders.filter(created__gte=since)
            .order_by("-created")
            .values(
                "created",
                "consumer_reviewed_at",
                "provider_reviewed_at",
                "completed_at",
                "state",
            )[:STATS_SAMPLE]
        )

        def durations(pairs):
            return [
                (end - start).total_seconds()
                for start, end in pairs
                if start and end and end >= start
            ]

        spans = {
            "consumer_review": durations(
                (o["created"], o["consumer_reviewed_at"]) for o in recent
            ),
            "provider_review": durations(
                (o["consumer_reviewed_at"], o["provider_reviewed_at"]) for o in recent
            ),
            "execution": durations(
                (
                    o["provider_reviewed_at"] or o["consumer_reviewed_at"],
                    o["completed_at"],
                )
                for o in recent
                if o["state"] == OrderStates.DONE
            ),
        }
        return {
            "window_days": STATS_WINDOW_DAYS,
            "states": states,
            "steps": [
                {
                    "step": step,
                    "samples": len(values),
                    "median_seconds": statistics.median(values) if values else None,
                }
                for step, values in spans.items()
            ],
        }

    def describe(self) -> dict:
        supported, reason, _ = self.kind_support(self.kind)
        orderer = self.orderer
        if self.kind.key == "auto_terminate":
            orderer = Orderer(ORDERER_SYSTEM)
            self.orderer = orderer
        consumer_facts = SimulatedConsumerFacts(self.offering, self.kind, orderer)
        consumer, verdict = self.consumer_step(consumer_facts)
        steps = [
            self.step(
                "placed",
                "Order placed",
                "consumer",
                "runs",
                "The order is checked against the offering's rules when placed.",
            ),
            consumer,
            self.project_start_step(),
            self.provider_step(verdict),
            self.start_date_step(),
            self.execution_step(),
            self.step("done", "Done", "backend", "runs", "The order is done."),
        ]
        return {
            "offering": {
                "uuid": self.offering.uuid.hex,
                "name": self.offering.name,
                "type": self.type,
            },
            "order_kind": self.kind.key,
            "supported": supported,
            "reason": reason,
            "orderer": {
                "kind": orderer.kind,
                "role": orderer.role,
                "label": ORDERER_LABELS.get(orderer.kind) or orderer.role or "",
            },
            "order_kinds": self.order_kinds(),
            "orderers": self.orderers(),
            "actors": self.actors(),
            "checks": self.checks(),
            "steps": steps,
            "terminals": self.terminals(steps),
            "stats": self.stats() if self.with_stats else None,
        }


def describe(
    offering: models.Offering,
    order_kind: str = "create",
    orderer_kind: str = ORDERER_ANY,
    role: str | None = None,
    with_stats: bool = False,
) -> dict:
    """The order flow of ``offering`` for one order kind and orderer."""
    kind = ORDER_KINDS_MAP[order_kind]
    orderer = make_orderer(orderer_kind, role)
    return _Describer(offering, kind, orderer, with_stats).describe()
