from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations

OPEN_STATES = ("pending", "pending_finalization")
CENT = Decimal("0.01")


def shift_invoice_totals(invoice, price_delta):
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


def bill_line_totals_once(apps, schema_editor):
    """
    Billing-sync items stored a line total as unit_price and the Arrow Qty as
    quantity, charging total x Qty. Bill those still on open invoices once;
    the Arrow quantity is kept in details. Issued invoices stay as issued.
    """
    InvoiceItem = apps.get_model("invoices", "InvoiceItem")
    items = (
        InvoiceItem.objects.filter(
            details__source="arrow",
            details__has_key="arrow_line_reference",
            invoice__state__in=OPEN_STATES,
            unit="quantity",
        )
        .exclude(quantity=Decimal("1"))
        .select_related("invoice")
    )
    for item in items:
        price_delta = item.unit_price * (Decimal("1") - item.quantity)
        item.details = {**(item.details or {}), "arrow_quantity": str(item.quantity)}
        item.quantity = Decimal("1")
        item.save(update_fields=["quantity", "details"])
        shift_invoice_totals(item.invoice, price_delta)


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_arrow", "0007_add_invoice_item_prefix"),
        ("invoices", "0038_credit_transaction_channel"),
    ]

    operations = [
        migrations.RunPython(bill_line_totals_once, migrations.RunPython.noop),
    ]
