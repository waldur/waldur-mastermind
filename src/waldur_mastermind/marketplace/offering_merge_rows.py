"""Which rows an offering merge changes, described for a human reader.

The merge engine plans its writes as ``(pk, old value, new value)`` triples
over model labels and column attnames. That is what the executor needs and
what the journal records, but it tells staff confirming a merge nothing: a
list of invoice item ids and plan component ids is not an answer to "which
five invoice lines, on whose invoices, for how much".

This module turns those triples into rows staff can read. Two sources feed it,
and both produce the same shape:

* a merge that has not run yet — the engine recomputes its plan and the rows
  are what it would write;
* a merge that has run — the journal, so the same lists can be shown
  afterwards, including after an undo.

Describing a page of rows must cost a bounded number of queries, so every
describer is written over a page at a time: one query for the objects with
their relations joined, one per related model for the names that old and new
values resolve to. Nothing here reads a row at a time, and nothing here
writes.
"""

import json
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.db import models as django_models

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace import offering_merge_coverage as coverage
from waldur_mastermind.marketplace.offering_merge import (
    planned_rows,
    resolve_relation,
)

# A rendered value is a label for a human, not a document; a long JSON diff is
# truncated rather than pushed at the UI in full.
MAX_VALUE_LENGTH = 200


@dataclass
class AffectedRow:
    """One row an offering merge changes, or deliberately leaves alone."""

    id: int
    uuid: str | None
    model: str
    field: str
    description: str
    old_value: str | None
    new_value: str | None
    kept_on_source: bool


def _truncate(text: str) -> str:
    if len(text) <= MAX_VALUE_LENGTH:
        return text
    return text[: MAX_VALUE_LENGTH - 1] + "…"


