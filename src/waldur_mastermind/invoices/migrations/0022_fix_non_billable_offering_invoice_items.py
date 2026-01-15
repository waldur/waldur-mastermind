"""
Fix invoice items incorrectly created for resources with non-billable offerings.

This issue was introduced in the billing service refactor (commit 236599072)
deployed in version 7.8.6 on November 2, 2025.

Affected period: December 2025 onwards
Root cause: MarketplaceBillingService.get_or_create_invoice() did not filter
            by offering.billable=True when processing resources.

Impact:
- 582 affected invoice items across 20 customers
- Total incorrect billing: €33,466.26
- Affected offering types: OpenStack.Instance, OpenStack.Volume
"""

import logging

from django.db import migrations
from django.db.models import Q

logger = logging.getLogger(__name__)


def fix_non_billable_offering_invoice_items(apps, schema_editor):
    """
    Fix invoice items incorrectly created for resources with non-billable offerings.

    Strategy:
    - PENDING invoices: Delete the incorrect items directly
    - CREATED invoices: Create compensation (negative) items to offset
    - PAID invoices with €0: Skip (no financial impact)
    """
    InvoiceItem = apps.get_model("invoices", "InvoiceItem")
    Invoice = apps.get_model("invoices", "Invoice")

    # Find all affected invoice items:
    # - Resource has an offering with billable=False
    # - Invoice is from December 2025 or later
    affected_items = list(
        InvoiceItem.objects.filter(
            resource__offering__billable=False,
        )
        .filter(Q(invoice__year=2025, invoice__month=12) | Q(invoice__year__gte=2026))
        .select_related(
            "invoice", "invoice__customer", "resource", "resource__offering"
        )
    )

    total_items = len(affected_items)
    if total_items == 0:
        logger.info("No affected invoice items found. Nothing to fix.")
        return

    logger.info(f"Found {total_items} affected invoice items to process")

    # Group by invoice state for different handling
    pending_items = []
    created_items = []

    for item in affected_items:
        if item.invoice.state == "pending":
            pending_items.append(item)
        elif item.invoice.state == "created":
            created_items.append(item)
        # PAID items - skip (likely €0 value, no action needed)

    logger.info(f"PENDING invoice items: {len(pending_items)}")
    logger.info(f"CREATED invoice items: {len(created_items)}")

    # Track affected invoices for cache update
    affected_invoice_pks = set()

    # Strategy 1: Delete items from PENDING invoices (January 2026+)
    deleted_pending = 0
    for item in pending_items:
        affected_invoice_pks.add(item.invoice.pk)
        logger.info(
            f"Deleting PENDING item: {item.uuid} - {item.name[:60]}... "
            f"(Invoice: {item.invoice.year}-{item.invoice.month:02d}, "
            f"Customer: {item.invoice.customer.name}, "
            f"Amount: {item.unit_price} x {item.quantity})"
        )
        item.delete()
        deleted_pending += 1

    logger.info(f"Deleted {deleted_pending} items from PENDING invoices")

    # Strategy 2: Create compensation items for CREATED invoices (December 2025)
    compensation_items = []
    for item in created_items:
        if item.unit_price <= 0:
            continue  # Skip items with zero or negative price

        affected_invoice_pks.add(item.invoice.pk)

        # Create a compensation (negative) item
        details = dict(item.details) if item.details else {}
        details["correction_reason"] = "Non-billable offering incorrectly billed"
        details["original_item_uuid"] = str(item.uuid)
        details["correction_migration"] = "0022_fix_non_billable_offering_invoice_items"

        compensation = InvoiceItem(
            invoice=item.invoice,
            name=f"Billing correction: {item.name[:80]}",
            unit_price=-item.unit_price,
            quantity=item.quantity,
            unit=item.unit,
            measured_unit=item.measured_unit,
            resource=item.resource,
            project=item.project,
            plan_component=item.plan_component,
            start=item.start,
            end=item.end,
            article_code=item.article_code,
            details=details,
        )
        compensation_items.append(compensation)
        logger.info(
            f"Creating compensation for CREATED item: {item.uuid} - {item.name[:60]}... "
            f"(Invoice: {item.invoice.year}-{item.invoice.month:02d}, "
            f"Customer: {item.invoice.customer.name}, "
            f"Amount: -{item.unit_price} x {item.quantity})"
        )

    # Bulk create compensation items
    if compensation_items:
        InvoiceItem.objects.bulk_create(compensation_items)
        logger.info(
            f"Created {len(compensation_items)} compensation items for CREATED invoices"
        )

    # Update invoice caches
    for invoice_pk in affected_invoice_pks:
        try:
            invoice = Invoice.objects.get(pk=invoice_pk)
            # Call update_cache if available (it recalculates totals)
            if hasattr(invoice, "update_cache"):
                invoice.update_cache()
                logger.info(
                    f"Updated cache for invoice {invoice.year}-{invoice.month:02d} "
                    f"({invoice.customer.name})"
                )
        except Exception as e:
            logger.warning(f"Failed to update invoice cache for pk={invoice_pk}: {e}")

    logger.info(
        f"Migration complete. "
        f"Deleted {deleted_pending} PENDING items, "
        f"created {len(compensation_items)} compensation items."
    )


def reverse_fix(apps, schema_editor):
    """
    Reverse migration - removes compensation items created by forward migration.
    Note: Cannot restore deleted PENDING items - they would be recreated by
    the billing system if needed.
    """
    InvoiceItem = apps.get_model("invoices", "InvoiceItem")
    Invoice = apps.get_model("invoices", "Invoice")

    # Find and delete compensation items created by this migration
    compensation_items = InvoiceItem.objects.filter(
        details__correction_migration="0022_fix_non_billable_offering_invoice_items",
    )

    # Track affected invoices
    affected_invoice_pks = set(
        compensation_items.values_list("invoice_id", flat=True).distinct()
    )

    count = compensation_items.count()
    compensation_items.delete()
    logger.info(f"Reverse migration: Deleted {count} compensation items")

    # Update invoice caches
    for invoice_pk in affected_invoice_pks:
        try:
            invoice = Invoice.objects.get(pk=invoice_pk)
            if hasattr(invoice, "update_cache"):
                invoice.update_cache()
        except Exception as e:
            logger.warning(f"Failed to update invoice cache for pk={invoice_pk}: {e}")

    logger.warning(
        "Note: Deleted PENDING items cannot be restored automatically. "
        "If the billing code fix has been reverted, they will be recreated "
        "when invoices are next processed."
    )


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0021_populate_plan_component_field"),
        (
            "marketplace",
            "0108_squashed_0131",
        ),  # Ensure marketplace models are available
    ]

    operations = [
        migrations.RunPython(
            fix_non_billable_offering_invoice_items,
            reverse_code=reverse_fix,
        ),
    ]
