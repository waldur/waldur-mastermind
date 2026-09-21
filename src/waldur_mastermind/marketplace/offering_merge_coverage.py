"""Coverage registry for the offering merge engine.

Every model field that references an ``Offering``, ``Plan``, ``OfferingComponent``
or ``PlanComponent`` — by foreign key, many-to-many, generic foreign key, stored
UUID, or component types used as JSON keys — has an entry here saying what a
merge does with it. ``find_uncovered_relations`` walks the relations of the four
models across all installed apps and reports any foreign key or many-to-many the
registry does not list; a unit test fails on it, so a new relation cannot be
added without deciding its merge strategy.

Strategies:

* ``repoint`` — the rows follow the resources to the target.
* ``repoint_dedupe`` — as ``repoint``, but the field is part of a unique set; a
  row whose repointed key already exists on the target stays on the source.
* ``rewrite_snapshot`` — the value is a historical snapshot (invoice items);
  it is rewritten according to the merge's invoice policy.
* ``recompute`` — a derived table; its rows are rebuilt from the moved data
  after the merge (and after undo) instead of being repointed or journalled.
* ``keep_on_source`` — configuration or history that describes the source
  offering itself; it stays with the archived source.
* ``not_applicable`` — the relation cannot be affected by a merge.

Every entry also declares the ``area`` of the service it belongs to. The
strategy says what a merge does with a row and the area says what the row is
about, so the two together turn the registry's internal labels into something
staff can read: "Billing history — 12 rows moved" rather than
``marketplace.ComponentUsage.component: 12``. Both the preview payload and the
drill-down endpoint report them, and the frontend groups by area.
"""

from dataclasses import dataclass

from django.apps import apps
from django.db import models

REPOINT = "repoint"
REPOINT_DEDUPE = "repoint_dedupe"
REWRITE_SNAPSHOT = "rewrite_snapshot"
RECOMPUTE = "recompute"
KEEP_ON_SOURCE = "keep_on_source"
NOT_APPLICABLE = "not_applicable"

# Areas of the service a covered row belongs to. Shared with the frontend
# through the API, so the values are stable identifiers and the titles are what
# staff read.
AREA_RESOURCES_AND_ORDERS = "resources_and_orders"
AREA_BILLING_HISTORY = "billing_history"
AREA_INVOICES = "invoices"
AREA_ACCOUNTS_AND_ACCESS = "accounts_and_access"
AREA_OFFERING_CONFIGURATION = "offering_configuration"

AREA_TITLES: dict[str, str] = {
    AREA_RESOURCES_AND_ORDERS: "Resources and orders",
    AREA_BILLING_HISTORY: "Billing history",
    AREA_INVOICES: "Invoices",
    AREA_ACCOUNTS_AND_ACCESS: "Accounts and access",
    AREA_OFFERING_CONFIGURATION: "Offering configuration",
}

AREAS = tuple(AREA_TITLES)

# What a merge does to a covered row, derived from its strategy and kind.
EFFECT_MOVED = "moved"
EFFECT_REWRITTEN = "rewritten"
EFFECT_RECOMPUTED = "recomputed"
EFFECT_DEDUPLICATED = "deduplicated"
EFFECT_KEPT_ON_SOURCE = "kept_on_source"

EFFECT_TITLES: dict[str, str] = {
    EFFECT_MOVED: "Moved to the target",
    EFFECT_REWRITTEN: "Rewritten in place",
    EFFECT_RECOMPUTED: "Recomputed after the merge",
    EFFECT_DEDUPLICATED: "Moved unless the target has it already",
    EFFECT_KEPT_ON_SOURCE: "Kept on the archived source",
}

EFFECTS = tuple(EFFECT_TITLES)

STRATEGIES = (
    REPOINT,
    REPOINT_DEDUPE,
    REWRITE_SNAPSHOT,
    RECOMPUTE,
    KEEP_ON_SOURCE,
    NOT_APPLICABLE,
)

# How the entry references the merged objects.
FK = "fk"  # ForeignKey / OneToOneField
M2M = "m2m"  # ManyToManyField with an auto-created through table
GENERIC = "generic"  # GenericForeignKey (content type + object id)
UUID = "uuid"  # a UUIDField holding the offering's uuid
JSON_KEYS = "json_keys"  # JSON document whose keys are renamed
SNAPSHOT = "snapshot"  # JSON document holding a copy of offering data

