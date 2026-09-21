"""Offering merge engine: preview, signal-free execution and undo.

A merge moves everything that belongs to one or more source offerings onto a
target offering, as the coverage registry (``offering_merge_coverage``) lists it,
and archives the sources. It never deletes an offering: ``Resource.offering`` is
``on_delete=CASCADE``.

* :func:`preview` computes counts, blockers and warnings, stores them on the
  :class:`~waldur_mastermind.marketplace.models.OfferingMerge` and moves it to
  ``previewed``. It writes nothing else.
* :func:`execute` locks the offerings, recomputes the preview and refuses if it
  differs from the stored one, then writes every change with
  ``QuerySet.update()`` — no save signal fires, so no plan period is closed or
  opened and no invoice item is terminated or reissued — journalling each one
  as an ``OfferingMergeChange``. Between registry entries it reports its
  progress on ``OfferingMerge.progress``, visible while it runs.
* :func:`undo` restores the foreign keys from the journal, applies the
  inverse key renames to JSON documents as they are now, and moves rows
  created for the moved resources since the merge back with them. It refuses
  if a moved resource switched plan or offering, or a row cannot go back.

``ComponentUsageMonthly`` is derived, so both directions recompute it for the
affected components and months instead of journalling it; the offering counter
quotas the bypassed signals would have maintained are recounted as well. The
recompute runs after the invoice snapshot rewrite, because the historical
allocation of LIMIT components is read from ``InvoiceItem.plan_component``.

Invoice snapshots (``InvoiceItem.details`` and ``InvoiceItem.plan_component``)
of the moved resources are rewritten per ``OfferingMerge.invoice_policy``:
``open_month`` touches only items on invoices that can still change
(``Invoice.States.MUTABLE_STATES``, the check ``move_resource`` uses), and
``all_months`` every item. A snapshot key is rewritten only if the document
already has it and its value names something the merge maps:

* ``offering_uuid``, ``offering_name`` and ``offering_type`` when the snapshot's
  ``offering_uuid`` is a source; ``service_provider_uuid`` and
  ``service_provider_name`` as well if the target has another provider;
* ``plan_uuid`` and ``plan_name`` when ``plan_uuid`` is a mapped source plan;
* ``plan_component_id``, ``offering_component_type`` and
  ``offering_component_name`` when the item's plan component (the foreign key,
  else ``details.plan_component_id``) is mapped; failing that, the component
  type and name when ``offering_component_type`` is a mapped source type.

``plan_component`` is repointed only when it is mapped. Items without one, or
with an unmapped one (manual items, credit compensations, items that copy
another item's ``details``), keep it; their snapshot keys follow the rules
above, so an item that names no source keeps its ``details`` unchanged. Keys are
never added, and ``InvoiceItem.name`` is never rewritten.

Both :func:`execute` and :func:`undo` verify their result inside the same
transaction, after every write, and store the report in
``OfferingMerge.verification``. A failed check does not roll back.
"""

import hashlib
import json
import logging
import uuid as uuid_lib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db import models as django_models
from django.utils import timezone
from django_fsm import TransitionNotAllowed

from waldur_core.quotas import fields as quotas_fields

from . import models, tasks
from . import offering_merge_coverage as coverage
from .enums import (
    BASIC_OFFERING,
    REMOTE_OFFERING,
    SITE_AGENT_OFFERING,
    SUPPORT_OFFERING,
    OfferingStates,
    OfferingUserStates,
    OrderStates,
    ResourceStates,
)

logger = logging.getLogger(__name__)

# Types whose resources have no backend scope object, so their resources can
# move between them in any direction. Any other type merges only into itself.
CROSS_TYPE_MERGEABLE_TYPES = frozenset(
    {BASIC_OFFERING, SUPPORT_OFFERING, SITE_AGENT_OFFERING}
)

# Rows written per UPDATE statement.
BATCH_SIZE = 1000

# A source plan needs a target plan only if one of these references it.
PLAN_REFERENCES = (
    "marketplace.Resource.plan",
    "marketplace.Order.plan",
    "marketplace.Order.old_plan",
    "marketplace.ResourcePlanPeriod.plan",
    "proposal.RequestedOffering.plan",
)

# Usage and quota history that makes a source component need a target one.
COMPONENT_REFERENCES = (
    "marketplace.ComponentUsage.component",
    "marketplace.ComponentQuota.component",
    "marketplace.ComponentUsagePollRecord.component",
    "marketplace.ComponentUserUsageLimit.component",
)


class OfferingMergeError(Exception):
    """A merge, or its undo, was refused. Nothing was written."""

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details


def _issue(code: str, message: str, **details) -> dict:
    return {"code": code, "message": message, "details": details}


def _hex(value) -> str | None:
    try:
        return uuid_lib.UUID(str(value)).hex
    except (TypeError, ValueError, AttributeError):
        return None


def _json_safe(value):
    return json.loads(json.dumps(value, default=str))


def _key_mapping(merge) -> dict:
    mapping = merge.attribute_key_mapping
    return mapping if isinstance(mapping, dict) else {}


def _rename_keys(value, mapping: dict):
    """Rename the keys of a JSON object; mapped keys win over unmapped ones."""
    if not isinstance(value, dict) or not mapping:
        return value
    result = {key: item for key, item in value.items() if key not in mapping}
    for key, item in value.items():
        if key in mapping:
            result[mapping[key]] = item
    return result


def _rename_answers(value, key_mapping: dict, type_mapping: dict):
    """Rename answer keys, and the component-type keys of ``old_limits``.

    A rename onto a key the document already has is skipped rather than
    overwriting that answer.
    """
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        new_key = key_mapping.get(key, key)
        if new_key != key and new_key in value:
            new_key = key
        result[new_key] = item
    if isinstance(result.get("old_limits"), dict):
        result["old_limits"] = _rename_keys(result["old_limits"], type_mapping)
    return result


# --- Invoice snapshots -------------------------------------------------------

# Snapshot keys written by ``billing_utils.get_component_details``.
OFFERING_SNAPSHOT_KEYS = ("offering_uuid", "offering_name", "offering_type")
PROVIDER_SNAPSHOT_KEYS = ("service_provider_uuid", "service_provider_name")


def _invoice_models():
    """Resolved lazily: the invoices app imports marketplace models."""
    return apps.get_model("invoices", "Invoice"), apps.get_model(
        "invoices", "InvoiceItem"
    )


def _mutable_invoice_states():
    return _invoice_models()[0].States.MUTABLE_STATES


def _invoice_items_in_scope(resource_ids, policy):
    """Invoice items of ``resource_ids`` that ``policy`` allows a merge to touch."""
    items = _invoice_models()[1].objects.filter(resource_id__in=list(resource_ids))
    if policy != models.OfferingMerge.InvoicePolicies.ALL_MONTHS:
        items = items.filter(invoice__state__in=_mutable_invoice_states())
    return items


def _offering_snapshot(offering: models.Offering) -> dict:
    customer = offering.customer
    provider = getattr(customer, "serviceprovider", None) if customer else None
    return {
        "offering_uuid": offering.uuid.hex,
        "offering_name": offering.name,
        "offering_type": offering.type,
        "service_provider_uuid": provider.uuid.hex if provider else "",
        "service_provider_name": customer.name if customer else "",
    }


@dataclass
class _SnapshotMap:
    """How the invoice snapshots of one moved resource are rewritten."""

    # Snapshot offering uuids (hex) whose offering-level keys are rewritten.
    offering_uuids: frozenset
    # Offering-level values written instead (see _offering_snapshot).
    offering: dict
    # Whether the service provider keys are rewritten too.
    provider_changes: bool
    # Old plan component id -> new plan component (plan and component loaded).
    plan_components: dict
    # Old plan uuid (hex) -> new plan.
    plans: dict
    # Old component type -> new offering component.
    component_types: dict


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _set_present(document: dict, key: str, value):
    """Overwrite ``key`` if the document has it, keeping a stringly-typed value a string."""
    if key not in document:
        return
    if isinstance(document[key], str) and not isinstance(value, str):
        value = str(value)
    document[key] = value


def _rewrite_snapshot(details, plan_component_id, mapping: _SnapshotMap):
    """(new details, new plan component id) of one invoice item; see the module doc."""
    new_pc = (
        mapping.plan_components.get(plan_component_id)
        if plan_component_id is not None
        else None
    )
    new_pc_id = new_pc.id if new_pc is not None else plan_component_id
    if not isinstance(details, dict):
        return details, new_pc_id

    result = dict(details)
    if _hex(details.get("offering_uuid")) in mapping.offering_uuids:
        keys = OFFERING_SNAPSHOT_KEYS
        if mapping.provider_changes:
            keys += PROVIDER_SNAPSHOT_KEYS
        for key in keys:
            _set_present(result, key, mapping.offering[key])

    plan = mapping.plans.get(_hex(details.get("plan_uuid")))
    if plan is not None:
        _set_present(result, "plan_uuid", plan.uuid.hex)
        _set_present(result, "plan_name", plan.name)

    snapshot_pc = new_pc
    if snapshot_pc is None:
        snapshot_pc = mapping.plan_components.get(
            _as_int(details.get("plan_component_id"))
        )
    if snapshot_pc is not None:
        _set_present(result, "plan_component_id", snapshot_pc.id)
        component = snapshot_pc.component
    else:
        component_type = details.get("offering_component_type")
        component = (
            mapping.component_types.get(component_type)
            if isinstance(component_type, str)
            else None
        )
    if component is not None:
        _set_present(result, "offering_component_type", component.type)
        _set_present(result, "offering_component_name", component.name)
    return result, new_pc_id


def _restore_keys(current, old, new):
    """Undo a snapshot rewrite key by key; None if a rewritten key changed since.

    Only the keys the merge changed are put back, so a document that changed
    elsewhere since (``resource_limit_periods`` grows with limit changes and
    terminations) keeps those changes.
    """
    if not all(isinstance(value, dict) for value in (current, old, new)):
        return old if current == new else None
    missing = object()
    result = dict(current)
    for key in set(old) | set(new):
        if old.get(key, missing) == new.get(key, missing):
            continue
        if current.get(key, missing) != new.get(key, missing):
            return None
        if key in old:
            result[key] = old[key]
        else:
            result.pop(key, None)
    return result


@dataclass
class _Write:
    """Planned changes to one field: ``rows`` are (pk, old value, new value)."""

    entry: coverage.CoverageEntry
    model: type[django_models.Model]
    attname: str
    is_json: bool
    rows: list[tuple[int, Any, Any]] = field(default_factory=list)


