"""
Tests for the non-billable offering invoice items migration (0022).

This module tests the migration that:
- Deletes invoice items from PENDING invoices for non-billable offerings
- Creates compensation (negative) items for CREATED invoices

Since migrations use apps.get_model(), we test the migration logic by
reimplementing it with real models to verify the expected behavior.
"""

from decimal import Decimal

from django.db.models import Q
from django.test import TestCase

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


def run_fix_migration_logic():
    """
    Reimplementation of the migration logic for testing purposes.
    Uses real models instead of apps.get_model().
    """
    InvoiceItem = invoice_models.InvoiceItem
    Invoice = invoice_models.Invoice

    # Find all affected invoice items
    affected_items = list(
        InvoiceItem.objects.filter(
            resource__offering__billable=False,
        )
        .filter(Q(invoice__year=2025, invoice__month=12) | Q(invoice__year__gte=2026))
        .select_related(
            "invoice", "invoice__customer", "resource", "resource__offering"
        )
    )

    if not affected_items:
        return {"deleted": 0, "compensations": 0}

    pending_items = []
    created_items = []

    for item in affected_items:
        if item.invoice.state == Invoice.States.PENDING:
            pending_items.append(item)
        elif item.invoice.state == Invoice.States.CREATED:
            created_items.append(item)

    affected_invoice_pks = set()

    # Strategy 1: Delete items from PENDING invoices
    deleted_pending = 0
    for item in pending_items:
        affected_invoice_pks.add(item.invoice.pk)
        item.delete()
        deleted_pending += 1

    # Strategy 2: Create compensation items for CREATED invoices
    compensation_items = []
    for item in created_items:
        if item.unit_price <= 0:
            continue

        affected_invoice_pks.add(item.invoice.pk)

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

    if compensation_items:
        InvoiceItem.objects.bulk_create(compensation_items)

    # Update invoice caches
    for invoice_pk in affected_invoice_pks:
        try:
            invoice = Invoice.objects.get(pk=invoice_pk)
            if hasattr(invoice, "update_cache"):
                invoice.update_cache()
        except Exception:
            pass

    return {"deleted": deleted_pending, "compensations": len(compensation_items)}


def run_reverse_migration_logic():
    """
    Reimplementation of the reverse migration logic for testing purposes.
    """
    InvoiceItem = invoice_models.InvoiceItem
    Invoice = invoice_models.Invoice

    # Find and delete compensation items created by this migration
    compensation_items = InvoiceItem.objects.filter(
        details__correction_migration="0022_fix_non_billable_offering_invoice_items",
    )

    affected_invoice_pks = set(
        compensation_items.values_list("invoice_id", flat=True).distinct()
    )

    count = compensation_items.count()
    compensation_items.delete()

    # Update invoice caches
    for invoice_pk in affected_invoice_pks:
        try:
            invoice = Invoice.objects.get(pk=invoice_pk)
            if hasattr(invoice, "update_cache"):
                invoice.update_cache()
        except Exception:
            pass

    return {"deleted": count}


