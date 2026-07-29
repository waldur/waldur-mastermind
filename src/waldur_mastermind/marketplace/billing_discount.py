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

The whole pass runs once at invoice finalization, before credit compensation
and affiliate-fee accrual, and is idempotent: existing discount items are
cleared and rebuilt on every run. A broken formula is logged and skipped for
that group — it must never block month close.

Main invoice items opt into aggregation by storing the volume that feeds the
formula under ``details["discount_usage"]`` at creation time (usage equals the
billed quantity for usage components, the raw limit for limit components and
the configured amount for fixed components).
"""

import logging
from collections import defaultdict
from decimal import Decimal

from waldur_mastermind.common import formula as common_formula
from waldur_mastermind.common.formula import FormulaError
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace.enums import DiscountAggregations

logger = logging.getLogger(__name__)

# Marker key on InvoiceItem.details carrying the volume that feeds the discount
# formula. Only main items with this key participate in aggregation.
DISCOUNT_USAGE_KEY = "discount_usage"


def apply_aggregated_volume_discounts(invoice) -> None:
    """Rebuild the invoice's volume-discount line items. The formula and its
    scope (aggregated across the customer's resources or per resource) come
    from each item's own plan component. Idempotent: clears then recomputes."""
    invoice.items.filter(details__is_discount=True).delete()

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
                _create_discount_item(item, percent, formula, total_usage)


def _evaluate_percent(formula: str, usage) -> Decimal:
    percent = common_formula.evaluate(formula, usage=usage)
    return max(Decimal("0"), min(Decimal("100"), percent))


def _create_discount_item(main_item, percent, formula, total_usage):
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
    name = (
        f"{get_invoice_item_name(main_item.resource)} / "
        f"{get_component_name(plan_component)} / "
        f"Volume discount ({percent}%)"
    )
    return invoice_models.InvoiceItem.objects.create(
        name=name,
        resource=main_item.resource,
        plan_component=plan_component,
        project=main_item.project,
        unit_price=-discount_amount,
        unit=invoice_models.Units.QUANTITY,
        quantity=1,
        article_code=main_item.article_code,
        invoice=main_item.invoice,
        start=main_item.start,
        end=main_item.end,
        details=details,
        measured_unit="",
    )