def _resolve_relation(entry: coverage.CoverageEntry):
    """(model holding the reference, its attname, unique-set attnames, related model)."""
    model = entry.model
    model_field = model._meta.get_field(entry.field_name)
    if entry.kind == coverage.M2M:
        through = model_field.remote_field.through
        reference = through._meta.get_field(model_field.m2m_reverse_field_name())
        owner = through._meta.get_field(model_field.m2m_field_name())
        return through, reference.attname, (owner.attname,), model_field.related_model
    unique = tuple(model._meta.get_field(name).attname for name in entry.unique_with)
    return model, model_field.attname, unique, model_field.related_model


class _MergeContext:
    """Everything the preview and the executor derive from a merge record.

    Building it performs reads only. ``writes`` is the complete list of changes
    execution would make; the preview reports on the same list, so the two can
    never disagree about what a merge does.
    """

    def __init__(self, merge: models.OfferingMerge, source_ids=None):
        # ``source_ids`` stands in for ``merge.sources`` when the merge record is
        # not stored (see :func:`preview_selection`).
        self.merge = merge
        self.target = models.Offering.objects.get(pk=merge.target_id)
        if source_ids is None:
            source_ids = merge.sources.values_list("pk", flat=True)
        self.sources = list(
            models.Offering.objects.filter(pk__in=source_ids).order_by("id")
        )
        self.source_ids = [source.id for source in self.sources]
        self.blockers: list[dict] = []
        self.warnings: list[dict] = []
        self.counts: dict[str, int] = {}
        self.left_on_source: dict[str, list[int]] = {}
        self.writes: list[_Write] = []

        self.source_plans = list(
            models.Plan.objects.filter(offering_id__in=self.source_ids).order_by("id")
        )
        self.source_components = list(
            models.OfferingComponent.objects.filter(
                offering_id__in=self.source_ids
            ).order_by("id")
        )
        self.source_plan_components = list(
            models.PlanComponent.objects.filter(
                plan__offering_id__in=self.source_ids
            ).order_by("id")
        )
        self.plan_map: dict[int, int] = {}
        self.component_map: dict[int, int] = {}
        self.plan_component_map: dict[int, int] = {}
        # Source offering id -> {source component type: target component type}
        self.component_type_map: dict[int, dict[str, str]] = {
            source_id: {} for source_id in self.source_ids
        }

        # Moved resource id -> the source offering it leaves.
        self.resource_offering = dict(
            models.Resource.objects.filter(offering_id__in=self.source_ids)
            .order_by("pk")
            .values_list("pk", "offering_id")
        )
        self.resource_ids = list(self.resource_offering)
        self.invoice_writes: list[_Write] = []
        self.invoice_summary: dict = {}

        self.summary_scope = _summary_scope(
            self.source_ids, self.target.id, self.resource_ids
        )

        self._check_selection()
        self._check_plan_mapping()
        self._check_component_mapping()
        self._map_plan_components()
        self._check_pending_orders()
        self._check_creation_issues()
        self._plan_writes()
        self._plan_invoice_snapshots()
        self._warn_answer_keys()
        self._warn_offering_users()
        self._warn_empty_backend_ids()
        self._warn_configuration_left_behind()

    # --- Selection ----------------------------------------------------------

    def _check_selection(self):
        if not self.sources:
            self.blockers.append(_issue("no_sources", "No source offering selected."))
        if self.target.id in self.source_ids:
            self.blockers.append(
                _issue(
                    "target_in_sources",
                    "The target cannot also be a source.",
                    offering=self.target.uuid.hex,
                )
            )
        if self.target.state == OfferingStates.ARCHIVED:
            self.blockers.append(
                _issue(
                    "target_archived",
                    "The target offering is archived.",
                    offering=self.target.uuid.hex,
                )
            )
        offerings = [*self.sources, self.target]
        # Offerings with a parent may merge only with their siblings: the same
        # parent and the same scope, as the per-tenant offerings of one tenant
        # have. The parent link stays as it is on both sides.
        siblings = (
            len({offering.parent_id for offering in offerings}) == 1
            and len(
                {
                    (offering.content_type_id, offering.object_id)
                    for offering in offerings
                }
            )
            == 1
        )
        for offering in offerings:
            if offering.type == REMOTE_OFFERING:
                self.blockers.append(
                    _issue(
                        "remote_offering",
                        f"Offering {offering.name} is a remote offering.",
                        offering=offering.uuid.hex,
                    )
                )
            if (offering.parent_id and not siblings) or offering.children.exists():
                self.blockers.append(
                    _issue(
                        "offering_hierarchy",
                        f"Offering {offering.name} has a parent or children.",
                        offering=offering.uuid.hex,
                    )
                )
        for source in self.sources:
            if source.type == self.target.type:
                continue
            if (
                source.type in CROSS_TYPE_MERGEABLE_TYPES
                and self.target.type in CROSS_TYPE_MERGEABLE_TYPES
            ):
                continue
            self.blockers.append(
                _issue(
                    "offering_type_not_allowed",
                    f"Cannot merge a {source.type} offering into a "
                    f"{self.target.type} offering.",
                    offering=source.uuid.hex,
                    source_type=source.type,
                    target_type=self.target.type,
                )
            )

    # --- Mappings -----------------------------------------------------------

    def _check_plan_mapping(self):
        raw = self.merge.plan_mapping
        if not isinstance(raw, dict):
            self.blockers.append(
                _issue("invalid_plan_mapping", "plan_mapping must be an object.")
            )
            raw = {}
        source_plans = {plan.uuid.hex: plan for plan in self.source_plans}
        target_plans = {plan.uuid.hex: plan for plan in self.target.plans.all()}
        mapping = {}
        for key, value in raw.items():
            source_uuid, target_uuid = _hex(key), _hex(value)
            if source_uuid not in source_plans or target_uuid not in target_plans:
                self.blockers.append(
                    _issue(
                        "invalid_plan_mapping",
                        "plan_mapping must map source plans to target plans.",
                        source_plan=str(key),
                        target_plan=str(value),
                    )
                )
                continue
            mapping[source_uuid] = target_plans[target_uuid]

        referenced = self._referenced_plan_ids()
        for plan in self.source_plans:
            target_plan = mapping.get(plan.uuid.hex)
            if target_plan is None:
                if plan.id not in referenced:
                    continue  # unused: stays on the archived source
                self.blockers.append(
                    _issue(
                        "unmapped_plan",
                        f"Plan {plan.name} has no target plan.",
                        plan=plan.uuid.hex,
                    )
                )
                continue
            if plan.unit != target_plan.unit:
                self.blockers.append(
                    _issue(
                        "plan_unit_mismatch",
                        f"Plan {plan.name} is billed per {plan.unit}, target plan "
                        f"{target_plan.name} per {target_plan.unit}.",
                        source_plan=plan.uuid.hex,
                        target_plan=target_plan.uuid.hex,
                        source_unit=plan.unit,
                        target_unit=target_plan.unit,
                    )
                )
            self.plan_map[plan.id] = target_plan.id

    def _referenced_plan_ids(self) -> set[int]:
        """Source plans that moved rows reference, and so need a target plan."""
        plan_ids = [plan.id for plan in self.source_plans]
        referenced = set()
        for label in PLAN_REFERENCES:
            entry = coverage.MERGE_COVERAGE[label]
            attname = entry.model._meta.get_field(entry.field_name).attname
            referenced.update(
                entry.model._base_manager.filter(**{f"{attname}__in": plan_ids})
                .values_list(attname, flat=True)
                .distinct()
            )
        return referenced

    def _referenced_component_ids(self) -> set[int]:
        """Source components that moved rows reference, and so need a target.

        Usage and quota history, priced plan components of mapped plans, and
        component types used as keys of the moved rows' JSON documents.
        """
        component_ids = [component.id for component in self.source_components]
        referenced = set()
        for label in COMPONENT_REFERENCES:
            entry = coverage.MERGE_COVERAGE[label]
            referenced.update(
                entry.model._base_manager.filter(component_id__in=component_ids)
                .values_list("component_id", flat=True)
                .distinct()
            )
        referenced.update(
            models.PlanComponent.objects.filter(
                plan_id__in=list(self.plan_map), component_id__in=component_ids
            )
            .filter(django_models.Q(price__gt=0) | django_models.Q(amount__gt=0))
            .values_list("component_id", flat=True)
        )
        by_type = {
            (component.offering_id, component.type): component.id
            for component in self.source_components
        }
        for entry in coverage.MERGE_COVERAGE.values():
            if entry.kind != coverage.JSON_KEYS:
                continue
            for offering_id, value, _pk in self._json_rows(entry):
                if not isinstance(value, dict):
                    continue
                keys = set(value)
                if entry.rename == coverage.ANSWER_KEYS:
                    old_limits = value.get("old_limits")
                    keys = set(old_limits) if isinstance(old_limits, dict) else set()
                for key in keys:
                    component_id = by_type.get((offering_id, key))
                    if component_id is not None:
                        referenced.add(component_id)
        return referenced

    def _json_rows(self, entry: coverage.CoverageEntry):
        """(offering id, JSON value, pk) of the moved rows of a JSON entry."""
        model = entry.model
        attname = model._meta.get_field(entry.field_name).attname
        qs = model._base_manager.filter(
            **{f"{entry.offering_path}__in": self.source_ids}
        )
        if entry.mapped_plan_path:
            qs = qs.filter(**{f"{entry.mapped_plan_path}__in": list(self.plan_map)})
        return qs.order_by("pk").values_list(entry.offering_path, attname, "pk")

    def _check_component_mapping(self):
        raw = self.merge.component_mapping
        if not isinstance(raw, dict):
            self.blockers.append(
                _issue(
                    "invalid_component_mapping", "component_mapping must be an object."
                )
            )
            raw = {}
        sources_by_uuid = {source.uuid.hex: source for source in self.sources}
        per_source: dict[int, dict] = {}
        for key, value in raw.items():
            source = sources_by_uuid.get(_hex(key))
            if source is None or not isinstance(value, dict):
                self.blockers.append(
                    _issue(
                        "invalid_component_mapping",
                        "component_mapping must be keyed by source offering.",
                        offering=str(key),
                    )
                )
                continue
            per_source[source.id] = value

        target_components = {
            component.type: component for component in self.target.components.all()
        }
        components_by_source = defaultdict(dict)
        for component in self.source_components:
            components_by_source[component.offering_id][component.type] = component

        referenced = self._referenced_component_ids()
        for source in self.sources:
            mapping = per_source.get(source.id, {})
            components = components_by_source[source.id]
            for source_type, target_type in mapping.items():
                if (
                    source_type not in components
                    or target_type not in target_components
                ):
                    self.blockers.append(
                        _issue(
                            "invalid_component_mapping",
                            "component_mapping must map source component types to "
                            "target component types.",
                            offering=source.uuid.hex,
                            source_type=str(source_type),
                            target_type=str(target_type),
                        )
                    )
            duplicated = [
                target_type
                for target_type, count in Counter(mapping.values()).items()
                if count > 1
            ]
            if duplicated:
                self.blockers.append(
                    _issue(
                        "component_mapping_not_injective",
                        "Two components of one source cannot share a target "
                        "component: their usage, quotas and limits would collide.",
                        offering=source.uuid.hex,
                        target_types=sorted(str(t) for t in duplicated),
                    )
                )
            for component_type, component in components.items():
                target_type = mapping.get(component_type)
                target_component = target_components.get(target_type)
                if target_type is None:
                    if component.id not in referenced:
                        continue  # unused: stays on the archived source
                    self.blockers.append(
                        _issue(
                            "unmapped_component",
                            f"Component {component_type} of {source.name} has no "
                            "target component.",
                            offering=source.uuid.hex,
                            component_type=component_type,
                        )
                    )
                    continue
                if target_component is None:
                    continue  # reported as invalid_component_mapping
                if component.billing_type != target_component.billing_type:
                    self.blockers.append(
                        _issue(
                            "component_billing_type_mismatch",
                            f"Component {component_type} is billed as "
                            f"{component.billing_type}, target component "
                            f"{target_type} as {target_component.billing_type}.",
                            offering=source.uuid.hex,
                            source_type=component_type,
                            target_type=target_type,
                            source_billing_type=component.billing_type,
                            target_billing_type=target_component.billing_type,
                        )
                    )
                self.component_map[component.id] = target_component.id
                self.component_type_map[source.id][component_type] = target_type

    def _map_plan_components(self):
        target_plan_components = {
            (pc.plan_id, pc.component_id): pc
            for pc in models.PlanComponent.objects.filter(
                plan_id__in=set(self.plan_map.values())
            )
        }
        plans = {plan.id: plan for plan in self.source_plans}
        components = {component.id: component for component in self.source_components}
        differences = []
        for plan_component in self.source_plan_components:
            target_plan_id = self.plan_map.get(plan_component.plan_id)
            target_component_id = self.component_map.get(plan_component.component_id)
            if target_plan_id is None or target_component_id is None:
                continue
            target_pc = target_plan_components.get(
                (target_plan_id, target_component_id)
            )
            if target_pc is not None:
                self.plan_component_map[plan_component.id] = target_pc.id
            if target_pc is None or target_pc.price != plan_component.price:
                differences.append(
                    {
                        "source_plan": plans[plan_component.plan_id].uuid.hex,
                        "component_type": components[plan_component.component_id].type,
                        "source_price": str(plan_component.price),
                        "target_price": (
                            str(target_pc.price) if target_pc is not None else None
                        ),
                    }
                )
        if differences:
            self.warnings.append(
                _issue(
                    "plan_price_difference",
                    "Mapped plan components have different prices; the merge "
                    "changes what customers pay from now on.",
                    components=differences,
                )
            )

    # --- State checks -------------------------------------------------------

    def _check_pending_orders(self):
        pending = list(
            models.Order.objects.filter(
                offering_id__in=self.source_ids, state__in=OrderStates.PENDING_STATES
            ).values_list("uuid", flat=True)
        )
        if pending:
            self.blockers.append(
                _issue(
                    "pending_orders",
                    f"{len(pending)} order(s) of the sources are still pending.",
                    orders=sorted(order_uuid.hex for order_uuid in pending),
                )
            )

    def _check_creation_issues(self):
        support_ids = [s.id for s in self.sources if s.type == SUPPORT_OFFERING]
        if not support_ids:
            return
        issue_model = apps.get_model("support", "Issue")
        issue_ids = models.Resource.objects.filter(
            offering_id__in=support_ids,
            content_type=ContentType.objects.get_for_model(issue_model),
        ).values_list("object_id", flat=True)
        open_issues = list(
            issue_model.objects.filter(id__in=issue_ids)
            .open()
            .values_list("uuid", flat=True)
        )
        if open_issues:
            self.blockers.append(
                _issue(
                    "open_creation_issue",
                    f"{len(open_issues)} resource(s) still have an open creation "
                    "request.",
                    issues=sorted(issue_uuid.hex for issue_uuid in open_issues),
                )
            )

    # --- Writes -------------------------------------------------------------

    def _source_ids_for(self, related_model) -> list[int]:
        if related_model is models.Offering:
            return self.source_ids
        if related_model is models.Plan:
            return [plan.id for plan in self.source_plans]
        if related_model is models.OfferingComponent:
            return [component.id for component in self.source_components]
        if related_model is models.PlanComponent:
            return [pc.id for pc in self.source_plan_components]
        raise ValueError(f"Unexpected related model {related_model}")

    def _value_map_for(self, related_model) -> dict[int, int]:
        if related_model is models.Offering:
            return {source_id: self.target.id for source_id in self.source_ids}
        if related_model is models.Plan:
            return self.plan_map
        if related_model is models.OfferingComponent:
            return self.component_map
        if related_model is models.PlanComponent:
            return self.plan_component_map
        raise ValueError(f"Unexpected related model {related_model}")

    def _plan_writes(self):
        entries = sorted(coverage.MERGE_COVERAGE.values(), key=lambda e: e.phase)
        for entry in entries:
            if entry.strategy == coverage.NOT_APPLICABLE:
                continue
            if entry.strategy == coverage.RECOMPUTE:
                component_ids, periods = self.summary_scope
                self.counts[entry.label] = len(component_ids) * len(periods)
            elif entry.kind in (coverage.FK, coverage.M2M):
                self._plan_relation(entry)
            elif entry.kind == coverage.JSON_KEYS:
                self._plan_json_keys(entry)
            elif entry.kind == coverage.SNAPSHOT:
                self.counts[entry.label] = entry.model._base_manager.filter(
                    **{f"{entry.offering_path}__in": self.source_ids}
                ).count()
            elif entry.kind == coverage.GENERIC:
                self.counts[entry.label] = entry.model._base_manager.filter(
                    **{
                        entry.ct_field: ContentType.objects.get_for_model(
                            models.Offering
                        ),
                        f"{entry.id_field}__in": self.source_ids,
                    }
                ).count()
            elif entry.kind == coverage.UUID:
                self.counts[entry.label] = entry.model._base_manager.filter(
                    **{
                        f"{entry.field_name}__in": [s.uuid for s in self.sources],
                    }
                ).count()

    def _plan_relation(self, entry: coverage.CoverageEntry):
        model, attname, unique, related_model = _resolve_relation(entry)
        manager = model._base_manager
        rows = list(
            manager.filter(**{f"{attname}__in": self._source_ids_for(related_model)})
            .order_by("pk")
            .values_list("pk", attname, *unique)
        )
        self.counts[entry.label] = len(rows)
        if entry.strategy not in (coverage.REPOINT, coverage.REPOINT_DEDUPE):
            return

        value_map = self._value_map_for(related_model)
        existing = set()
        if unique:
            existing_qs = manager.filter(
                **{f"{attname}__in": set(value_map.values())}
            ).filter(**{f"{unique[0]}__in": {row[2] for row in rows}})
            existing = set(existing_qs.values_list(attname, *unique))

        write = _Write(entry, model, attname, is_json=False)
        collisions = []
        for pk, old, *unique_values in rows:
            new = value_map.get(old)
            if new is None:
                continue  # unmapped: reported as a blocker
            key = (new, *unique_values)
            if unique and key in existing:
                collisions.append(pk)
                continue
            existing.add(key)
            write.rows.append((pk, old, new))

        if collisions:
            if entry.strategy == coverage.REPOINT_DEDUPE:
                self.left_on_source[entry.label] = collisions
            else:
                self.blockers.append(
                    _issue(
                        entry.collision_blocker or "unique_collision",
                        f"{len(collisions)} {model._meta.label} row(s) would collide "
                        "with existing rows of the target.",
                        model=model._meta.label,
                        rows=collisions,
                    )
                )
        if write.rows:
            self.writes.append(write)

    def _plan_json_keys(self, entry: coverage.CoverageEntry):
        model = entry.model
        attname = model._meta.get_field(entry.field_name).attname
        key_mapping = _key_mapping(self.merge)
        write = _Write(entry, model, attname, is_json=True)
        for offering_id, value, pk in self._json_rows(entry):
            type_mapping = self.component_type_map.get(offering_id, {})
            if entry.rename == coverage.COMPONENT_TYPES:
                new = _rename_keys(value, type_mapping)
            else:
                new = _rename_answers(value, key_mapping, type_mapping)
            if new != value:
                write.rows.append((pk, value, new))
        self.counts[entry.label] = len(write.rows)
        if write.rows:
            self.writes.append(write)

    # --- Invoice snapshots --------------------------------------------------

    def _snapshot_maps(self) -> dict[int, _SnapshotMap]:
        """Source offering id -> how its resources' invoice snapshots change."""
        target_pcs = {
            pc.id: pc
            for pc in models.PlanComponent.objects.filter(
                pk__in=set(self.plan_component_map.values())
            ).select_related("plan", "component")
        }
        plan_components = {
            source_pc: target_pcs[target_pc]
            for source_pc, target_pc in self.plan_component_map.items()
        }
        target_plans = models.Plan.objects.in_bulk(set(self.plan_map.values()))
        source_plans = {plan.id: plan for plan in self.source_plans}
        plans = {
            source_plans[source_plan].uuid.hex: target_plans[target_plan]
            for source_plan, target_plan in self.plan_map.items()
        }
        target_components = {
            component.type: component for component in self.target.components.all()
        }
        offering = _offering_snapshot(self.target)
        return {
            source.id: _SnapshotMap(
                offering_uuids=frozenset({source.uuid.hex}),
                offering=offering,
                provider_changes=source.customer_id != self.target.customer_id,
                plan_components=plan_components,
                plans=plans,
                component_types={
                    source_type: target_components[target_type]
                    for source_type, target_type in self.component_type_map[
                        source.id
                    ].items()
                    if target_type in target_components
                },
            )
            for source in self.sources
        }

    def _plan_invoice_snapshots(self):
        """Plan the snapshot rewrite of the moved resources' invoice items."""
        policies = models.OfferingMerge.InvoicePolicies
        policy = self.merge.invoice_policy
        if policy not in (policies.OPEN_MONTH, policies.ALL_MONTHS):
            self.blockers.append(
                _issue(
                    "invalid_invoice_policy",
                    f"Unknown invoice policy {policy}.",
                    policy=str(policy),
                )
            )
            return
        _invoice_model, invoice_item_model = _invoice_models()
        mutable = _mutable_invoice_states()
        maps = self._snapshot_maps()
        open_rows, closed_rows = [], []
        on_closed = 0
        for pk, resource_id, pc_id, details, state in (
            invoice_item_model.objects.filter(resource_id__in=self.resource_ids)
            .order_by("pk")
            .values_list(
                "pk", "resource_id", "plan_component_id", "details", "invoice__state"
            )
        ):
            is_open = state in mutable
            if not is_open:
                on_closed += 1
            new_details, new_pc = _rewrite_snapshot(
                details, pc_id, maps[self.resource_offering[resource_id]]
            )
            if new_details == details and new_pc == pc_id:
                continue
            (open_rows if is_open else closed_rows).append(
                (pk, pc_id, new_pc, details, new_details)
            )

        rows = open_rows
        if policy == policies.ALL_MONTHS:
            rows = sorted(open_rows + closed_rows)
        fk_write = _Write(
            coverage.MERGE_COVERAGE["invoices.InvoiceItem.plan_component"],
            invoice_item_model,
            "plan_component_id",
            is_json=False,
        )
        details_write = _Write(
            coverage.MERGE_COVERAGE["invoices.InvoiceItem.details"],
            invoice_item_model,
            "details",
            is_json=True,
        )
        for pk, old_pc, new_pc, old_details, new_details in rows:
            if old_pc != new_pc:
                fk_write.rows.append((pk, old_pc, new_pc))
            if old_details != new_details:
                details_write.rows.append((pk, old_details, new_details))
        self.invoice_writes = [w for w in (fk_write, details_write) if w.rows]
        self.invoice_summary = {
            "policy": policy,
            "to_rewrite": len(rows),
            "to_rewrite_by_policy": {
                policies.OPEN_MONTH: len(open_rows),
                policies.ALL_MONTHS: len(open_rows) + len(closed_rows),
            },
            "on_closed_invoices": on_closed,
            "kept_on_closed_invoices": (
                on_closed if policy == policies.OPEN_MONTH else 0
            ),
        }

    def stale(self) -> "_Stale":
        """What the moved resources must no longer reference once merged."""
        target_types = {component.type for component in self.target.components.all()}
        renamed = {
            source_id: {
                source_type
                for source_type, target_type in mapping.items()
                if source_type != target_type
            }
            - target_types
            for source_id, mapping in self.component_type_map.items()
        }
        allowed = defaultdict(set)
        for label, pks in self.left_on_source.items():
            allowed[coverage.MERGE_COVERAGE[label].model_label].update(pks)
        source_plans = {plan.id: plan for plan in self.source_plans}
        return _Stale(
            offering_ids=set(self.source_ids),
            plan_ids=set(self.plan_map),
            component_ids=set(self.component_map),
            plan_component_ids=set(self.plan_component_map),
            offering_uuids={source.uuid.hex for source in self.sources},
            plan_uuids={source_plans[pk].uuid.hex for pk in self.plan_map},
            json_keys={
                resource_id: renamed[offering_id]
                for resource_id, offering_id in self.resource_offering.items()
            },
            allowed=dict(allowed),
        )

    # --- Warnings -----------------------------------------------------------

    def _warn_answer_keys(self):
        target_keys = set((self.target.options or {}).get("options") or {})
        source_keys = {
            source.id: set((source.options or {}).get("options") or {})
            for source in self.sources
        }
        key_mapping = _key_mapping(self.merge)
        unknown = Counter()
        for offering_id, attributes in models.Order.objects.filter(
            offering_id__in=self.source_ids
        ).values_list("offering_id", "attributes"):
            if not isinstance(attributes, dict):
                continue
            for key in attributes:
                if (
                    key in source_keys[offering_id]
                    and key not in target_keys
                    and key not in key_mapping
                ):
                    unknown[key] += 1
        if unknown:
            self.warnings.append(
                _issue(
                    "unknown_answer_keys",
                    "Order answers use keys the target's form does not know and "
                    "attribute_key_mapping does not rename.",
                    keys=dict(sorted(unknown.items())),
                )
            )

    def _warn_offering_users(self):
        pks = self.left_on_source.get("marketplace.OfferingUser.offering")
        if not pks:
            return
        users = models.OfferingUser.objects.filter(pk__in=pks).values_list(
            "user__uuid", "user__username"
        )
        self.warnings.append(
            _issue(
                "offering_user_on_both",
                f"{len(pks)} user(s) already have an account on the target (or "
                "another source); their source account stays on the archived "
                "source.",
                users=[
                    {"uuid": user_uuid.hex, "username": username}
                    for user_uuid, username in sorted(set(users))
                ],
            )
        )

    def _warn_empty_backend_ids(self):
        if self.target.type != SITE_AGENT_OFFERING:
            return
        count = (
            models.Resource.objects.filter(
                offering_id__in=self.source_ids, backend_id=""
            )
            .exclude(state=ResourceStates.TERMINATED)
            .count()
        )
        if count:
            self.warnings.append(
                _issue(
                    "empty_backend_id",
                    f"{count} resource(s) have no backend_id; the site agent "
                    "cannot match them to backend objects.",
                    count=count,
                )
            )

    def _warn_configuration_left_behind(self):
        entries = {
            label: count
            for label, count in self.counts.items()
            if count
            and coverage.MERGE_COVERAGE[label].strategy == coverage.KEEP_ON_SOURCE
            and label
            not in (
                "marketplace.Plan.offering",
                "marketplace.OfferingComponent.offering",
                "marketplace.PlanComponent.plan",
                "marketplace.PlanComponent.component",
            )
        }
        if entries:
            self.warnings.append(
                _issue(
                    "source_configuration_stays",
                    "Configuration specific to the sources stays on the archived "
                    "sources.",
                    entries=entries,
                )
            )

    # --- Result -------------------------------------------------------------

    def preview(self) -> dict:
        return _json_safe(
            {
                "target": self.target.uuid.hex,
                "sources": [source.uuid.hex for source in self.sources],
                "counts": self.counts,
                "left_on_source": {
                    label: len(pks) for label, pks in self.left_on_source.items()
                },
                "summaries_to_recompute": {
                    "components": len(self.summary_scope[0]),
                    "periods": [
                        f"{year:04d}-{month:02d}"
                        for year, month in self.summary_scope[1]
                    ],
                },
                "invoice_items": self.invoice_summary,
                "blockers": self.blockers,
                "warnings": self.warnings,
            }
        )