# Execution phases: history first, then resources and orders, then the rest.
HISTORY = 1
RESOURCES = 2
REST = 3

# JSON_KEYS renames.
COMPONENT_TYPES = "component_types"
ANSWER_KEYS = "answer_keys"

MERGED_MODELS = ("Offering", "Plan", "OfferingComponent", "PlanComponent")


@dataclass(frozen=True)
class CoverageEntry:
    label: str  # "<app_label>.<Model>.<field>"
    strategy: str
    reason: str
    # Which part of the service the rows belong to. Declared, not derived: the
    # model label is no guide (``marketplace.Resource.limits`` is about
    # resources, ``marketplace.PosixIdPool.offering`` about access).
    area: str = ""
    kind: str = FK
    phase: int = REST
    # Other fields of the unique set the referencing field belongs to.
    unique_with: tuple[str, ...] = ()
    # For REPOINT entries with unique_with: blocker code raised on collision.
    collision_blocker: str = ""
    # GENERIC: names of the content type and object id fields.
    ct_field: str = "content_type"
    id_field: str = "object_id"
    # JSON_KEYS / SNAPSHOT: lookup path from the row to its offering.
    offering_path: str = ""
    # JSON_KEYS: which rename applies.
    rename: str = ""
    # JSON_KEYS: path from the row to the marketplace resource it belongs to
    # ("pk" for Resource itself); undo uses it to find rows created after the
    # merge. Empty when rows are not tied to a resource.
    resource_path: str = ""
    # JSON_KEYS: path to a plan that must be mapped for the row to be renamed.
    mapped_plan_path: str = ""

    @property
    def model_label(self) -> str:
        return self.label.rsplit(".", 1)[0]

    @property
    def field_name(self) -> str:
        return self.label.rsplit(".", 1)[1]

    @property
    def model(self) -> type[models.Model]:
        return apps.get_model(self.model_label)

    @property
    def area_title(self) -> str:
        return AREA_TITLES[self.area]

    @property
    def effect(self) -> str:
        """What a merge does to the rows, as one word for the UI.

        A JSON document is never repointed: the row stays where it is and its
        keys are renamed, so a ``repoint`` of a JSON field is a rewrite.

        A ``not_applicable`` entry has no effect to report — a merge does
        nothing to it, and it is left out of the preview entirely — so asking
        for one is a mistake rather than an empty answer.
        """
        if self.strategy == REPOINT:
            return EFFECT_REWRITTEN if self.kind == JSON_KEYS else EFFECT_MOVED
        effect = {
            REPOINT_DEDUPE: EFFECT_DEDUPLICATED,
            REWRITE_SNAPSHOT: EFFECT_REWRITTEN,
            RECOMPUTE: EFFECT_RECOMPUTED,
            KEEP_ON_SOURCE: EFFECT_KEPT_ON_SOURCE,
        }.get(self.strategy)
        if effect is None:
            raise ValueError(f"{self.label} is {self.strategy}; it has no effect.")
        return effect

    @property
    def effect_title(self) -> str:
        return EFFECT_TITLES[self.effect]

    @property
    def can_list_rows(self) -> bool:
        """Whether the affected rows of this entry can be listed one by one.

        Recomputed summaries cannot: the count is a projection over components
        and months, not a set of rows that exist yet. Relations a merge cannot
        touch have nothing to list.
        """
        return self.strategy not in (RECOMPUTE, NOT_APPLICABLE)


def _entries(*entries: CoverageEntry) -> dict[str, CoverageEntry]:
    result = {}
    for entry in entries:
        if entry.label in result:
            raise ValueError(f"Duplicate coverage entry {entry.label}")
        if entry.strategy not in STRATEGIES:
            raise ValueError(f"Unknown strategy {entry.strategy} for {entry.label}")
        if entry.area not in AREAS:
            raise ValueError(f"Unknown area {entry.area!r} for {entry.label}")
        result[entry.label] = entry
    return result


E = CoverageEntry

