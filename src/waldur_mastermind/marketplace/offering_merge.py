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
  as an ``OfferingMergeChange``.
* :func:`undo` restores the foreign keys from the journal, applies the
  inverse key renames to JSON documents as they are now, and moves rows
  created for the moved resources since the merge back with them. It refuses
  if a moved resource switched plan or offering, or a row cannot go back.

``ComponentUsageMonthly`` is derived, so both directions recompute it for the
affected components and months instead of journalling it; the offering counter
quotas the bypassed signals would have maintained are recounted as well.

The invoice snapshot rewrite (``rewrite_snapshot`` entries) is a placeholder
here: invoice items are left untouched.
"""

import json
import logging
import uuid as uuid_lib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models
from django.db import transaction
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

    def __init__(self, merge: models.OfferingMerge):
        self.merge = merge
        self.target = models.Offering.objects.get(pk=merge.target_id)
        self.sources = list(
            models.Offering.objects.filter(
                pk__in=merge.sources.values_list("pk", flat=True)
            ).order_by("id")
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

        self.resource_ids = list(
            models.Resource.objects.filter(offering_id__in=self.source_ids)
            .order_by("pk")
            .values_list("pk", flat=True)
        )

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
        for offering in [*self.sources, self.target]:
            if offering.type == REMOTE_OFFERING:
                self.blockers.append(
                    _issue(
                        "remote_offering",
                        f"Offering {offering.name} is a remote offering.",
                        offering=offering.uuid.hex,
                    )
                )
            if offering.parent_id or offering.children.exists():
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
                "blockers": self.blockers,
                "warnings": self.warnings,
            }
        )


def build_preview(merge: models.OfferingMerge) -> dict:
    """Compute the preview of ``merge`` without writing anything."""
    return _MergeContext(merge).preview()


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
    """Rewrite invoice item snapshots per ``merge.invoice_policy``.

    Placeholder: a merge leaves every ``InvoiceItem`` untouched for now;
    ``context.plan_component_map`` carries the source-to-target plan component
    mapping the policy will need.
    """


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


def execute(merge: models.OfferingMerge) -> models.OfferingMerge:
    """Run a previewed merge. Raises ``OfferingMergeError`` if it was refused.

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
            for write in context.writes:
                _apply(merge, write)
            rewrite_invoice_snapshots(merge, context)
            _archive_sources(merge, context.source_ids)
            _recompute_summaries(*context.summary_scope)
            _recalculate_counters([*context.source_ids, context.target.id])
            merge.set_done()
            merge.save(update_fields=["state", "modified"])
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
        self.fk_changes = [
            change
            for change in changes
            if (change.model, change.field) not in json_fields
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


def undo(merge: models.OfferingMerge) -> models.OfferingMerge:
    """Move everything a completed merge moved back to its sources.

    Raises ``OfferingMergeError`` without writing anything if a moved resource
    switched plan or offering since, a foreign key the merge wrote holds another
    value, a moved resource has a pending order, or rows created since the
    merge cannot be moved back.
    """
    with transaction.atomic():
        merge = models.OfferingMerge.objects.select_for_update().get(pk=merge.pk)
        if merge.state != models.OfferingMerge.States.DONE:
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
        plan.apply()
        _recompute_summaries(*scope)
        _recalculate_counters(
            [*merge.sources.values_list("pk", flat=True), merge.target_id]
        )
        try:
            merge.set_undone()
        except TransitionNotAllowed:
            raise OfferingMergeError(f"Cannot undo a merge in state {merge.state}.")
        merge.save(update_fields=["state", "modified"])
    return merge