def build_preview(merge: models.OfferingMerge) -> dict:
    """Compute the preview of ``merge`` without writing anything."""
    return _MergeContext(merge).preview()


def preview_selection(
    sources: list[models.Offering],
    target: models.Offering,
    plan_mapping: dict | None = None,
    component_mapping: dict | None = None,
) -> dict:
    """Compute the preview of a merge that is not stored. Reads only.

    Lets a caller report the blockers and warnings of a candidate merge, such
    as a duplicate group with its suggested mapping, before anyone creates it.
    """
    merge = models.OfferingMerge(
        target=target,
        plan_mapping=plan_mapping or {},
        component_mapping=component_mapping or {},
    )
    return _MergeContext(merge, [source.pk for source in sources]).preview()


def preview(merge: models.OfferingMerge) -> models.OfferingMerge:
    """Compute the preview, store it and move the merge to ``previewed``."""
    with transaction.atomic():
        merge = models.OfferingMerge.objects.select_for_update().get(pk=merge.pk)
        try:
            merge.set_previewed()
        except TransitionNotAllowed:
            raise OfferingMergeError(f"Cannot preview a merge in state {merge.state}.")
        merge.preview = build_preview(merge)
        merge.error_message = ""
        merge.save(update_fields=["state", "preview", "error_message", "modified"])
    return merge