class NonBillableOfferingMigrationTest(TestCase):
    """Tests for the migration fixing non-billable offering invoice items."""

    def setUp(self):
        """Set up test fixtures with billable and non-billable offerings."""
        # Create a non-billable offering (e.g., OpenStack.Instance child)
        self.non_billable_offering = marketplace_factories.OfferingFactory(
            billable=False,
            state=OfferingStates.ACTIVE,
        )
        self.non_billable_resource = marketplace_factories.ResourceFactory(
            offering=self.non_billable_offering,
        )

        # Create a billable offering for comparison
        self.billable_offering = marketplace_factories.OfferingFactory(
            billable=True,
            state=OfferingStates.ACTIVE,
        )
        self.billable_resource = marketplace_factories.ResourceFactory(
            offering=self.billable_offering,
            project=self.non_billable_resource.project,
        )

        self.customer = self.non_billable_resource.project.customer

    def test_pending_invoice_items_are_deleted(self):
        """Test that items in PENDING invoices are deleted."""
        # Create PENDING invoice for January 2026
        pending_invoice = invoice_factories.InvoiceFactory(
            customer=self.customer,
            year=2026,
            month=1,
            state=invoice_models.Invoice.States.PENDING,
        )

        # Create invoice item for non-billable resource
        non_billable_item = invoice_factories.InvoiceItemFactory(
            invoice=pending_invoice,
            resource=self.non_billable_resource,
            project=self.non_billable_resource.project,
            unit_price=Decimal("100.00"),
            quantity=1,
        )
        non_billable_item_uuid = non_billable_item.uuid

        # Create invoice item for billable resource (should NOT be deleted)
        billable_item = invoice_factories.InvoiceItemFactory(
            invoice=pending_invoice,
            resource=self.billable_resource,
            project=self.billable_resource.project,
            unit_price=Decimal("200.00"),
            quantity=1,
        )

        # Run the migration logic
        result = run_fix_migration_logic()

        # Verify: non-billable item should be deleted
        self.assertFalse(
            invoice_models.InvoiceItem.objects.filter(
                uuid=non_billable_item_uuid
            ).exists(),
            "Non-billable invoice item in PENDING invoice should be deleted",
        )
        self.assertEqual(result["deleted"], 1)

        # Verify: billable item should still exist
        billable_item.refresh_from_db()
        self.assertEqual(billable_item.unit_price, Decimal("200.00"))

    def test_created_invoice_items_get_compensation(self):
        """Test that items in CREATED invoices get compensation items."""
        # Create CREATED invoice for December 2025
        created_invoice = invoice_factories.InvoiceFactory(
            customer=self.customer,
            year=2025,
            month=12,
            state=invoice_models.Invoice.States.CREATED,
        )

        # Create invoice item for non-billable resource
        original_item = invoice_factories.InvoiceItemFactory(
            invoice=created_invoice,
            resource=self.non_billable_resource,
            project=self.non_billable_resource.project,
            name="OpenStack Instance - Test VM",
            unit_price=Decimal("150.00"),
            quantity=1,
            unit="month",
        )

        initial_item_count = created_invoice.items.count()

        # Run the migration logic
        result = run_fix_migration_logic()

        # Verify: original item should still exist
        original_item.refresh_from_db()
        self.assertEqual(original_item.unit_price, Decimal("150.00"))

        # Verify: compensation item should be created
        self.assertEqual(
            created_invoice.items.count(),
            initial_item_count + 1,
            "A compensation item should be created",
        )
        self.assertEqual(result["compensations"], 1)

        # Find compensation item
        compensation_item = created_invoice.items.filter(
            unit_price__lt=0,
        ).first()

        self.assertIsNotNone(compensation_item, "Compensation item should exist")
        self.assertEqual(compensation_item.unit_price, Decimal("-150.00"))
        self.assertIn("Billing correction:", compensation_item.name)
        self.assertEqual(
            compensation_item.details.get("correction_migration"),
            "0022_fix_non_billable_offering_invoice_items",
        )
        self.assertEqual(
            compensation_item.details.get("original_item_uuid"),
            str(original_item.uuid),
        )

    def test_billable_items_are_not_affected(self):
        """Test that billable offering items are not affected by the migration."""
        # Create PENDING invoice
        pending_invoice = invoice_factories.InvoiceFactory(
            customer=self.customer,
            year=2026,
            month=1,
            state=invoice_models.Invoice.States.PENDING,
        )

        # Create invoice item for billable resource
        billable_item = invoice_factories.InvoiceItemFactory(
            invoice=pending_invoice,
            resource=self.billable_resource,
            project=self.billable_resource.project,
            unit_price=Decimal("300.00"),
            quantity=2,
        )

        # Run the migration logic
        result = run_fix_migration_logic()

        # Verify: billable item should NOT be affected
        billable_item.refresh_from_db()
        self.assertEqual(billable_item.unit_price, Decimal("300.00"))
        self.assertEqual(billable_item.quantity, 2)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["compensations"], 0)

    def test_items_before_december_2025_are_not_affected(self):
        """Test that items before December 2025 are not affected."""
        # Create PENDING invoice for November 2025
        nov_invoice = invoice_factories.InvoiceFactory(
            customer=self.customer,
            year=2025,
            month=11,
            state=invoice_models.Invoice.States.PENDING,
        )

        # Create invoice item for non-billable resource
        old_item = invoice_factories.InvoiceItemFactory(
            invoice=nov_invoice,
            resource=self.non_billable_resource,
            project=self.non_billable_resource.project,
            unit_price=Decimal("100.00"),
            quantity=1,
        )

        # Run the migration logic
        result = run_fix_migration_logic()

        # Verify: old item should NOT be affected
        old_item.refresh_from_db()
        self.assertEqual(old_item.unit_price, Decimal("100.00"))
        self.assertEqual(result["deleted"], 0)

    def test_zero_price_items_are_skipped_for_compensation(self):
        """Test that items with zero price don't get compensation."""
        # Create CREATED invoice for December 2025
        created_invoice = invoice_factories.InvoiceFactory(
            customer=self.customer,
            year=2025,
            month=12,
            state=invoice_models.Invoice.States.CREATED,
        )

        # Create invoice item with zero price
        invoice_factories.InvoiceItemFactory(
            invoice=created_invoice,
            resource=self.non_billable_resource,
            project=self.non_billable_resource.project,
            unit_price=Decimal("0.00"),
            quantity=1,
        )

        initial_item_count = created_invoice.items.count()

        # Run the migration logic
        result = run_fix_migration_logic()

        # Verify: no compensation item should be created for zero price items
        self.assertEqual(
            created_invoice.items.count(),
            initial_item_count,
            "No compensation item should be created for zero price items",
        )
        self.assertEqual(result["compensations"], 0)

    def test_multiple_items_across_multiple_invoices(self):
        """Test handling multiple items across different invoices."""
        # Create PENDING invoice
        pending_invoice = invoice_factories.InvoiceFactory(
            customer=self.customer,
            year=2026,
            month=1,
            state=invoice_models.Invoice.States.PENDING,
        )

        # Create CREATED invoice
        created_invoice = invoice_factories.InvoiceFactory(
            customer=self.customer,
            year=2025,
            month=12,
            state=invoice_models.Invoice.States.CREATED,
        )

        # Create items in both invoices
        pending_item = invoice_factories.InvoiceItemFactory(
            invoice=pending_invoice,
            resource=self.non_billable_resource,
            project=self.non_billable_resource.project,
            unit_price=Decimal("100.00"),
            quantity=1,
        )
        pending_item_uuid = pending_item.uuid

        created_item = invoice_factories.InvoiceItemFactory(
            invoice=created_invoice,
            resource=self.non_billable_resource,
            project=self.non_billable_resource.project,
            unit_price=Decimal("200.00"),
            quantity=1,
        )

        # Run the migration logic
        result = run_fix_migration_logic()

        # Verify: pending item deleted
        self.assertFalse(
            invoice_models.InvoiceItem.objects.filter(uuid=pending_item_uuid).exists()
        )
        self.assertEqual(result["deleted"], 1)

        # Verify: created item has compensation
        created_item.refresh_from_db()
        compensation = created_invoice.items.filter(unit_price__lt=0).first()
        self.assertIsNotNone(compensation)
        self.assertEqual(compensation.unit_price, Decimal("-200.00"))
        self.assertEqual(result["compensations"], 1)


