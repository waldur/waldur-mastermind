"""Organization-aggregated volume discounts for offering components.

A volume discount is computed on the TOTAL billed quantity of one plan
component, summed across every resource billed under that plan component in
the customer's invoice (bound to the formula variable ``usage``). The
resulting percentage is distributed back across the component's main invoice
items as paired negative "discount" line items, so per-resource / per-project
cost attribution is preserved.

The discount formula and its aggregation scope are configured by the service
provider per plan (``PlanComponent.discount_formula``), so each invoice item
is discounted only under its own plan's configuration — plans of the same
offering never share or inherit each other's discounts.

The final pass runs at invoice finalization, before credit compensation and
affiliate-fee accrual. An open invoice gets the same lines as estimates, so the
invoice total, the credit forecast and the cost policies show what the month
will be charged rather than its gross: they are refreshed daily and after a
billing change, marked ``details["is_estimate"]``, and rebuilt as final when
the month closes. Each pass is idempotent and brings the existing lines in line
with the items in place, so a pass with nothing changed writes nothing. A
broken formula is logged and skipped for that group — it must never block
month close.

Main invoice items opt into aggregation by storing the volume that feeds the
formula under ``details["discount_usage"]`` at creation time (usage equals the
billed quantity for usage components, the raw limit for limit components and
the configured amount for fixed components).
"""

import logging
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from waldur_core.structure.models import Project
from waldur_mastermind.billing import handlers as billing_handlers
from waldur_mastermind.common import formula as common_formula
from waldur_mastermind.common.formula import FormulaError
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.utils import format_percent
from waldur_mastermind.marketplace.enums import DiscountAggregations

logger = logging.getLogger(__name__)

# Marker key on InvoiceItem.details carrying the volume that feeds the discount
# formula. Only main items with this key participate in aggregation.
DISCOUNT_USAGE_KEY = "discount_usage"

# Marker key on a discount line of an open invoice: the amount is month to date
# and is replaced by the final one when the month closes.
ESTIMATE_KEY = "is_estimate"

# A billing change queues a refresh of the customer's open invoices this many
# seconds later, so a burst of changes (a project's resources terminated at
# once, say) is rebuilt once rather than once per resource.
REFRESH_DEBOUNCE_SECONDS = 60


def refresh_debounce_key(customer_id) -> str:
    return f"volume_discount_refresh:{customer_id}"


def apply_aggregated_volume_discounts(invoice, estimate=False) -> None:
    """Bring the invoice's volume-discount lines in line with its items. The
    formula and its scope (aggregated across the customer's resources or per
    resource) come from each item's own plan component.

    ``estimate`` builds the lines of an open invoice: they are marked as
    estimates, and nothing is written once the invoice has closed. Month close
    passes ``estimate=False`` and turns them into final lines.
    """
    with transaction.atomic():
        # Passes over one invoice can overlap now that open invoices are
        # refreshed too (the daily pass, one queued by a billing change, month
        # close), and each would create the same missing line. Locking the
        # invoice row serialises them. no_key: a plain FOR UPDATE also blocks
        # the FOR KEY SHARE every new item of this invoice takes for its
        # foreign key, so billing writes for the customer would wait on the
        # pass, and at month close on the whole close of the invoice.
        locked = invoice_models.Invoice.objects.select_for_update(no_key=True).get(
            pk=invoice.pk
        )
        if (
            estimate
            and locked.state not in invoice_models.Invoice.States.MUTABLE_STATES
        ):
            # Closed while the refresh waited: month close wrote the final lines.
            return
        _reconcile(invoice, _wanted_lines(invoice, estimate))


def refresh_estimated_discounts(invoice) -> None:
    """Rebuild an open invoice's discount lines as estimates. A failure is
    logged, never raised: the estimate is a convenience, month close is not."""
    try:
        apply_aggregated_volume_discounts(invoice, estimate=True)
    except Exception:
        logger.exception(
            "Unable to refresh estimated volume discounts of invoice %s.", invoice
        )


def with_discounts(invoices):
    """The invoices a discount pass can change: those with an item under a plan
    component that has a formula, or with discount lines that may now be stale.
    Saves loading every item of every invoice on a deployment that uses no
    discounts."""
    return invoices.filter(
        Q(items__plan_component__discount_formula__gt="")
        | Q(items__details__has_key="is_discount")
    ).distinct()


def refresh_open_invoices(invoices) -> None:
    """Rebuild the estimates of the open invoices among ``invoices`` that a
    pass can change."""
    open_invoices = invoices.filter(
        state__in=invoice_models.Invoice.States.MUTABLE_STATES
    )
    for invoice in with_discounts(open_invoices):
        refresh_estimated_discounts(invoice)