def _normalized_name(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def suggest_mapping(sources: list[models.Offering], target: models.Offering) -> dict:
    """Suggest plan and component mappings for a merge form to pre-fill.

    A source plan maps to the target plan with the same name (ignoring case
    and whitespace), preferring one that is not archived. A source component
    maps to the target component of the same type, else of the same name.
    Anything without a match is listed as unmatched. Reads only.
    """
    target_plans = {}
    for plan in target.plans.order_by("archived", "id"):
        target_plans.setdefault(_normalized_name(plan.name), plan)
    target_components = list(target.components.order_by("id"))
    by_type = {component.type: component for component in target_components}
    by_name = {}
    for component in target_components:
        by_name.setdefault(_normalized_name(component.name), component)

    plan_mapping, component_mapping = {}, {}
    unmatched_plans, unmatched_components = [], []
    for source in sources:
        for plan in source.plans.order_by("id"):
            match = target_plans.get(_normalized_name(plan.name))
            if match is None:
                unmatched_plans.append(
                    {
                        "offering_uuid": source.uuid.hex,
                        "plan_uuid": plan.uuid.hex,
                        "name": plan.name,
                    }
                )
            else:
                plan_mapping[plan.uuid.hex] = match.uuid.hex
        types = {}
        for component in source.components.order_by("id"):
            match = by_type.get(component.type) or by_name.get(
                _normalized_name(component.name)
            )
            if match is None:
                unmatched_components.append(
                    {
                        "offering_uuid": source.uuid.hex,
                        "type": component.type,
                        "name": component.name,
                    }
                )
            else:
                types[component.type] = match.type
        component_mapping[source.uuid.hex] = types
    return {
        "plan_mapping": plan_mapping,
        "component_mapping": component_mapping,
        "unmatched_plans": unmatched_plans,
        "unmatched_components": unmatched_components,
    }


def _summary_scope(source_ids, target_id, resource_ids):
    """Components and months whose ComponentUsageMonthly rows a merge affects.

    Every component of the sources and the target, for every month in which the
    moved resources have usage or invoice items, every month in which a source
    component already has a summary, and the current month (whose figures are
    read live from resource limits and current usages).
    """
    invoice_item_model = apps.get_model("invoices", "InvoiceItem")
    component_ids = sorted(
        models.OfferingComponent.objects.filter(
            offering_id__in=[*source_ids, target_id]
        ).values_list("pk", flat=True)
    )
    now = timezone.now()
    periods = {(now.year, now.month)}
    periods.update(
        (period.year, period.month)
        for period in models.ComponentUsage.objects.filter(resource_id__in=resource_ids)
        .values_list("billing_period", flat=True)
        .distinct()
    )
    periods.update(
        invoice_item_model.objects.filter(resource_id__in=resource_ids)
        .values_list("invoice__year", "invoice__month")
        .distinct()
    )
    periods.update(
        (period.year, period.month)
        for period in models.ComponentUsageMonthly.objects.filter(
            component__offering_id__in=source_ids
        )
        .values_list("billing_period", flat=True)
        .distinct()
    )
    return component_ids, sorted(periods)


def _recompute_summaries(component_ids, periods):
    """Rebuild the derived monthly summaries; they are not journalled."""
    for component in models.OfferingComponent.objects.filter(pk__in=component_ids):
        for year, month in periods:
            tasks.refresh_component_usage_summary(
                component, year, month, delete_empty=True
            )


def _recalculate_counters(offering_ids):
    """Recount the offering quotas that follow rows written with update().

    ``order_count`` is a counter maintained by Order save/delete signals, which
    the merge bypasses. The user counts are refreshed nightly, only for
    offerings with active terms of service; they are refreshed here on the same
    condition. Category offering counts follow the archive/unarchive, which go
    through save() and so keep them right.
    """
    for offering in models.Offering.objects.filter(pk__in=offering_ids):
        for quota_field in models.Offering.get_quotas_fields(
            field_class=quotas_fields.CounterQuotaField
        ):
            quota_field.recalculate_usage(offering)
        if offering.terms_of_service_configs.filter(is_active=True).exists():
            offering_users = models.OfferingUser.objects.filter(offering=offering)
            offering.set_quota_usage("total_users_count", offering_users.count())
            offering.set_quota_usage(
                "active_users_count",
                offering_users.filter(state=OfferingUserStates.OK).count(),
            )


def _lock_offerings(merge: models.OfferingMerge):
    ids = [*merge.sources.values_list("pk", flat=True), merge.target_id]
    list(
        models.Offering.objects.select_for_update()
        .filter(pk__in=ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def _journal(merge, model, attname, rows):
    models.OfferingMergeChange.objects.bulk_create(
        [
            models.OfferingMergeChange(
                merge=merge,
                model=model._meta.label,
                object_id=pk,
                field=attname,
                old_value=old,
                new_value=new,
            )
            for pk, old, new in rows
        ],
        batch_size=BATCH_SIZE,
    )


def _apply(merge: models.OfferingMerge, write: _Write):
    _journal(merge, write.model, write.attname, write.rows)
    manager = write.model._base_manager
    if write.is_json:
        for pk, _old, new in write.rows:
            manager.filter(pk=pk).update(**{write.attname: new})
        return
    by_value = defaultdict(list)
    for pk, _old, new in write.rows:
        by_value[new].append(pk)
    for new, pks in by_value.items():
        for start in range(0, len(pks), BATCH_SIZE):
            manager.filter(pk__in=pks[start : start + BATCH_SIZE]).update(
                **{write.attname: new}
            )


def rewrite_invoice_snapshots(merge: models.OfferingMerge, context: _MergeContext):
    """Rewrite the moved resources' invoice snapshots per ``merge.invoice_policy``.

    ``plan_component`` is journalled as a foreign key and ``details`` as the
    whole previous document; both are written with ``update()``. The rules are
    in the module docstring.
    """
    for write in context.invoice_writes:
        _apply(merge, write)


@dataclass
class _Stale:
    """References the moved resources must no longer hold after a merge or undo."""

    offering_ids: set
    plan_ids: set
    component_ids: set
    plan_component_ids: set
    offering_uuids: set
    plan_uuids: set
    # Moved resource id -> component-type keys of its limits and usages that
    # should have been renamed.
    json_keys: dict
    # Model label -> pks deliberately left on the old side (dedupe collisions).
    allowed: dict = field(default_factory=dict)
    # Invoice items not to check: those the undo skipped, and those that
    # existed at the merge but were not rewritten (their snapshot may name the
    # target legitimately).
    ignored_invoice_items: set = field(default_factory=set)


def _check(code: str, passed: bool, **details) -> dict:
    return {"code": code, "passed": bool(passed), "details": _json_safe(details)}


# Rows listed per failing check at most.
MAX_REPORTED_ROWS = 50


class _Verification:
    """Checks that a merge, or its undo, left resources and billing intact.

    Constructed before the writes, it captures what the checks compare against;
    :meth:`verify` runs after every write, in the same transaction. ``moves``
    maps each moved resource id to (offering it leaves, offering it reaches).
    """

    def __init__(self, merge, moves: dict, offering_ids, policy: str):
        self.merge = merge
        self.moves = moves
        self.resource_ids = sorted(moves)
        self.offering_ids = sorted(set(offering_ids))
        self.policy = policy
        self.offering_uuids = dict(
            models.Offering.objects.filter(pk__in=self.offering_ids).values_list(
                "pk", "uuid"
            )
        )
        self.customer_ids = sorted(
            {
                customer_id
                for customer_id in models.Resource.objects.filter(
                    pk__in=self.resource_ids
                ).values_list("project__customer_id", flat=True)
                if customer_id is not None
            }
        )
        self.resource_counts = self._resource_counts()
        self.invoice_totals = self._invoice_totals()
        self.closed_items = (
            self._closed_item_hashes()
            if policy == models.OfferingMerge.InvoicePolicies.OPEN_MONTH
            else None
        )
        self.roles = self._offering_roles()

    # --- Captures -----------------------------------------------------------

    def _resource_counts(self) -> dict[int, int]:
        counts = dict.fromkeys(self.offering_ids, 0)
        counts.update(
            models.Resource.objects.filter(offering_id__in=self.offering_ids)
            .values_list("offering_id")
            .annotate(count=django_models.Count("pk"))
            .values_list("offering_id", "count")
        )
        return counts

    def _invoice_totals(self) -> dict[str, str]:
        """Total of each affected customer's invoice for the current month."""
        invoice_model, _ = _invoice_models()
        now = timezone.now()
        invoices = (
            invoice_model.objects.filter(
                customer_id__in=self.customer_ids,
                year=now.year,
                month=now.month,
                state__in=_mutable_invoice_states(),
            )
            .select_related("customer")
            .prefetch_related("items")
        )
        return {invoice.customer.uuid.hex: str(invoice.total) for invoice in invoices}

    def _closed_item_hashes(self) -> dict[int, str]:
        _, invoice_item_model = _invoice_models()
        items = (
            invoice_item_model.objects.filter(resource_id__in=self.resource_ids)
            .exclude(invoice__state__in=_mutable_invoice_states())
            .values_list("pk", "details", "plan_component_id")
        )
        return {
            pk: hashlib.sha256(
                json.dumps([details, pc_id], sort_keys=True, default=str).encode()
            ).hexdigest()
            for pk, details, pc_id in items
        }

    def _offering_roles(self) -> dict[int, int]:
        user_role_model = apps.get_model("permissions", "UserRole")
        counts = dict.fromkeys(self.offering_ids, 0)
        counts.update(
            user_role_model.objects.filter(
                content_type=ContentType.objects.get_for_model(models.Offering),
                object_id__in=self.offering_ids,
                is_active=True,
            )
            .values_list("object_id")
            .annotate(count=django_models.Count("pk"))
            .values_list("object_id", "count")
        )
        return counts

    # --- Checks -------------------------------------------------------------

    def _check_resource_counts(self) -> dict:
        expected = dict(self.resource_counts)
        for leaves, reaches in self.moves.values():
            expected[leaves] = expected.get(leaves, 0) - 1
            expected[reaches] = expected.get(reaches, 0) + 1
        actual = self._resource_counts()
        return _check(
            "resource_count",
            all(expected[pk] == actual.get(pk, 0) for pk in expected),
            offerings={
                self.offering_uuids[pk].hex: {
                    "before": self.resource_counts.get(pk, 0),
                    "expected": expected[pk],
                    "actual": actual.get(pk, 0),
                }
                for pk in self.offering_ids
            },
        )

    def _check_plan_periods(self) -> dict:
        resource_ids = (
            models.ResourcePlanPeriod.objects.filter(
                resource_id__in=self.resource_ids, end=None
            )
            .values_list("resource_id")
            .annotate(count=django_models.Count("pk"))
            .filter(count__gt=1)
            .values_list("resource_id", flat=True)
        )
        resources = models.Resource.objects.filter(
            pk__in=list(resource_ids)
        ).values_list("uuid", flat=True)
        return _check(
            "single_open_plan_period",
            not resources,
            resources=sorted(resource_uuid.hex for resource_uuid in resources),
        )

    def _check_invoice_totals(self) -> dict:
        after = self._invoice_totals()
        customers = {
            customer: {
                "before": self.invoice_totals.get(customer),
                "after": after.get(customer),
            }
            for customer in sorted(set(self.invoice_totals) | set(after))
        }
        return _check(
            "open_invoice_totals",
            all(value["before"] == value["after"] for value in customers.values()),
            customers=customers,
        )

    def _check_references(self, stale: _Stale) -> dict:
        rows = defaultdict(list)
        resource_ids = self.resource_ids

        def report(model, qs):
            label = model._meta.label
            pks = [
                pk
                for pk in qs.order_by("pk").values_list("pk", flat=True)
                if pk not in stale.allowed.get(label, ())
            ]
            if pks:
                rows[label].extend(pks)

        report(
            models.Resource,
            models.Resource.objects.filter(pk__in=resource_ids).filter(
                django_models.Q(offering_id__in=stale.offering_ids)
                | django_models.Q(plan_id__in=stale.plan_ids)
            ),
        )
        report(
            models.ResourcePlanPeriod,
            models.ResourcePlanPeriod.objects.filter(
                resource_id__in=resource_ids, plan_id__in=stale.plan_ids
            ),
        )
        report(
            models.Order,
            models.Order.objects.filter(resource_id__in=resource_ids).filter(
                django_models.Q(offering_id__in=stale.offering_ids)
                | django_models.Q(plan_id__in=stale.plan_ids)
                | django_models.Q(old_plan_id__in=stale.plan_ids)
            ),
        )
        for model in _USAGE_MODELS:
            report(
                model,
                model._base_manager.filter(
                    resource_id__in=resource_ids,
                    component_id__in=stale.component_ids,
                ),
            )

        for pk, limits, current_usages in models.Resource.objects.filter(
            pk__in=resource_ids
        ).values_list("pk", "limits", "current_usages"):
            keys = stale.json_keys.get(pk, set())
            if any(
                isinstance(value, dict) and keys & set(value)
                for value in (limits, current_usages)
            ):
                rows["marketplace.Resource (JSON keys)"].append(pk)

        _, invoice_item_model = _invoice_models()
        for pk, pc_id, details in (
            _invoice_items_in_scope(resource_ids, self.policy)
            .exclude(pk__in=stale.ignored_invoice_items)
            .order_by("pk")
            .values_list("pk", "plan_component_id", "details")
        ):
            details = details if isinstance(details, dict) else {}
            if (
                pc_id in stale.plan_component_ids
                or _as_int(details.get("plan_component_id")) in stale.plan_component_ids
                or _hex(details.get("offering_uuid")) in stale.offering_uuids
                or _hex(details.get("plan_uuid")) in stale.plan_uuids
            ):
                rows[invoice_item_model._meta.label].append(pk)

        return _check(
            "no_stale_references",
            not rows,
            rows={
                label: pks[:MAX_REPORTED_ROWS] for label, pks in sorted(rows.items())
            },
            counts={label: len(pks) for label, pks in sorted(rows.items())},
        )

    def _check_offering_roles(self) -> dict:
        """The merge never moves or copies offering-scoped roles.

        The registry keeps them on the sources (``keep_on_source``): copying
        them would hand the source's managers the target. So the check asserts
        that no offering's active role count changed, and reports how many
        active roles stay on the (archived) sources as information.
        """
        after = self._offering_roles()
        source_ids = set(self.merge.sources.values_list("pk", flat=True))
        return _check(
            "offering_roles_unchanged",
            after == self.roles,
            offerings={
                self.offering_uuids[pk].hex: {
                    "before": self.roles.get(pk, 0),
                    "after": after.get(pk, 0),
                }
                for pk in self.offering_ids
            },
            kept_on_sources=sum(
                count for pk, count in after.items() if pk in source_ids
            ),
        )

    def _check_closed_items(self) -> dict:
        if self.closed_items is None:
            return _check(
                "closed_invoice_items_unchanged",
                True,
                applicable=False,
                policy=self.policy,
            )
        after = self._closed_item_hashes()
        changed = sorted(
            pk for pk, digest in self.closed_items.items() if after.get(pk) != digest
        )
        return _check(
            "closed_invoice_items_unchanged",
            not changed,
            applicable=True,
            checked=len(self.closed_items),
            changed=changed[:MAX_REPORTED_ROWS],
        )

    def verify(self, stale: _Stale) -> dict:
        checks = [
            self._check_resource_counts(),
            self._check_plan_periods(),
            self._check_invoice_totals(),
            self._check_references(stale),
            self._check_offering_roles(),
            self._check_closed_items(),
        ]
        return {
            "passed": all(check["passed"] for check in checks),
            "checked_at": timezone.now().isoformat(),
            "checks": checks,
        }


def _store_verification(merge: models.OfferingMerge, stage: str, report: dict):
    """Keep the report of each stage; ``passed`` follows the latest one."""
    verification = dict(merge.verification or {})
    verification[stage] = report
    verification["stage"] = stage
    verification["passed"] = report["passed"]
    merge.verification = verification
    if report["passed"]:
        logger.info("Offering merge %s %s verification passed.", merge.uuid.hex, stage)
    else:
        logger.warning(
            "Offering merge %s %s verification failed: %s.",
            merge.uuid.hex,
            stage,
            ", ".join(
                check["code"] for check in report["checks"] if not check["passed"]
            ),
        )


def _archive_sources(merge: models.OfferingMerge, source_ids: list[int]):
    """Archive the sources through the FSM, journalling their previous state.

    A regular save, unlike every other write, so that the offering state
    handlers (category offering counts, service settings) see the archive just
    as they do when staff archive an offering by hand.
    """
    for source in models.Offering.objects.filter(pk__in=source_ids).order_by("pk"):
        previous = source.state
        if previous == OfferingStates.ARCHIVED:
            continue
        source.archive()
        source.save(update_fields=["state"])
        _journal(merge, models.Offering, "state", [(source.pk, previous, source.state)])


class _Progress:
    """Execution progress, stored on ``OfferingMerge.progress`` for polling.

    The merge runs in one transaction, so a progress row written through it
    would stay invisible until the merge ends. Each report is therefore
    written through a second database connection in autocommit mode and is
    visible at once. The row is not locked by the merge's transaction (the
    journal's foreign keys take only a key-share lock), so the update does not
    wait. A failed report is logged and never fails the merge. If the merge
    rolls back, the last report stays: it names the step that failed.
    """

    def __init__(self, merge: models.OfferingMerge, steps: list[tuple[str, int]]):
        self.merge = merge
        self.steps_total = len(steps)
        self.rows_total = sum(rows for _name, rows in steps)
        self.steps_done = 0
        self.rows_done = 0
        self.step = ""
        self._connection = None

    def snapshot(self) -> dict:
        return {
            "step": self.step,
            "steps_done": self.steps_done,
            "steps_total": self.steps_total,
            "rows_done": self.rows_done,
            "rows_total": self.rows_total,
            "updated_at": timezone.now().isoformat(),
        }

    def start(self, step: str):
        self.step = step
        self._report()

    def finish(self, rows: int):
        self.steps_done += 1
        self.rows_done += rows

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _report(self):
        try:
            if self._connection is None:
                self._connection = connections.create_connection(DEFAULT_DB_ALIAS)
            _write_progress(self._connection, self.merge.pk, self.snapshot())
        except Exception:
            logger.warning(
                "Could not report the progress of offering merge %s.",
                self.merge.uuid.hex,
                exc_info=True,
            )


def _write_progress(connection, merge_id: int, progress: dict):
    """Write ``progress`` through ``connection``, committing at once."""
    table = connection.ops.quote_name(models.OfferingMerge._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table} SET progress = %s::jsonb WHERE id = %s",  # noqa: S608
            [json.dumps(progress), merge_id],
        )


def execute(merge: models.OfferingMerge) -> models.OfferingMerge:
    """Run a previewed or queued merge. Raises ``OfferingMergeError`` if refused.

    The API queues a merge (``previewed`` to ``queued``) before handing it to
    a Celery task, so that a second request is refused; this function then
    moves it to ``running``. A shell caller may pass a ``previewed`` merge.

    On any failure the merge's writes are rolled back and the record moves to
    ``failed`` with the error.
    """
    with transaction.atomic():
        merge = models.OfferingMerge.objects.select_for_update().get(pk=merge.pk)
        try:
            merge.set_running()
        except TransitionNotAllowed:
            raise OfferingMergeError(f"Cannot execute a merge in state {merge.state}.")
        merge.save(update_fields=["state", "modified"])

    try:
        with transaction.atomic():
            _lock_offerings(merge)
            context = _MergeContext(merge)
            current = context.preview()
            if current != merge.preview:
                raise OfferingMergeError(
                    "The merge changed since it was previewed; preview it again.",
                    details={"previewed": merge.preview, "current": current},
                )
            if context.blockers:
                raise OfferingMergeError(
                    "The merge has blockers.", details=context.blockers
                )
            verification = _Verification(
                merge,
                {
                    resource_id: (offering_id, context.target.id)
                    for resource_id, offering_id in context.resource_offering.items()
                },
                [*context.source_ids, context.target.id],
                merge.invoice_policy,
            )
            steps = [
                (write.entry.label, len(write.rows), partial(_apply, merge, write))
                for write in context.writes
            ]
            steps += [
                (
                    "invoice_snapshots",
                    sum(len(write.rows) for write in context.invoice_writes),
                    partial(rewrite_invoice_snapshots, merge, context),
                ),
                (
                    "archive_sources",
                    len(context.source_ids),
                    partial(_archive_sources, merge, context.source_ids),
                ),
                # After the rewrite: historical LIMIT allocations follow
                # InvoiceItem.plan_component.
                (
                    "recompute_summaries",
                    0,
                    partial(_recompute_summaries, *context.summary_scope),
                ),
                (
                    "recalculate_counters",
                    0,
                    partial(
                        _recalculate_counters,
                        [*context.source_ids, context.target.id],
                    ),
                ),
            ]
            progress = _Progress(merge, [(name, rows) for name, rows, _run in steps])
            try:
                for name, rows, run in steps:
                    progress.start(name)
                    run()
                    progress.finish(rows)
                progress.start("verification")
            finally:
                progress.close()
            report = verification.verify(context.stale())
            report["invoice_items"] = {
                **context.invoice_summary,
                "rewritten": context.invoice_summary.get("to_rewrite", 0),
                # Undo moves back only items created after this one.
                "last_item_id": _invoice_models()[1]
                .objects.order_by("-pk")
                .values_list("pk", flat=True)
                .first()
                or 0,
            }
            _store_verification(merge, "execute", report)
            progress.step = "done"
            merge.progress = progress.snapshot()
            merge.set_done()
            merge.save(update_fields=["state", "verification", "progress", "modified"])
    except Exception as error:
        logger.exception("Offering merge %s failed.", merge.uuid.hex)
        with transaction.atomic():
            merge = models.OfferingMerge.objects.select_for_update().get(pk=merge.pk)
            merge.set_failed()
            merge.error_message = str(error)
            merge.save(update_fields=["state", "error_message", "modified"])
        raise
    logger.info(
        "Offering merge %s moved %s into %s.",
        merge.uuid.hex,
        merge.preview.get("sources"),
        merge.preview.get("target"),
    )
    return merge


# Usage and quota rows a moved resource may accumulate on the target's components.
_USAGE_MODELS = (
    models.ComponentUsage,
    models.ComponentQuota,
    models.ComponentUsagePollRecord,
    models.ComponentUserUsageLimit,
)


def _unique_attnames(model) -> tuple[str, ...]:
    entry = coverage.MERGE_COVERAGE[f"{model._meta.label}.component"]
    return tuple(model._meta.get_field(name).attname for name in entry.unique_with)


def _json_fields() -> dict[tuple[str, str], coverage.CoverageEntry]:
    return {
        (
            entry.model._meta.label,
            entry.model._meta.get_field(entry.field_name).attname,
        ): (entry)
        for entry in coverage.MERGE_COVERAGE.values()
        if entry.kind == coverage.JSON_KEYS
    }


class _UndoPlan:
    """What undoing a merge writes, and why it may be refused. Reads only.

    Foreign keys the merge wrote are restored from the journal and must still
    hold the value the merge wrote. JSON documents are not restored from the
    journal: the inverse key rename is applied to their current value, so limit
    changes and usage reports made since the merge survive. Usage, quota and
    order rows created since the merge for the moved resources are moved back
    to the source along with them.
    """

    def __init__(self, merge: models.OfferingMerge, changes):
        self.merge = merge
        self.blockers: list[dict] = []
        json_fields = _json_fields()
        self.journal = defaultdict(dict)  # (label, attname) -> {pk: change}
        for change in changes:
            self.journal[(change.model, change.field)][change.object_id] = change
        invoice_item_label = _invoice_models()[1]._meta.label
        # Invoice items are restored by their own rules (_plan_invoice_items).
        self.fk_changes = [
            change
            for change in changes
            if (change.model, change.field) not in json_fields
            and change.model != invoice_item_label
        ]

        resource_label = models.Resource._meta.label
        # Moved resource id -> its source offering id.
        self.resource_source = {
            pk: change.old_value
            for pk, change in self.journal[(resource_label, "offering_id")].items()
        }
        # Moved resource id -> (source plan id, target plan id).
        self.resource_plan = {
            pk: (change.old_value, change.new_value)
            for pk, change in self.journal[(resource_label, "plan_id")].items()
        }
        self._build_inverse_mappings()

        self._check_fk_drift()
        self._check_pending_orders()
        self.usage_moves = {
            model: self._plan_usage_moves(model) for model in _USAGE_MODELS
        }
        self.order_moves = self._plan_order_moves()
        self.json_writes = [self._plan_json(entry) for entry in json_fields.values()]
        self._plan_invoice_items()

    def _build_inverse_mappings(self):
        """Per source offering: target type -> source type, target -> source component."""
        source_ids = list(self.merge.sources.values_list("pk", flat=True))
        sources_by_uuid = {
            uuid.hex: pk
            for pk, uuid in models.Offering.objects.filter(
                pk__in=source_ids
            ).values_list("pk", "uuid")
        }
        target_components = dict(
            models.OfferingComponent.objects.filter(
                offering_id=self.merge.target_id
            ).values_list("type", "pk")
        )
        source_components = {
            (offering_id, component_type): pk
            for pk, offering_id, component_type in models.OfferingComponent.objects.filter(
                offering_id__in=source_ids
            ).values_list("pk", "offering_id", "type")
        }
        self.target_component_ids = set(target_components.values())
        self.source_component_ids = set(source_components.values())
        self.inverse_types = defaultdict(dict)
        self.inverse_components = defaultdict(dict)
        raw = self.merge.component_mapping
        for key, mapping in (raw if isinstance(raw, dict) else {}).items():
            source_id = sources_by_uuid.get(_hex(key))
            if source_id is None or not isinstance(mapping, dict):
                continue
            for source_type, target_type in mapping.items():
                source_component = source_components.get((source_id, source_type))
                target_component = target_components.get(target_type)
                if source_component is None or target_component is None:
                    continue
                self.inverse_types[source_id][target_type] = source_type
                self.inverse_components[source_id][target_component] = source_component
        self.inverse_keys = {new: old for old, new in _key_mapping(self.merge).items()}

    def _check_fk_drift(self):
        groups = defaultdict(list)
        for change in self.fk_changes:
            groups[(change.model, change.field)].append(change)
        drifted = defaultdict(list)
        for (label, attname), group in groups.items():
            model = apps.get_model(label)
            current = dict(
                model._base_manager.filter(
                    pk__in=[change.object_id for change in group]
                ).values_list("pk", attname)
            )
            for change in group:
                if (
                    change.object_id not in current
                    or _json_safe(current[change.object_id]) != change.new_value
                ):
                    drifted[(label, attname)].append(change.object_id)

        resource_label = models.Resource._meta.label
        moved = sorted(
            {
                pk
                for key in (
                    (resource_label, "offering_id"),
                    (resource_label, "plan_id"),
                )
                for pk in drifted.pop(key, [])
            }
        )
        if moved:
            self.blockers.append(
                _issue(
                    "resource_changed",
                    "Moved resources switched plan or offering since the merge.",
                    resources=sorted(
                        resource_uuid.hex
                        for resource_uuid in models.Resource.objects.filter(
                            pk__in=moved
                        ).values_list("uuid", flat=True)
                    ),
                )
            )
        for (label, attname), pks in sorted(drifted.items()):
            self.blockers.append(
                _issue(
                    "changed_since_merge",
                    f"{len(pks)} {label} row(s) no longer hold the {attname} the "
                    "merge wrote.",
                    model=label,
                    field=attname,
                    rows=sorted(pks),
                )
            )

    def _check_pending_orders(self):
        pending = list(
            models.Order.objects.filter(
                resource_id__in=list(self.resource_source),
                state__in=OrderStates.PENDING_STATES,
            ).values_list("uuid", flat=True)
        )
        if pending:
            self.blockers.append(
                _issue(
                    "pending_orders",
                    f"{len(pending)} order(s) of moved resources are pending.",
                    orders=sorted(order_uuid.hex for order_uuid in pending),
                )
            )

    def _unmappable(self, label, pks, reason):
        self.blockers.append(
            _issue(
                "unmappable_new_row",
                f"{len(pks)} {label} row(s) created since the merge cannot be "
                f"moved back: {reason}",
                model=label,
                rows=sorted(pks),
            )
        )

    def _plan_usage_moves(self, model) -> dict[int, int]:
        """New rows on target components -> the mapped source component."""
        label = model._meta.label
        unique = _unique_attnames(model)
        journalled = self.journal[(label, "component_id")]
        moved_resources = list(self.resource_source)
        rows = list(
            model._base_manager.filter(
                resource_id__in=moved_resources,
                component_id__in=self.target_component_ids | self.source_component_ids,
            ).values_list("pk", "resource_id", "component_id", *unique)
        )
        moves, unmappable = {}, []
        final_keys = Counter()
        for pk, resource_id, component_id, *unique_values in rows:
            if pk in journalled:
                component_id = journalled[pk].old_value
            elif component_id in self.target_component_ids:
                source_component = self.inverse_components[
                    self.resource_source[resource_id]
                ].get(component_id)
                if source_component is None:
                    unmappable.append(pk)
                    continue
                moves[pk] = component_id = source_component
            final_keys[(component_id, *unique_values)] += 1
        if unmappable:
            self._unmappable(
                label, unmappable, "their component has no source counterpart."
            )
        conflicts = [key for key, count in final_keys.items() if count > 1]
        if conflicts:
            self.blockers.append(
                _issue(
                    "unique_conflict",
                    f"Moving {label} rows back would violate its unique constraint "
                    f"on (component, {', '.join(unique)}).",
                    model=label,
                    fields=["component_id", *unique],
                    count=len(conflicts),
                )
            )
        return moves

    def _plan_order_moves(self) -> dict[int, dict]:
        """New orders of moved resources on the target -> source offering and plans."""
        journalled = self.journal[(models.Order._meta.label, "offering_id")]
        rows = (
            models.Order.objects.filter(
                resource_id__in=list(self.resource_source),
                offering_id=self.merge.target_id,
            )
            .exclude(pk__in=list(journalled))
            .values_list("pk", "resource_id", "plan_id", "old_plan_id")
        )
        moves, unmappable = {}, []
        for pk, resource_id, plan_id, old_plan_id in rows:
            source_plan, target_plan = self.resource_plan.get(resource_id, (None, None))
            update = {"offering_id": self.resource_source[resource_id]}
            for attname, value in (("plan_id", plan_id), ("old_plan_id", old_plan_id)):
                if value is None:
                    continue
                if target_plan is None or value != target_plan:
                    unmappable.append(pk)
                    break
                update[attname] = source_plan
            else:
                moves[pk] = update
        if unmappable:
            self._unmappable(
                models.Order._meta.label,
                unmappable,
                "they use a target plan the resource was not moved to.",
            )
        return moves

    def _row_source(self, entry, pk, resource_id) -> int | None:
        if resource_id in self.resource_source:
            return self.resource_source[resource_id]
        # Rows not tied to a moved resource: use what the merge journalled.
        label = entry.model._meta.label
        change = self.journal[(label, "offering_id")].get(pk)
        if change is not None:
            return change.old_value
        change = self.journal[(label, "plan_id")].get(pk)
        if change is not None:
            return (
                models.Plan.objects.filter(pk=change.old_value)
                .values_list("offering_id", flat=True)
                .first()
            )
        return None

    def _plan_json(self, entry) -> _Write:
        model = entry.model
        attname = model._meta.get_field(entry.field_name).attname
        journalled = set(self.journal[(model._meta.label, attname)])
        manager = model._base_manager
        if entry.resource_path == "pk":
            qs = manager.filter(pk__in=list(self.resource_source))
            resource_field = "pk"
        elif entry.resource_path:
            qs = manager.filter(
                django_models.Q(
                    **{f"{entry.resource_path}__in": list(self.resource_source)}
                )
                | django_models.Q(pk__in=list(journalled))
            )
            resource_field = entry.resource_path
        else:
            qs = manager.filter(pk__in=list(journalled))
            resource_field = "pk"
        write = _Write(entry, model, attname, is_json=True)
        for pk, resource_id, value in qs.order_by("pk").values_list(
            "pk", resource_field, attname
        ):
            if entry.resource_path == "":
                resource_id = None
            source_id = self._row_source(entry, pk, resource_id)
            if source_id is None:
                continue
            types = self.inverse_types.get(source_id, {})
            if entry.rename == coverage.COMPONENT_TYPES:
                new = _rename_keys(value, types)
            else:
                new = _rename_answers(value, self.inverse_keys, types)
            if new != value:
                write.rows.append((pk, value, new))
        return write

    def _plan_invoice_items(self):
        """Restore rewritten invoice items; move new items of moved resources back.

        A rewritten item is restored exactly: ``plan_component`` from the
        journal, ``details`` by putting back the keys the merge rewrote (keys
        changed by billing since, such as ``resource_limit_periods``, stay).
        An item is skipped, and listed in the report, if it was deleted, if its
        invoice was closed since an ``open_month`` merge (restoring it would
        change an issued invoice), or if a rewritten value changed since.

        Items created for the moved resources since the merge, within the
        policy's scope, get the inverse rewrite, as usage rows do.
        """
        _, invoice_item_model = _invoice_models()
        label = invoice_item_model._meta.label
        details_changes = self.journal[(label, "details")]
        pc_changes = self.journal[(label, "plan_component_id")]
        journalled = set(details_changes) | set(pc_changes)
        open_month = (
            self.merge.invoice_policy != models.OfferingMerge.InvoicePolicies.ALL_MONTHS
        )
        mutable = _mutable_invoice_states()
        current = {
            pk: rest
            for pk, *rest in invoice_item_model.objects.filter(
                pk__in=list(journalled)
            ).values_list("pk", "plan_component_id", "details", "invoice__state")
        }
        self.invoice_restores = []  # (pk, {attname: value})
        self.invoice_skipped = []
        for pk in sorted(journalled):
            if pk not in current:
                self.invoice_skipped.append({"id": pk, "reason": "deleted"})
                continue
            pc_id, details, state = current[pk]
            if open_month and state not in mutable:
                self.invoice_skipped.append({"id": pk, "reason": "invoice_closed"})
                continue
            update = {}
            pc_change = pc_changes.get(pk)
            if pc_change is not None:
                if pc_id != pc_change.new_value:
                    self.invoice_skipped.append(
                        {"id": pk, "reason": "changed_since_merge"}
                    )
                    continue
                update["plan_component_id"] = pc_change.old_value
            details_change = details_changes.get(pk)
            if details_change is not None:
                restored = _restore_keys(
                    details, details_change.old_value, details_change.new_value
                )
                if restored is None:
                    self.invoice_skipped.append(
                        {"id": pk, "reason": "changed_since_merge"}
                    )
                    continue
                update["details"] = restored
            self.invoice_restores.append((pk, update))

        self.invoice_moves = []  # (pk, {attname: value})
        self.invoice_untouched = set()
        self._snapshot_maps = {}
        # Items that already existed at the merge are journalled or were left
        # alone on purpose; only later ones are moved back.
        last_item_id = (
            (self.merge.verification or {})
            .get("execute", {})
            .get("invoice_items", {})
            .get("last_item_id")
        )
        in_scope = _invoice_items_in_scope(
            self.resource_source, self.merge.invoice_policy
        )
        if last_item_id is None:
            self.invoice_untouched = set(
                in_scope.exclude(pk__in=list(journalled)).values_list("pk", flat=True)
            )
            return
        self.invoice_untouched = set(
            in_scope.filter(pk__lte=last_item_id)
            .exclude(pk__in=list(journalled))
            .values_list("pk", flat=True)
        )
        new_items = in_scope.filter(pk__gt=last_item_id)
        for pk, resource_id, pc_id, details in new_items.order_by("pk").values_list(
            "pk", "resource_id", "plan_component_id", "details"
        ):
            new_details, new_pc = _rewrite_snapshot(
                details, pc_id, self._inverse_snapshot_map(resource_id)
            )
            update = {}
            if new_pc != pc_id:
                update["plan_component_id"] = new_pc
            if new_details != details:
                update["details"] = new_details
            if update:
                self.invoice_moves.append((pk, update))

    def _inverse_snapshot_map(self, resource_id) -> _SnapshotMap:
        """How a new invoice item of a moved resource goes back to its source."""
        source_id = self.resource_source[resource_id]
        source_plan_id, target_plan_id = self.resource_plan.get(
            resource_id, (None, None)
        )
        key = (source_id, source_plan_id, target_plan_id)
        if key in self._snapshot_maps:
            return self._snapshot_maps[key]
        offerings = models.Offering.objects.in_bulk([source_id, self.merge.target_id])
        source, target = offerings[source_id], offerings[self.merge.target_id]
        plan_components, plans = {}, {}
        if source_plan_id and target_plan_id:
            source_pcs = {
                pc.component_id: pc
                for pc in models.PlanComponent.objects.filter(
                    plan_id=source_plan_id
                ).select_related("plan", "component")
            }
            inverse = self.inverse_components[source_id]
            for pc_id, component_id in models.PlanComponent.objects.filter(
                plan_id=target_plan_id
            ).values_list("pk", "component_id"):
                source_pc = source_pcs.get(inverse.get(component_id))
                if source_pc is not None:
                    plan_components[pc_id] = source_pc
            plan_objects = models.Plan.objects.in_bulk([source_plan_id, target_plan_id])
            plans = {
                plan_objects[target_plan_id].uuid.hex: plan_objects[source_plan_id]
            }
        source_components = {
            component.type: component
            for component in models.OfferingComponent.objects.filter(
                offering_id=source_id
            )
        }
        mapping = _SnapshotMap(
            offering_uuids=frozenset({target.uuid.hex}),
            offering=_offering_snapshot(source),
            provider_changes=source.customer_id != target.customer_id,
            plan_components=plan_components,
            plans=plans,
            component_types={
                target_type: source_components[source_type]
                for target_type, source_type in self.inverse_types[source_id].items()
                if source_type in source_components
            },
        )
        self._snapshot_maps[key] = mapping
        return mapping

    def invoice_report(self) -> dict:
        return {
            "policy": self.merge.invoice_policy,
            "restored": len(self.invoice_restores),
            "moved_back": len(self.invoice_moves),
            "skipped": self.invoice_skipped,
        }

    def stale(self) -> _Stale:
        """What the moved resources must no longer reference once undone."""
        target_plan_ids = {
            target_plan for _source_plan, target_plan in self.resource_plan.values()
        }
        target_component_ids = {
            component_id
            for mapping in self.inverse_components.values()
            for component_id in mapping
        }
        source_types = defaultdict(set)
        for offering_id, component_type in models.OfferingComponent.objects.filter(
            offering_id__in=set(self.resource_source.values())
        ).values_list("offering_id", "type"):
            source_types[offering_id].add(component_type)
        renamed = {
            source_id: {
                target_type
                for target_type, source_type in mapping.items()
                if target_type != source_type
            }
            - source_types[source_id]
            for source_id, mapping in self.inverse_types.items()
        }
        target_uuid = (
            models.Offering.objects.filter(pk=self.merge.target_id)
            .values_list("uuid", flat=True)
            .get()
        )
        return _Stale(
            offering_ids={self.merge.target_id},
            plan_ids=target_plan_ids,
            component_ids=target_component_ids,
            plan_component_ids=set(
                models.PlanComponent.objects.filter(
                    plan_id__in=target_plan_ids,
                    component_id__in=target_component_ids,
                ).values_list("pk", flat=True)
            ),
            offering_uuids={target_uuid.hex},
            plan_uuids={
                plan_uuid.hex
                for plan_uuid in models.Plan.objects.filter(
                    pk__in=target_plan_ids
                ).values_list("uuid", flat=True)
            },
            json_keys={
                resource_id: renamed.get(source_id, set())
                for resource_id, source_id in self.resource_source.items()
            },
            ignored_invoice_items={item["id"] for item in self.invoice_skipped}
            | self.invoice_untouched,
        )

    def apply(self):
        # Foreign keys and offering states, newest first.
        offering_label = models.Offering._meta.label
        grouped = defaultdict(list)
        for change in self.fk_changes:
            if change.model == offering_label and change.field == "state":
                # Symmetric with _archive_sources: state handlers must see it.
                offering = models.Offering.objects.get(pk=change.object_id)
                offering.state = change.old_value
                offering.save(update_fields=["state"])
                continue
            grouped[(change.model, change.field, change.old_value)].append(
                change.object_id
            )
        for (label, attname, old_value), pks in grouped.items():
            manager = apps.get_model(label)._base_manager
            for start in range(0, len(pks), BATCH_SIZE):
                manager.filter(pk__in=pks[start : start + BATCH_SIZE]).update(
                    **{attname: old_value}
                )

        for model, moves in self.usage_moves.items():
            by_component = defaultdict(list)
            for pk, component_id in moves.items():
                by_component[component_id].append(pk)
            for component_id, pks in by_component.items():
                model._base_manager.filter(pk__in=pks).update(component_id=component_id)

        for pk, update in self.order_moves.items():
            models.Order.objects.filter(pk=pk).update(**update)

        for write in self.json_writes:
            for pk, _old, new in write.rows:
                write.model._base_manager.filter(pk=pk).update(**{write.attname: new})

        manager = _invoice_models()[1]._base_manager
        for pk, update in [*self.invoice_restores, *self.invoice_moves]:
            manager.filter(pk=pk).update(**update)


def undo_blockers(merge: models.OfferingMerge) -> list[dict]:
    """Why undoing ``merge`` would be refused right now. Reads only.

    The API calls this before queueing an undo, so that a refusal is answered
    synchronously; :func:`undo` checks again under lock.
    """
    return _json_safe(_UndoPlan(merge, list(merge.changes.order_by("-id"))).blockers)


def _refusal_message(error: Exception) -> str:
    details = getattr(error, "details", None)
    if isinstance(details, list):
        codes = sorted(
            {
                item["code"]
                for item in details
                if isinstance(item, dict) and "code" in item
            }
        )
        if codes:
            return f"{error} ({', '.join(codes)})"
    return str(error)


def undo(merge: models.OfferingMerge) -> models.OfferingMerge:
    """Move everything a completed merge moved back to its sources.

    Accepts a ``done`` merge, or one the API moved to ``undoing`` before
    handing it to a Celery task.

    Raises ``OfferingMergeError`` without writing anything if a moved resource
    switched plan or offering since, a foreign key the merge wrote holds another
    value, a moved resource has a pending order, or rows created since the
    merge cannot be moved back. An ``undoing`` merge then returns to ``done``
    with the reason in ``error_message``: the merge is still in effect.

    Rewritten invoice items are restored exactly unless deleted, changed since,
    or (under ``open_month``) on an invoice closed since; those are skipped and
    listed in ``verification["undo"]["invoice_items"]["skipped"]``.
    """
    try:
        return _undo(merge)
    except Exception as error:
        with transaction.atomic():
            current = models.OfferingMerge.objects.select_for_update().get(pk=merge.pk)
            if current.state == models.OfferingMerge.States.UNDOING:
                current.set_undo_refused()
                current.error_message = _refusal_message(error)
                current.save(update_fields=["state", "error_message", "modified"])
        raise


def _undo(merge: models.OfferingMerge) -> models.OfferingMerge:
    States = models.OfferingMerge.States
    with transaction.atomic():
        merge = models.OfferingMerge.objects.select_for_update().get(pk=merge.pk)
        if merge.state not in (States.DONE, States.UNDOING):
            raise OfferingMergeError(f"Cannot undo a merge in state {merge.state}.")
        _lock_offerings(merge)
        plan = _UndoPlan(merge, list(merge.changes.order_by("-id")))
        if plan.blockers:
            raise OfferingMergeError(
                "The merge cannot be undone.", details=_json_safe(plan.blockers)
            )
        scope = _summary_scope(
            list(merge.sources.values_list("pk", flat=True)),
            merge.target_id,
            list(plan.resource_source),
        )
        source_ids = list(merge.sources.values_list("pk", flat=True))
        verification = _Verification(
            merge,
            {
                resource_id: (merge.target_id, source_id)
                for resource_id, source_id in plan.resource_source.items()
            },
            [*source_ids, merge.target_id],
            merge.invoice_policy,
        )
        plan.apply()
        # After the invoice restore: historical LIMIT allocations follow
        # InvoiceItem.plan_component.
        _recompute_summaries(*scope)
        _recalculate_counters([*source_ids, merge.target_id])
        report = verification.verify(plan.stale())
        report["invoice_items"] = plan.invoice_report()
        _store_verification(merge, "undo", report)
        try:
            merge.set_undone()
        except TransitionNotAllowed:
            raise OfferingMergeError(f"Cannot undo a merge in state {merge.state}.")
        merge.error_message = ""
        merge.save(update_fields=["state", "verification", "error_message", "modified"])
    return merge