# Order matters: the executor writes in registry order within each phase.
MERGE_COVERAGE: dict[str, CoverageEntry] = _entries(
    # --- Phase 1: billing and usage history --------------------------------
    E(
        "marketplace.ResourcePlanPeriod.plan",
        REPOINT,
        "Billing periods follow the resource; plan is not nullable, so every "
        "source plan needs a mapping.",
        phase=HISTORY,
        area=AREA_BILLING_HISTORY,
    ),
    E(
        "marketplace.ComponentUsage.component",
        REPOINT,
        "Usage history follows the resource. Collisions on the unique sets are "
        "impossible because the component mapping is injective per source.",
        phase=HISTORY,
        unique_with=("resource", "plan_period", "billing_period"),
        collision_blocker="component_usage_collision",
        area=AREA_BILLING_HISTORY,
    ),
    E(
        "marketplace.ComponentUsageMonthly.component",
        RECOMPUTE,
        "Per-component monthly summary derived from usages, limits and invoice "
        "items; rebuilt for the affected source and target components and "
        "months after the merge and after undo.",
        phase=HISTORY,
        area=AREA_BILLING_HISTORY,
    ),
    E(
        "marketplace.ComponentQuota.component",
        REPOINT_DEDUPE,
        "Quotas follow the resource; unique per (resource, component).",
        phase=HISTORY,
        unique_with=("resource",),
        area=AREA_BILLING_HISTORY,
    ),
    E(
        "marketplace.ComponentUsagePollRecord.component",
        REPOINT_DEDUPE,
        "Poll accumulation state follows the resource; unique per "
        "(resource, component).",
        phase=HISTORY,
        unique_with=("resource",),
        area=AREA_BILLING_HISTORY,
    ),
    E(
        "marketplace.ComponentUserUsageLimit.component",
        REPOINT_DEDUPE,
        "Per-user limits follow the resource; unique per (resource, component, user).",
        phase=HISTORY,
        unique_with=("resource", "user"),
        area=AREA_BILLING_HISTORY,
    ),
    # --- Phase 2: resources and orders --------------------------------------
    E(
        "marketplace.Resource.offering",
        REPOINT,
        "The point of the merge.",
        phase=RESOURCES,
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.Resource.plan",
        REPOINT,
        "Via plan_mapping, written without signals so no plan period is closed "
        "or opened and no invoice item is touched.",
        phase=RESOURCES,
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.Resource.limits",
        REPOINT,
        "Keyed by component type; renamed via component_mapping.",
        kind=JSON_KEYS,
        phase=RESOURCES,
        offering_path="offering",
        rename=COMPONENT_TYPES,
        resource_path="pk",
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.Resource.current_usages",
        REPOINT,
        "Keyed by component type; renamed via component_mapping.",
        kind=JSON_KEYS,
        phase=RESOURCES,
        offering_path="offering",
        rename=COMPONENT_TYPES,
        resource_path="pk",
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.Resource.attributes",
        REPOINT,
        "Order answers; keys renamed via attribute_key_mapping.",
        kind=JSON_KEYS,
        phase=RESOURCES,
        offering_path="offering",
        rename=ANSWER_KEYS,
        resource_path="pk",
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.Order.offering",
        REPOINT,
        "Order history follows the resources.",
        phase=RESOURCES,
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.Order.plan",
        REPOINT,
        "Via plan_mapping.",
        phase=RESOURCES,
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.Order.old_plan",
        REPOINT,
        "Via plan_mapping.",
        phase=RESOURCES,
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.Order.limits",
        REPOINT,
        "Keyed by component type; renamed via component_mapping.",
        kind=JSON_KEYS,
        phase=RESOURCES,
        offering_path="offering",
        rename=COMPONENT_TYPES,
        resource_path="resource",
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.Order.attributes",
        REPOINT,
        "Answers renamed via attribute_key_mapping; the component-type keys of "
        "old_limits via component_mapping.",
        kind=JSON_KEYS,
        phase=RESOURCES,
        offering_path="offering",
        rename=ANSWER_KEYS,
        resource_path="resource",
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    # --- Phase 3: everything else -------------------------------------------
    E(
        "marketplace.ResourceProject.limits",
        REPOINT,
        "Same format as Resource.limits.",
        kind=JSON_KEYS,
        offering_path="resource__offering",
        rename=COMPONENT_TYPES,
        resource_path="resource",
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.ResourceProject.current_usages",
        REPOINT,
        "Same format as Resource.current_usages.",
        kind=JSON_KEYS,
        offering_path="resource__offering",
        rename=COMPONENT_TYPES,
        resource_path="resource",
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.ResourceLimitChangeRequest.requested_limits",
        REPOINT,
        "Keyed by component type; renamed via component_mapping.",
        kind=JSON_KEYS,
        offering_path="resource__offering",
        rename=COMPONENT_TYPES,
        resource_path="resource",
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "marketplace.OfferingUser.offering",
        REPOINT_DEDUPE,
        "Accounts follow the resources' users; a user who already has an "
        "account on the target keeps it and the source account stays behind "
        "(warned).",
        unique_with=("user",),
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "marketplace.OfferingUserGroup.offering",
        REPOINT,
        "Project-mapped backend groups belong to the moved projects' resources.",
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "marketplace.OfferingRoleGroup.offering",
        REPOINT_DEDUPE,
        "LDAP groups of resource-scoped roles; their scopes are the moved "
        "resources, so the target must keep serving them.",
        unique_with=("content_type", "object_id", "role"),
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "support.Issue.offering",
        REPOINT,
        "Tickets follow the resources they were raised for; creation issues of "
        "Support resources must still resolve to their offering.",
        area=AREA_RESOURCES_AND_ORDERS,
    ),
    E(
        "invoices.CustomerCredit.offerings",
        REPOINT_DEDUPE,
        "The customer's credit keeps covering the moved resources. It widens "
        "the credit to the target only for that one customer.",
        kind=M2M,
        area=AREA_INVOICES,
    ),
    E(
        "policy.CustomerUsagePolicyComponent.component",
        REPOINT_DEDUPE,
        "A customer's own usage cap keeps applying to its moved resources.",
        unique_with=("policy", "period"),
        area=AREA_BILLING_HISTORY,
    ),
    E(
        "proposal.RequestedOffering.offering",
        REPOINT,
        "Calls keep offering the service; the archived source cannot be ordered from.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "proposal.RequestedOffering.plan",
        REPOINT,
        "Via plan_mapping, together with RequestedOffering.offering.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "waldur_autoprovisioning.Rule.plan",
        REPOINT,
        "Rules keep provisioning the merged service rather than an archived plan.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "waldur_autoprovisioning.Rule.plan_limits",
        REPOINT,
        "Keyed by component type; renamed via component_mapping.",
        kind=JSON_KEYS,
        offering_path="plan__offering",
        rename=COMPONENT_TYPES,
        mapped_plan_path="plan",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "waldur_openportal.ProjectTemplate.offerings",
        REPOINT_DEDUPE,
        "Templates keep listing the service, not its archived source.",
        kind=M2M,
        area=AREA_OFFERING_CONFIGURATION,
    ),
    # Invoices: historical snapshots, rewritten per the merge's invoice policy.
    E(
        "invoices.InvoiceItem.plan_component",
        REWRITE_SNAPSHOT,
        "Invoice history; the invoice policy decides which months are rewritten.",
        area=AREA_INVOICES,
    ),
    E(
        "invoices.InvoiceItem.details",
        REWRITE_SNAPSHOT,
        "Snapshot of offering, plan and component names and uuids; rewritten "
        "per invoice policy.",
        kind=SNAPSHOT,
        offering_path="resource__offering",
        area=AREA_INVOICES,
    ),
    # Offering structure: stays with the archived source.
    E(
        "marketplace.Plan.offering",
        KEEP_ON_SOURCE,
        "Source plans stay; their users are repointed via plan_mapping.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.OfferingComponent.offering",
        KEEP_ON_SOURCE,
        "Source components stay; their users are repointed via component_mapping.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.OfferingComponent.overage_component",
        KEEP_ON_SOURCE,
        "Links two components of the same source offering.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.PlanComponent.plan",
        KEEP_ON_SOURCE,
        "Price list of a source plan.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.PlanComponent.component",
        KEEP_ON_SOURCE,
        "Price list of a source plan.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    # Offering configuration: describes the source service, the target has its own.
    E(
        "marketplace.UserOfferingConsent.offering",
        KEEP_ON_SOURCE,
        "Consent was given to the source's terms, not the target's; the "
        "target asks its users for consent to its own terms.",
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "marketplace.OfferingTermsOfService.offering",
        KEEP_ON_SOURCE,
        "The target's terms of service apply after the merge.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.OfferingUserAttributeConfig.offering",
        KEEP_ON_SOURCE,
        "Offering configuration; the target has its own.",
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "marketplace.Screenshot.offering",
        KEEP_ON_SOURCE,
        "Marketing content of the source.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.OfferingFile.offering",
        KEEP_ON_SOURCE,
        "Documents of the source.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.OfferingAccessEndpoint.offering",
        KEEP_ON_SOURCE,
        "Endpoints of the source's backend.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.AccessSubnetOfferingScope.offering",
        KEEP_ON_SOURCE,
        "Access policy is the target's own.",
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "marketplace.OfferingAccessSubnet.offering",
        KEEP_ON_SOURCE,
        "Access policy is the target's own.",
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "marketplace.PosixIdPool.offering",
        KEEP_ON_SOURCE,
        "The source's own POSIX id range; the target resolves its own pool.",
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "marketplace.PosixIdentity.offering",
        NOT_APPLICABLE,
        "Audit only: the offering that first triggered the allocation.",
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "marketplace.OfferingSoftwareCatalog.offering",
        KEEP_ON_SOURCE,
        "Software available on the source's backend.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.OfferingPartition.offering",
        KEEP_ON_SOURCE,
        "Partitions of the source's backend.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.SlurmOfferingQoS.offering",
        KEEP_ON_SOURCE,
        "QoS of the source's backend.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.IntegrationStatus.offering",
        KEEP_ON_SOURCE,
        "Health of agents integrated with the source.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.BackendResource.offering",
        KEEP_ON_SOURCE,
        "Discovered but unimported resources of the source's backend.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.BackendResourceRequest.offering",
        KEEP_ON_SOURCE,
        "Discovery requests against the source's backend.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.MaintenanceAnnouncementOffering.offering",
        KEEP_ON_SOURCE,
        "Announcements describe the source service; staff announce anew for "
        "the target.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.MaintenanceAnnouncementOfferingTemplate.offering",
        KEEP_ON_SOURCE,
        "Announcement templates of the source service.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace_site_agent.AgentIdentity.offering",
        KEEP_ON_SOURCE,
        "An agent serves the source's backend; the target has its own identities.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "google.GoogleCalendar.offering",
        KEEP_ON_SOURCE,
        "The source's booking calendar.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "booking.BusySlot.offering",
        KEEP_ON_SOURCE,
        "Busy slots of the source's calendar.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "waldur_arrow.ArrowVendorOfferingMapping.offering",
        KEEP_ON_SOURCE,
        "Vendor mapping configuration; staff remap the vendor explicitly.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "waldur_arrow.ArrowVendorOfferingMapping.plan",
        KEEP_ON_SOURCE,
        "Vendor mapping configuration; staff remap the vendor explicitly.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace_script.DryRun.order_offering",
        KEEP_ON_SOURCE,
        "Dry-run log of the source's scripts.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace_script.DryRun.order_plan",
        KEEP_ON_SOURCE,
        "Dry-run log of the source's scripts.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "policy.OfferingEstimatedCostPolicy.scope",
        KEEP_ON_SOURCE,
        "Moving the source's policy would impose it on the target's customers.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "policy.OfferingUsagePolicy.scope",
        KEEP_ON_SOURCE,
        "Moving the source's policy would impose it on the target's customers. "
        "Also covers SlurmPeriodicUsagePolicy (multi-table child).",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "policy.OfferingComponentLimit.component",
        KEEP_ON_SOURCE,
        "Belongs to a source OfferingUsagePolicy, which stays; its save "
        "validates the component offering.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "promotions.Campaign.offerings",
        KEEP_ON_SOURCE,
        "Repointing would extend a discount to every customer of the target.",
        kind=M2M,
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "promotions.Campaign.required_offerings",
        KEEP_ON_SOURCE,
        "Campaign eligibility is the provider's decision, not the merge's.",
        kind=M2M,
        area=AREA_OFFERING_CONFIGURATION,
    ),
    # Relations that cannot be involved in an allowed merge.
    E(
        "marketplace.Offering.parent",
        NOT_APPLICABLE,
        "Offerings with children are refused by the preview, and offerings with "
        "a parent merge only with siblings of the same parent and scope; the "
        "parent link stays on both sides.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace_remote.ProjectUpdateRequest.offering",
        NOT_APPLICABLE,
        "Remote offerings are refused by the preview.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.OfferingMerge.target",
        NOT_APPLICABLE,
        "The merge record itself.",
        area=AREA_OFFERING_CONFIGURATION,
    ),
    E(
        "marketplace.OfferingMerge.sources",
        NOT_APPLICABLE,
        "The merge record itself.",
        kind=M2M,
        area=AREA_OFFERING_CONFIGURATION,
    ),
    # Generic foreign keys and stored uuids: not discoverable from _meta.
    E(
        "permissions.UserRole.scope",
        KEEP_ON_SOURCE,
        "Offering-scoped roles (e.g. offering manager) grant control of the "
        "offering; copying them would give the source's managers the target. "
        "Resource-scoped roles are unaffected: the resources keep their ids.",
        kind=GENERIC,
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "permissions.RoleAvailability.scope",
        KEEP_ON_SOURCE,
        "The source's role catalogue; the target's catalogue is its own.",
        kind=GENERIC,
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "permissions.CustomerRoleConcealment.scope",
        NOT_APPLICABLE,
        "Scoped to customers in practice, never to offerings.",
        kind=GENERIC,
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "checklist.ChecklistCompletion.scope",
        KEEP_ON_SOURCE,
        "Completions scoped to the offering answer the source's checklist; "
        "per-user completions hang off OfferingUser and move with it.",
        kind=GENERIC,
        ct_field="scope_content_type",
        id_field="scope_object_id",
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "logging.EventConsumerScope.scope",
        KEEP_ON_SOURCE,
        "Repointing would let a consumer of the source read every event of the target.",
        kind=GENERIC,
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "logging.EventSubscriptionQueue.offering_uuid",
        KEEP_ON_SOURCE,
        "Queues of subscribers to the source; same reason as EventConsumerScope.",
        kind=UUID,
        area=AREA_ACCOUNTS_AND_ACCESS,
    ),
    E(
        "waldur_pid.DataciteReferral.scope",
        KEEP_ON_SOURCE,
        "Referrals to the source's DOI.",
        kind=GENERIC,
        area=AREA_OFFERING_CONFIGURATION,
    ),
)


def _relation_label(field) -> str:
    return f"{field.model._meta.label}.{field.name}"


def discover_relations() -> set[str]:
    """Labels of every FK, one-to-one and many-to-many that points at a merged model.

    Walks the reverse relations (``_meta.get_fields(include_hidden=True)``, i.e.
    ``_meta.related_objects`` plus the ones hidden by ``related_name="+"``) of
    the four merged models. Relations of auto-created many-to-many through
    tables are reported as the many-to-many field that owns them.
    """
    labels = set()
    for model_name in MERGED_MODELS:
        model = apps.get_model("marketplace", model_name)
        for relation in model._meta.get_fields(include_hidden=True):
            if not (relation.auto_created and not relation.concrete):
                continue
            if not relation.is_relation or relation.related_model is None:
                continue
            if relation.related_model._meta.auto_created:
                # FK of an auto-created M2M through table; its M2M field is
                # reported by its own reverse relation.
                continue
            if (
                relation.many_to_many
                and not relation.field.remote_field.through._meta.auto_created
            ):
                # M2M through an explicit model: that model's FKs are the
                # relations, and they are reported on their own.
                continue
            labels.add(_relation_label(relation.field))
    return labels


def find_uncovered_relations(registry: dict[str, CoverageEntry] | None = None):
    """Relations to merged models that ``registry`` has no entry for."""
    registry = MERGE_COVERAGE if registry is None else registry
    return sorted(discover_relations() - set(registry))


def find_stale_entries(registry: dict[str, CoverageEntry] | None = None):
    """FK and M2M entries of ``registry`` that no longer match a real relation."""
    registry = MERGE_COVERAGE if registry is None else registry
    discovered = discover_relations()
    return sorted(
        label
        for label, entry in registry.items()
        if entry.kind in (FK, M2M) and label not in discovered
    )