def _wanted_lines(invoice, estimate) -> dict[str, dict]:
    """The discount lines the invoice should carry, keyed by the uuid of the
    main item each one discounts."""
    main_items = [
        item
        for item in invoice.items.select_related(
            "resource", "plan_component", "plan_component__component"
        )
        if item.plan_component_id
        and item.resource_id
        and item.plan_component.component_id
        and item.details.get(DISCOUNT_USAGE_KEY) is not None
    ]

    # Bucket by plan component: it carries the formula and the aggregation
    # scope, so items billed under a plan without a discount configured are
    # never discounted by another plan's formula.
    plan_component_buckets = defaultdict(list)
    for item in main_items:
        plan_component_buckets[item.plan_component_id].append(item)

    wanted = {}
    for items in plan_component_buckets.values():
        plan_component = items[0].plan_component
        offering_component = plan_component.component
        formula = (plan_component.discount_formula or "").strip()
        if not formula:
            continue

        # Per-resource scope discounts each resource on its own usage; the
        # per-customer scope treats the whole plan-component bucket as one
        # group.
        if plan_component.discount_aggregation == DiscountAggregations.PER_RESOURCE:
            per_resource = defaultdict(list)
            for item in items:
                per_resource[item.resource_id].append(item)
            groups = list(per_resource.values())
        else:
            groups = [items]

        for group in groups:
            total_usage = sum(
                Decimal(str(item.details[DISCOUNT_USAGE_KEY])) for item in group
            )
            try:
                percent = _evaluate_percent(formula, total_usage)
            except FormulaError:
                logger.warning(
                    "Volume discount formula for offering component %s failed to "
                    "evaluate on usage %s; skipping discount for invoice %s.",
                    offering_component.pk,
                    total_usage,
                    invoice.uuid,
                )
                continue
            if percent <= 0:
                continue
            for item in group:
                line = _discount_line(item, percent, formula, total_usage, estimate)
                if line:
                    wanted[item.uuid.hex] = line
    return wanted


def _evaluate_percent(formula: str, usage) -> Decimal:
    percent = common_formula.evaluate(formula, usage=usage)
    return max(Decimal("0"), min(Decimal("100"), percent))


def _discount_line(main_item, percent, formula, total_usage, estimate):
    """Field values of the discount line for one main item, or None when the
    discount comes to nothing."""
    discount_amount = quantize_price(
        Decimal(main_item.unit_price) * Decimal(main_item.quantity) * percent / 100
    )
    if discount_amount <= 0:
        return None

    from waldur_mastermind.marketplace.billing_utils import (
        get_component_name,
        get_invoice_item_name,
    )

    plan_component = main_item.plan_component
    offering_component = plan_component.component
    details = {
        "is_discount": True,
        "discount_type": "org_volume_discount",
        "discount_formula": formula,
        "discount_percent": float(percent),
        "aggregated_usage": float(total_usage),
        "offering_component_type": offering_component.type,
        "offering_component_name": offering_component.name,
        # Link back to the main line item this discount applies to, so the UI
        # can pair the discount with its component instead of listing it loose.
        "discount_of_item": main_item.uuid.hex,
    }
    label = f"Volume discount ({format_percent(percent)}%)"
    if estimate:
        details[ESTIMATE_KEY] = True
        label = f"Volume discount ({format_percent(percent)}%, estimated)"
    return dict(
        name=(
            f"{get_invoice_item_name(main_item.resource)} / "
            f"{get_component_name(plan_component)} / {label}"
        ),
        resource=main_item.resource,
        plan_component=plan_component,
        project=main_item.project,
        project_name=main_item.project_name,
        project_uuid=main_item.project_uuid,
        unit_price=-discount_amount,
        unit=invoice_models.Units.QUANTITY,
        quantity=1,
        article_code=main_item.article_code,
        start=main_item.start,
        end=main_item.end,
        details=details,
        measured_unit="",
    )


def _differs(line, name, value) -> bool:
    field = line._meta.get_field(name)
    if field.is_relation:
        return getattr(line, field.attname) != (value.pk if value else None)
    return getattr(line, name) != value


def _reconcile(invoice, wanted: dict[str, dict]) -> None:
    """Update, create and delete discount lines until the invoice carries
    exactly ``wanted``.

    A line is matched to its main item by ``discount_of_item`` and left alone
    when nothing about it changed. Deleting and recreating every line instead
    made each pass over an open invoice log a deletion and a creation per line,
    every day, for a discount that had not moved.
    """
    stale = []
    for line in invoice.items.filter(details__is_discount=True):
        fields = wanted.pop((line.details or {}).get("discount_of_item"), None)
        if fields is None:
            # Its main item is gone or no longer discounted, or it is a second
            # line for an item that already has one.
            stale.append(line)
            continue
        changed = [
            name for name, value in fields.items() if _differs(line, name, value)
        ]
        if changed:
            for name in changed:
                setattr(line, name, fields[name])
            line.save(update_fields=changed)

    for fields in wanted.values():
        invoice_models.InvoiceItem.objects.create(invoice=invoice, **fields)

    if stale:
        project_ids = {line.project_id for line in stale if line.project_id}
        invoice_models.InvoiceItem.objects.filter(
            pk__in=[line.pk for line in stale]
        ).delete()
        # A saved line refreshes the price estimates through post_save; a
        # deleted one has no such handler, so the estimates would keep it.
        billing_handlers.update_estimates_for_scopes(
            [invoice.customer, *Project.objects.filter(id__in=project_ids)]
        )