class NonBillableOfferingMigrationReverseTest(TestCase):
    """Tests for reversing the migration."""

    def setUp(self):
        """Set up test fixtures."""
        self.non_billable_offering = marketplace_factories.OfferingFactory(
            billable=False,
            state=OfferingStates.ACTIVE,
        )
        self.non_billable_resource = marketplace_factories.ResourceFactory(
            offering=self.non_billable_offering,
        )
        self.customer = self.non_billable_resource.project.customer

    def test_reverse_removes_compensation_items(self):
        """Test that reverse migration removes compensation items."""
        # Create CREATED invoice
        created_invoice = invoice_factories.InvoiceFactory(
            customer=self.customer,
            year=2025,
            month=12,
            state=invoice_models.Invoice.States.CREATED,
        )

        # Create original invoice item
        original_item = invoice_factories.InvoiceItemFactory(
            invoice=created_invoice,
            resource=self.non_billable_resource,
            project=self.non_billable_resource.project,
            unit_price=Decimal("100.00"),
            quantity=1,
        )

        # Create compensation item (as if migration ran)
        compensation_item = invoice_factories.InvoiceItemFactory(
            invoice=created_invoice,
            resource=self.non_billable_resource,
            project=self.non_billable_resource.project,
            name="Billing correction: Test",
            unit_price=Decimal("-100.00"),
            quantity=1,
            details={
                "correction_migration": "0022_fix_non_billable_offering_invoice_items",
                "original_item_uuid": str(original_item.uuid),
            },
        )
        compensation_uuid = compensation_item.uuid

        # Run reverse migration logic
        result = run_reverse_migration_logic()

        # Verify: compensation item should be deleted
        self.assertFalse(
            invoice_models.InvoiceItem.objects.filter(uuid=compensation_uuid).exists(),
            "Compensation item should be deleted by reverse migration",
        )
        self.assertEqual(result["deleted"], 1)

        # Verify: original item should still exist
        original_item.refresh_from_db()
        self.assertEqual(original_item.unit_price, Decimal("100.00"))

    def test_reverse_does_not_affect_other_items(self):
        """Test that reverse migration doesn't affect non-compensation items."""
        # Create invoice
        invoice = invoice_factories.InvoiceFactory(
            customer=self.customer,
            year=2025,
            month=12,
            state=invoice_models.Invoice.States.CREATED,
        )

        # Create regular invoice item
        regular_item = invoice_factories.InvoiceItemFactory(
            invoice=invoice,
            resource=self.non_billable_resource,
            project=self.non_billable_resource.project,
            unit_price=Decimal("100.00"),
            quantity=1,
            details={},  # No migration marker
        )

        # Create a manual compensation (not from migration)
        manual_compensation = invoice_factories.InvoiceItemFactory(
            invoice=invoice,
            resource=self.non_billable_resource,
            project=self.non_billable_resource.project,
            unit_price=Decimal("-50.00"),
            quantity=1,
            details={
                "correction_reason": "Manual adjustment",
                # Note: no correction_migration marker
            },
        )

        # Run reverse migration logic
        result = run_reverse_migration_logic()

        # Verify: both items should still exist
        regular_item.refresh_from_db()
        manual_compensation.refresh_from_db()

        self.assertEqual(regular_item.unit_price, Decimal("100.00"))
        self.assertEqual(manual_compensation.unit_price, Decimal("-50.00"))
        self.assertEqual(result["deleted"], 0)
