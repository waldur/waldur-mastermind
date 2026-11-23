from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations

OPEN_STATES = ("pending", "pending_finalization")
CENT = Decimal("0.01")


def _shift_invoice_totals(invoice, price_delta):
    """
    Move an invoice's cached totals by a known change in its item prices.

    Mirrors Invoice.update_cache (total_price = items, total_cost = items +
    tax) on the historical model, so the migration does not depend on the
    live model's columns.
    """
    price = (Decimal(invoice.total_price) + price_delta).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    tax = price * Decimal(invoice.tax_percent) / 100
    invoice.total_price = price
    invoice.total_cost = (price + tax).quantize(CENT, rounding=ROUND_HALF_UP)
    invoice.save(update_fields=["total_price", "total_cost"])


def _quantity_price(item):
    """An item's price when its unit is a plain quantity; None otherwise."""
    if item.unit != "quantity":
        return None
    return item.unit_price * item.quantity


def zero_cloud_cost_price(apps, schema_editor):
    """
    Stop billing Arrow consumption through marketplace usage.

    The consumption sync bills each license with its own invoice item; a
    priced cloud_cost usage component charged the same consumption again.
    The usage stays recorded, at price 0.
    """
    PlanComponent = apps.get_model("marketplace", "PlanComponent")
    ArrowVendorOfferingMapping = apps.get_model(
        "waldur_arrow", "ArrowVendorOfferingMapping"
    )
    Resource = apps.get_model("marketplace", "Resource")
    InvoiceItem = apps.get_model("invoices", "InvoiceItem")

    arrow_offering_ids = set(
        ArrowVendorOfferingMapping.objects.values_list("offering_id", flat=True)
    ) | set(
        Resource.objects.filter(
            attributes__has_key="arrow_license_reference"
        ).values_list("offering_id", flat=True)
    )
    plan_components = PlanComponent.objects.filter(
        component__type="cloud_cost", component__offering_id__in=arrow_offering_ids
    )
    plan_component_ids = list(plan_components.values_list("id", flat=True))
    plan_components.exclude(price=0).update(price=0)

    # An invoice item keeps the unit price it was created with, so the items
    # on open invoices would go on charging the usage; issued ones stay.
    items = (
        InvoiceItem.objects.filter(
            plan_component_id__in=plan_component_ids, invoice__state__in=OPEN_STATES
        )
        .exclude(unit_price=0)
        .select_related("invoice")
    )
    for item in items:
        old_price = _quantity_price(item)
        item.unit_price = 0
        item.save(update_fields=["unit_price"])
        if old_price is not None:
            _shift_invoice_totals(item.invoice, -old_price)


def unfreeze_running_month(apps, schema_editor):
    """
    Reopen consumption records that were finalized while their month ran.

    The billing check used to reconcile the current month, which froze the
    record at a partial amount and stopped the consumption sync from billing
    the rest. The running month is reopened here, dropping the compensation
    made for it while that is still pending. An earlier month is settled
    against its full billing export when it is reconciled: the scheduled
    check does the previous month, older ones need a manual reconciliation.
    Imported months stay settled.
    """
    ArrowConsumptionRecord = apps.get_model("waldur_arrow", "ArrowConsumptionRecord")
    month_start = date.today().replace(day=1)
    records = ArrowConsumptionRecord.objects.filter(
        billing_period__gte=month_start, finalized_at__isnull=False
    ).select_related("compensation_item__invoice")
    for record in records:
        if (record.raw_data or {}).get("source") == "arrow_import":
            continue
        compensation = record.compensation_item
        if compensation and compensation.invoice.state not in OPEN_STATES:
            continue
        record.compensation_item = None
        record.final_sell = None
        record.final_buy = None
        record.finalized_at = None
        record.reconciled_at = None
        record.save()
        if compensation:
            old_price = _quantity_price(compensation)
            invoice = compensation.invoice
            compensation.delete()
            if old_price is not None:
                _shift_invoice_totals(invoice, -old_price)


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_arrow", "0008_bill_arrow_line_totals_once"),
        ("marketplace", "0329_offeringcomponent_limit_increase_only"),
        ("invoices", "0038_credit_transaction_channel"),
    ]

    operations = [
        migrations.RunPython(zero_cloud_cost_price, migrations.RunPython.noop),
        migrations.RunPython(unfreeze_running_month, migrations.RunPython.noop),
    ]