def _format(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


# Suffixes of keys that identify something rather than describe it.
IDENTIFIER_SUFFIXES = ("_uuid", "_id")


def _name_sibling(key: str, changed: set) -> str | None:
    """The changed key that already names what ``key`` identifies, if any.

    A snapshot carries a name and an id for the same thing, and a merge rewrites
    both: ``plan_name`` beside ``plan_uuid``, ``offering_name`` beside
    ``offering_uuid``. The name answers the reader's question, so the identifier
    is noise — but only while the name is there to replace it.

    The search widens by dropping one segment at a time, because an identifier
    is often more specific than the name that covers it: the plan component id
    changes precisely because the plan changed, and ``plan_name`` says so.
    """
    for suffix in IDENTIFIER_SUFFIXES:
        if not key.endswith(suffix):
            continue
        stem = key[: -len(suffix)]
        while stem:
            name = f"{stem}_name"
            if name in changed:
                return name
            stem = stem.rpartition("_")[0]
    return None


def _render_document(document, other) -> str | None:
    """A JSON value rendered as the keys that differ from ``other``, named.

    An invoice item's ``details`` snapshot holds a dozen keys of which a merge
    rewrites five; showing the whole document twice would bury them, and
    showing every rewritten key buries the two that mean anything under the
    uuids and row ids beside them.

    Which keys differ is decided over both documents, so the two sides of one
    change hide and show the same keys.
    """
    if not isinstance(document, dict):
        return None if document is None else _truncate(_format(document))
    other = other if isinstance(other, dict) else {}
    missing = object()
    changed = {
        key
        for key in set(document) | set(other)
        if document.get(key, missing) != other.get(key, missing)
    }
    shown = sorted(
        key
        for key in changed
        if key in document and _name_sibling(key, changed) is None
    )
    if not shown:
        return ""
    return _truncate(", ".join(f"{key}: {_format(document[key])}" for key in shown))


# --- Names for the values ----------------------------------------------------


def _offering_names(ids) -> dict[Any, str]:
    return {
        offering.pk: offering.name
        for offering in models.Offering._base_manager.filter(pk__in=ids)
    }


def _offering_names_by_uuid(uuids) -> dict[Any, str]:
    # A stored uuid comes back as a UUID from a UUIDField and as a string from
    # a CharField; both spellings key the same name.
    names = {}
    for offering in models.Offering._base_manager.filter(uuid__in=uuids):
        names[offering.uuid] = offering.name
        names[offering.uuid.hex] = offering.name
    return names


def _plan_names(ids) -> dict[Any, str]:
    return {
        plan.pk: f"{plan.name} ({plan.offering.name})"
        for plan in models.Plan._base_manager.filter(pk__in=ids).select_related(
            "offering"
        )
    }


def _component_names(ids) -> dict[Any, str]:
    return {
        component.pk: f"{component.type} ({component.offering.name})"
        for component in models.OfferingComponent._base_manager.filter(
            pk__in=ids
        ).select_related("offering")
    }


def _plan_component_names(ids) -> dict[Any, str]:
    return {
        plan_component.pk: (
            f"{plan_component.component.type} ({plan_component.plan.name})"
        )
        for plan_component in models.PlanComponent._base_manager.filter(
            pk__in=ids
        ).select_related("plan", "component")
    }


_NAME_RESOLVERS = {
    models.Offering: _offering_names,
    models.Plan: _plan_names,
    models.OfferingComponent: _component_names,
    models.PlanComponent: _plan_component_names,
}


def _value_names(entry: coverage.CoverageEntry, values) -> dict[Any, str]:
    """Primary key (or stored uuid) -> the name it stands for."""
    values = {value for value in values if value is not None}
    if not values:
        return {}
    if entry.kind == coverage.UUID:
        return _offering_names_by_uuid(values)
    if entry.kind == coverage.GENERIC:
        return _offering_names(values)
    related_model = resolve_relation(entry)[3]
    return _NAME_RESOLVERS[related_model](values)


# --- Describers --------------------------------------------------------------


def _period(year, month) -> str:
    return f"{year:04d}-{month:02d}"


def _detail(text: str, detail) -> str:
    """Append a distinguishing detail, when the row has one."""
    return f"{text}, {detail}" if detail else text


def _uuid_of(obj) -> str | None:
    """The object's uuid as hex, for the models that have one."""
    uuid = getattr(obj, "uuid", None)
    return uuid.hex if uuid is not None else None


def _user_name(user) -> str:
    return user.full_name or user.username


def _role_name(role) -> str:
    return role.description or role.name


def _describe_invoice_items(model, pks) -> dict[int, tuple[str | None, str]]:
    result = {}
    for item in model._base_manager.filter(pk__in=pks).select_related(
        "invoice", "invoice__customer", "resource", "plan_component__component"
    ):
        component = (
            item.plan_component.component.type
            if item.plan_component
            else (item.details or {}).get("offering_component_type") or item.name
        )
        parts = [
            f"Invoice {item.invoice.number}",
            item.invoice.customer.name,
            item.resource.name if item.resource else "",
            str(component),
            _period(item.invoice.year, item.invoice.month),
            f"{item.price}",
        ]
        result[item.pk] = (item.uuid.hex, ", ".join(part for part in parts if part))
    return result


def _describe_resources(model, pks) -> dict[int, tuple[str | None, str]]:
    return {
        resource.pk: (
            resource.uuid.hex,
            ", ".join(
                part
                for part in (
                    resource.name,
                    resource.project.name if resource.project else "",
                    resource.plan.name if resource.plan else "",
                )
                if part
            ),
        )
        for resource in model._base_manager.filter(pk__in=pks).select_related(
            "project", "plan"
        )
    }


def _describe_orders(model, pks) -> dict[int, tuple[str | None, str]]:
    return {
        order.pk: (
            order.uuid.hex,
            ", ".join(
                [
                    order.resource.name,
                    order.get_type_display(),
                    order.get_state_display(),
                    order.created.strftime("%Y-%m-%d"),
                ]
            ),
        )
        for order in model._base_manager.filter(pk__in=pks).select_related("resource")
    }


def _describe_component_usages(model, pks) -> dict[int, tuple[str | None, str]]:
    return {
        usage.pk: (
            usage.uuid.hex,
            ", ".join(
                [
                    usage.resource.name,
                    usage.component.type,
                    usage.billing_period.strftime("%Y-%m"),
                    f"{usage.usage}",
                ]
            ),
        )
        for usage in model._base_manager.filter(pk__in=pks).select_related(
            "resource", "component"
        )
    }


def _describe_offering_users(model, pks) -> dict[int, tuple[str | None, str]]:
    return {
        offering_user.pk: (
            offering_user.uuid.hex,
            ", ".join(
                part
                for part in (
                    offering_user.user.full_name or offering_user.user.username,
                    offering_user.username,
                    offering_user.offering.name,
                )
                if part
            ),
        )
        for offering_user in model._base_manager.filter(pk__in=pks).select_related(
            "user", "offering"
        )
    }


def _describe_plan_periods(model, pks) -> dict[int, tuple[str | None, str]]:
    result = {}
    for period in model._base_manager.filter(pk__in=pks).select_related(
        "resource", "plan"
    ):
        # A plan period is open at either end: not started, or still running.
        start = period.start.strftime("%Y-%m-%d") if period.start else "open"
        end = period.end.strftime("%Y-%m-%d") if period.end else "open"
        result[period.pk] = (
            period.uuid.hex,
            ", ".join([period.resource.name, period.plan.name, f"{start} to {end}"]),
        )
    return result


def _offering_owned(label: str):
    """A describer for a named thing that belongs to one offering.

    Most of the configuration a merge leaves on the archived source is shaped
    this way — a screenshot, a file, a plan — and differs only in the word for
    what it is.
    """

    def describe(model, pks) -> dict[int, tuple[str | None, str]]:
        return {
            obj.pk: (
                _uuid_of(obj),
                f"{label} {obj.name} of {obj.offering.name}"
                if obj.name
                else f"{label} of {obj.offering.name}",
            )
            for obj in model._base_manager.filter(pk__in=pks).select_related("offering")
        }

    return describe


def _describe_terms_of_service(model, pks) -> dict[int, tuple[str | None, str]]:
    return {
        terms.pk: (
            terms.uuid.hex,
            _detail(
                f"Terms of service for {terms.offering.name}",
                f"version {terms.version}" if terms.version else "",
            ),
        )
        for terms in model._base_manager.filter(pk__in=pks).select_related("offering")
    }


def _describe_offering_components(model, pks) -> dict[int, tuple[str | None, str]]:
    # Two offerings routinely carry components of the same name, and the type
    # is what the merge maps, so both belong in the description.
    return {
        component.pk: (
            component.uuid.hex,
            f"Component {component.name} ({component.type}) "
            f"of {component.offering.name}",
        )
        for component in model._base_manager.filter(pk__in=pks).select_related(
            "offering"
        )
    }


def _describe_plan_components(model, pks) -> dict[int, tuple[str | None, str]]:
    # A price-list row has no identity of its own: it is the price of one
    # component in one plan.
    return {
        plan_component.pk: (
            None,
            f"Price of "
            f"{plan_component.component.name if plan_component.component else '?'}"
            f" in plan {plan_component.plan.name}"
            f" of {plan_component.plan.offering.name}",
        )
        for plan_component in model._base_manager.filter(pk__in=pks).select_related(
            "component", "plan__offering"
        )
    }


def _describe_access_endpoints(model, pks) -> dict[int, tuple[str | None, str]]:
    return {
        endpoint.pk: (
            endpoint.uuid.hex,
            f"Access endpoint {endpoint.name} ({endpoint.url}) "
            f"of {endpoint.offering.name}",
        )
        for endpoint in model._base_manager.filter(pk__in=pks).select_related(
            "offering"
        )
    }


def _describe_component_quotas(model, pks) -> dict[int, tuple[str | None, str]]:
    return {
        quota.pk: (
            None,
            f"Quota of {quota.component.name} for {quota.resource.name}, "
            f"limit {quota.limit}",
        )
        for quota in model._base_manager.filter(pk__in=pks).select_related(
            "resource", "component"
        )
    }


def _describe_user_consents(model, pks) -> dict[int, tuple[str | None, str]]:
    return {
        consent.pk: (
            consent.uuid.hex,
            _detail(
                f"Consent of {_user_name(consent.user)} to the terms "
                f"of {consent.offering.name}",
                f"version {consent.version}" if consent.version else "",
            ),
        )
        for consent in model._base_manager.filter(pk__in=pks).select_related(
            "user", "offering"
        )
    }


def _scope_offerings(rows) -> dict[int, str]:
    """Offering id -> name for a page of rows scoped by a generic foreign key.

    One query for the page: the rows of an offering-scoped entry all point at
    an offering, so their object ids can be resolved together.
    """
    return {
        offering.pk: offering.name
        for offering in models.Offering._base_manager.filter(
            pk__in={row.object_id for row in rows}
        )
    }


def _scoped_on(text: str, offerings: dict, object_id) -> str:
    """Say which offering a generically scoped row applies to, when it is known."""
    name = offerings.get(object_id)
    return f"{text} on {name}" if name else text


def _describe_user_roles(model, pks) -> dict[int, tuple[str | None, str]]:
    rows = list(model._base_manager.filter(pk__in=pks).select_related("user", "role"))
    offerings = _scope_offerings(rows)
    return {
        row.pk: (
            _uuid_of(row),
            _scoped_on(
                f"{_role_name(row.role)} for {_user_name(row.user)}",
                offerings,
                row.object_id,
            ),
        )
        for row in rows
    }


def _describe_role_availabilities(model, pks) -> dict[int, tuple[str | None, str]]:
    rows = list(model._base_manager.filter(pk__in=pks).select_related("role"))
    offerings = _scope_offerings(rows)
    return {
        row.pk: (
            _uuid_of(row),
            _scoped_on(f"{_role_name(row.role)} offered", offerings, row.object_id),
        )
        for row in rows
    }


DESCRIBERS = {
    "invoices.InvoiceItem": _describe_invoice_items,
    "marketplace.Resource": _describe_resources,
    "marketplace.Order": _describe_orders,
    "marketplace.ComponentUsage": _describe_component_usages,
    "marketplace.OfferingUser": _describe_offering_users,
    "marketplace.ResourcePlanPeriod": _describe_plan_periods,
    "marketplace.OfferingTermsOfService": _describe_terms_of_service,
    "marketplace.Plan": _offering_owned("Plan"),
    "marketplace.OfferingComponent": _describe_offering_components,
    "marketplace.PlanComponent": _describe_plan_components,
    "marketplace.Screenshot": _offering_owned("Screenshot"),
    "marketplace.OfferingFile": _offering_owned("File"),
    "marketplace.OfferingAccessEndpoint": _describe_access_endpoints,
    "marketplace.ComponentQuota": _describe_component_quotas,
    "marketplace.UserOfferingConsent": _describe_user_consents,
    "permissions.UserRole": _describe_user_roles,
    "permissions.RoleAvailability": _describe_role_availabilities,
}


NAME_FIELDS = ("name", "title", "label")


def _name_field(model) -> str | None:
    """The field that holds what a human would call the row, if there is one."""
    for candidate in NAME_FIELDS:
        try:
            field = model._meta.get_field(candidate)
        except FieldDoesNotExist:
            continue
        if field.concrete and not field.is_relation:
            return candidate
    return None


def _foreign_keys(model):
    return [
        field
        for field in model._meta.get_fields()
        if field.concrete
        and (field.many_to_one or field.one_to_one)
        and field.related_model is not None
    ]


def _offering_path(model) -> str | None:
    """Lookup path from ``model`` to the offering that owns its rows.

    Directly when the model has an offering, otherwise one hop through another
    foreign key — a quota hangs off a resource, and the resource has the
    offering. One hop only: further than that the link stops meaning
    "belongs to".
    """
    if model is models.Offering:
        return None
    keys = _foreign_keys(model)
    for field in keys:
        if field.related_model is models.Offering:
            return field.name
    for field in keys:
        if field.related_model is model:
            continue
        for inner in _foreign_keys(field.related_model):
            if inner.related_model is models.Offering:
                return f"{field.name}__{inner.name}"
    return None


def _follow(obj, path: str | None):
    if not path:
        return None
    for step in path.split("__"):
        obj = getattr(obj, step, None)
        if obj is None:
            return None
    return obj


def _writes_own_str(model) -> bool:
    """Whether the model says what a row is, rather than inheriting the repr."""
    return model.__str__ is not django_models.Model.__str__


def describe_fallback(model, pks) -> dict[int, tuple[str | None, str]]:
    """The description of a model no describer covers, built from what it has.

    Chosen, not inherited by accident: the coverage registry lists some eighty
    entries over models that come and go with the installed plugins, so most of
    them will never be worth a hand-written description. They still have to
    read as something — ``OfferingTermsOfService object (2)`` tells staff
    confirming a merge nothing at all — so this assembles one out of what every
    Django model can offer: what the model is called, what the row is called,
    and which offering it belongs to.

    A model that writes its own ``__str__`` already says more than a verbose
    name can, so that wins over the generic wording; the uuid appears only when
    nothing else distinguishes the row.
    """
    name_field = _name_field(model)
    path = _offering_path(model)
    queryset = model._base_manager.filter(pk__in=pks)
    if path:
        queryset = queryset.select_related(path)
    verbose = str(model._meta.verbose_name).strip()
    verbose = verbose[:1].upper() + verbose[1:]

    result = {}
    for obj in queryset:
        uuid = _uuid_of(obj)
        name = str(getattr(obj, name_field, "") or "") if name_field else ""
        offering = _follow(obj, path)
        if name:
            text = f"{verbose} {name}"
        elif _writes_own_str(model):
            text = str(obj)
        else:
            text = verbose
        if offering is not None:
            text = f"{text} of {offering.name}"
        elif text == verbose:
            # Nothing so far tells one row from another.
            text = f"{verbose} ({uuid})" if uuid else f"{verbose} #{obj.pk}"
        result[obj.pk] = (uuid, text)
    return result


def describe_m2m_rows(through_model, owner_attname, pks) -> dict:
    """Describe the rows of an auto-created many-to-many table by their owner.

    A through row has no identity of its own — ``CustomerCredit_offerings
    object (3)`` names nothing — so it is described as the object that owns the
    relation: the credit, the template, the campaign.
    """
    owners = dict(
        through_model._base_manager.filter(pk__in=pks).values_list("pk", owner_attname)
    )
    owner_model = through_model._meta.get_field(
        owner_attname.removesuffix("_id")
    ).related_model
    described = describe_fallback(owner_model, set(owners.values()))
    return {
        pk: (None, described.get(owner_id, (None, str(owner_id)))[1])
        for pk, owner_id in owners.items()
    }


# --- Assembling the page -----------------------------------------------------


class AffectedRows:
    """The rows of one coverage entry, ready to paginate and then describe.

    ``items`` is what the paginator slices: a list for a merge that has not run
    and a queryset of journal rows for one that has. ``describe`` is given that
    page and only that page, which is what keeps the query count bounded.
    """

    def __init__(self, entry: coverage.CoverageEntry, model, attname: str, items):
        self.entry = entry
        self.model = model
        self.attname = attname
        self.items = items

    def describe(self, page) -> list[AffectedRow]:
        rows = [(pk, old, new) for pk, old, new in page]
        pks = [pk for pk, _old, _new in rows]
        described = self._describe_objects(pks)
        # A document is rendered from itself; only a reference needs a name.
        is_document = self.entry.kind in (coverage.JSON_KEYS, coverage.SNAPSHOT)
        names = (
            {}
            if is_document
            else _value_names(
                self.entry, [value for _pk, old, new in rows for value in (old, new)]
            )
        )
        result = []
        for pk, old, new in rows:
            uuid, description = described.get(pk, (None, f"#{pk}"))
            if is_document:
                old_value = _render_document(old, new)
                new_value = _render_document(new, old) if new is not None else None
            else:
                old_value = names.get(old) if old is not None else None
                new_value = names.get(new) if new is not None else None
            result.append(
                AffectedRow(
                    id=pk,
                    uuid=uuid,
                    model=self.model._meta.label,
                    field=self.attname,
                    description=description,
                    old_value=old_value,
                    new_value=new_value,
                    kept_on_source=new is None,
                )
            )
        return result

    def _describe_objects(self, pks) -> dict[int, tuple[str | None, str]]:
        if not pks:
            return {}
        if self.entry.kind == coverage.M2M:
            owner_attname = resolve_relation(self.entry)[2][0]
            return describe_m2m_rows(self.model, owner_attname, pks)
        describer = DESCRIBERS.get(self.model._meta.label, describe_fallback)
        return describer(self.model, pks)


def _written_model(entry: coverage.CoverageEntry) -> tuple[Any, str]:
    """The model and column a merge writes for ``entry``.

    Not always the model of the label: a many-to-many is written through its
    through table, and a generic foreign key through its object id column.
    """
    if entry.kind in (coverage.FK, coverage.M2M):
        model, attname, _unique, _related = resolve_relation(entry)
        return model, attname
    if entry.kind == coverage.GENERIC:
        return entry.model, entry.id_field
    if entry.kind == coverage.UUID:
        return entry.model, entry.field_name
    model = entry.model
    return model, model._meta.get_field(entry.field_name).attname


def journalled_rows(merge, model, attname: str):
    """The journal of what the merge wrote for one entry, newest row first."""
    return merge.changes.filter(model=model._meta.label, field=attname).order_by(
        "-object_id", "-id"
    )


def uses_journal(merge) -> bool:
    """Whether the merge is far enough along for the journal to be the truth.

    Once a merge has run, recomputing its plan would describe a merge of an
    archived source into an offering that already holds the rows — a plan for
    something nobody asked for. The journal says what was actually written,
    and it outlives an undo, so an undone merge can still be inspected.
    """
    states = models.OfferingMerge.States
    return merge.state in (states.DONE, states.UNDOING, states.UNDONE)


def affected_rows(merge, entry: coverage.CoverageEntry) -> AffectedRows:
    """The rows ``entry`` covers for ``merge``, newest first. Reads only."""
    model, attname = _written_model(entry)
    if uses_journal(merge):
        items = journalled_rows(merge, model, attname).values_list(
            "object_id", "old_value", "new_value"
        )
    else:
        items = sorted(planned_rows(merge, entry), key=lambda row: -row[0])
    return AffectedRows(entry, model, attname, items)
