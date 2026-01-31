"""Tests for Arrow models."""

from decimal import Decimal

from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.waldur_arrow import models
from waldur_mastermind.waldur_arrow.tests.fixtures import ArrowFixture


class ArrowSettingsTest(TestCase):
    """Tests for ArrowSettings model."""

    def test_get_active_returns_active_settings(self):
        settings = models.ArrowSettings.objects.create(
            api_url="https://api.arrow.test/",
            api_key="test-key",
            is_active=True,
        )

        result = models.ArrowSettings.get_active()

        self.assertEqual(result, settings)

    def test_get_active_returns_none_when_no_active(self):
        models.ArrowSettings.objects.create(
            api_url="https://api.arrow.test/",
            api_key="test-key",
            is_active=False,
        )

        result = models.ArrowSettings.get_active()

        self.assertIsNone(result)

    def test_str_representation(self):
        settings = models.ArrowSettings.objects.create(
            api_url="https://api.arrow.test/",
            api_key="test-key",
        )

        self.assertEqual(str(settings), "Arrow Settings (https://api.arrow.test/)")


class ArrowCustomerMappingTest(TestCase):
    """Tests for ArrowCustomerMapping model."""

    def setUp(self):
        self.fixture = ArrowFixture()

    def test_str_representation(self):
        mapping = self.fixture.customer_mapping

        expected = f"{mapping.arrow_reference} -> {mapping.waldur_customer.name}"
        self.assertEqual(str(mapping), expected)

    def test_unique_together_constraint(self):
        # First mapping already exists in fixture
        self.fixture.customer_mapping

        # Trying to create duplicate should raise error
        with self.assertRaises(Exception):
            models.ArrowCustomerMapping.objects.create(
                settings=self.fixture.arrow_settings,
                arrow_reference=self.fixture.customer_mapping.arrow_reference,
                waldur_customer=structure_factories.CustomerFactory(),
            )


class ArrowBillingSyncTest(TestCase):
    """Tests for ArrowBillingSync model."""

    def setUp(self):
        self.fixture = ArrowFixture()

    def test_mark_synced_transition(self):
        sync = self.fixture.billing_sync

        self.assertEqual(sync.state, models.ArrowBillingSync.States.PENDING)

        sync.mark_synced()
        sync.save()

        self.assertEqual(sync.state, models.ArrowBillingSync.States.SYNCED)
        self.assertIsNotNone(sync.synced_at)

    def test_mark_validated_transition(self):
        sync = self.fixture.billing_sync
        sync.mark_synced()
        sync.save()

        sync.mark_validated()
        sync.save()

        self.assertEqual(sync.state, models.ArrowBillingSync.States.VALIDATED)
        self.assertIsNotNone(sync.validated_at)

    def test_mark_reconciled_transition(self):
        sync = self.fixture.billing_sync
        sync.mark_synced()
        sync.save()
        sync.mark_validated()
        sync.save()

        sync.mark_reconciled()
        sync.save()

        self.assertEqual(sync.state, models.ArrowBillingSync.States.RECONCILED)
        self.assertIsNotNone(sync.reconciled_at)

    def test_str_representation(self):
        sync = self.fixture.billing_sync

        expected = f"Sync {sync.statement_reference} ({sync.report_period})"
        self.assertEqual(str(sync), expected)


class ArrowBillingSyncItemTest(TestCase):
    """Tests for ArrowBillingSyncItem model."""

    def setUp(self):
        self.fixture = ArrowFixture()

    def test_get_invoice_item_details(self):
        # Ensure billing_sync is created first, which creates the invoice
        billing_sync = self.fixture.billing_sync
        invoice = billing_sync.invoice
        invoice_item = invoice_models.InvoiceItem.objects.create(
            invoice=invoice,
            name="Test Item",
            unit_price=Decimal("100.00"),
            quantity=Decimal("1"),
        )

        sync_item = models.ArrowBillingSyncItem.objects.create(
            billing_sync=self.fixture.billing_sync,
            arrow_line_reference="LINE-001",
            invoice_item=invoice_item,
            original_price=Decimal("100.00"),
            vendor_name="Microsoft",
            subscription_reference="XSPS1234",
            classification="IAAS",
        )

        details = sync_item.get_invoice_item_details()

        self.assertEqual(details["source"], "arrow")
        self.assertEqual(details["arrow_line_reference"], "LINE-001")
        self.assertEqual(details["vendor_name"], "Microsoft")
        self.assertEqual(details["classification"], "IAAS")

    def test_get_compensation_details(self):
        billing_sync = self.fixture.billing_sync
        invoice = billing_sync.invoice
        invoice_item = invoice_models.InvoiceItem.objects.create(
            invoice=invoice,
            name="Test Item",
            unit_price=Decimal("100.00"),
            quantity=Decimal("1"),
        )

        sync_item = models.ArrowBillingSyncItem.objects.create(
            billing_sync=self.fixture.billing_sync,
            arrow_line_reference="LINE-001",
            invoice_item=invoice_item,
            original_price=Decimal("100.00"),
        )

        details = sync_item.get_compensation_details(final_price=Decimal("95.00"))

        self.assertEqual(details["source"], "arrow_reconciliation")
        self.assertEqual(details["original_line_reference"], "LINE-001")
        self.assertEqual(details["original_price"], "100.00")
        self.assertEqual(details["final_price"], "95.00")
        self.assertEqual(details["original_period"], "2024-01")

    def test_str_representation(self):
        billing_sync = self.fixture.billing_sync
        invoice = billing_sync.invoice
        invoice_item = invoice_models.InvoiceItem.objects.create(
            invoice=invoice,
            name="Test Item",
            unit_price=Decimal("100.00"),
            quantity=Decimal("1"),
        )

        sync_item = models.ArrowBillingSyncItem.objects.create(
            billing_sync=self.fixture.billing_sync,
            arrow_line_reference="LINE-001",
            invoice_item=invoice_item,
            original_price=Decimal("100.00"),
        )

        self.assertEqual(str(sync_item), "Item LINE-001")
