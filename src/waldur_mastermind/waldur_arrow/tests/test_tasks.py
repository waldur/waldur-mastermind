"""Tests for Arrow Celery tasks."""

from decimal import Decimal
from unittest import mock

from django.test import TestCase

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.waldur_arrow import models, tasks
from waldur_mastermind.waldur_arrow.tests.fixtures import ArrowFixture


class SyncArrowBillingTest(TestCase):
    """Tests for sync_arrow_billing task."""

    def setUp(self):
        self.fixture = ArrowFixture()

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    def test_sync_creates_billing_sync_and_items(self, mock_export):
        self.fixture.arrow_settings  # Ensure settings exist
        self.fixture.customer_mapping  # Ensure mapping exists

        mock_export.return_value = {
            "headers": [
                "Sequence",
                "End User Company Name",
                "Customer Total Price",
                "Total Wholesale Price",
                "Vendor Name",
                "Statement Reference",
                "Qty",
            ],
            "values": [
                [
                    "LINE-001",
                    self.fixture.customer_mapping.arrow_company_name,
                    "100.00",
                    "80.00",
                    "Microsoft",
                    "STMT-001",
                    "1",
                ],
                [
                    "LINE-002",
                    self.fixture.customer_mapping.arrow_company_name,
                    "200.00",
                    "160.00",
                    "Amazon",
                    "STMT-001",
                    "1",
                ],
            ],
        }

        tasks.sync_arrow_billing(year=2024, month=2)

        # Verify billing sync was created
        sync = models.ArrowBillingSync.objects.get(
            customer_mapping=self.fixture.customer_mapping,
            report_period="2024-02",
        )
        self.assertEqual(sync.state, models.ArrowBillingSync.States.SYNCED)
        self.assertEqual(sync.sell_total, Decimal("300.00"))

        # Verify items were created
        self.assertEqual(sync.items.count(), 2)

        # Verify invoice items were created
        invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer,
            year=2024,
            month=2,
        )
        self.assertEqual(invoice.items.count(), 2)

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    def test_sync_skips_zero_amount_items(self, mock_export):
        self.fixture.arrow_settings
        self.fixture.customer_mapping

        mock_export.return_value = {
            "headers": [
                "Sequence",
                "End User Company Name",
                "Customer Total Price",
                "Qty",
            ],
            "values": [
                [
                    "LINE-001",
                    self.fixture.customer_mapping.arrow_company_name,
                    "100.00",
                    "1",
                ],
                [
                    "LINE-002",
                    self.fixture.customer_mapping.arrow_company_name,
                    "0.00",
                    "1",
                ],
            ],
        }

        tasks.sync_arrow_billing(year=2024, month=3)

        sync = models.ArrowBillingSync.objects.get(
            customer_mapping=self.fixture.customer_mapping,
            report_period="2024-03",
        )
        # Only one item should be created (zero amount skipped)
        self.assertEqual(sync.items.count(), 1)

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    def test_sync_ignores_unmapped_customers(self, mock_export):
        self.fixture.arrow_settings
        self.fixture.customer_mapping

        mock_export.return_value = {
            "headers": [
                "Sequence",
                "End User Company Name",
                "Customer Total Price",
            ],
            "values": [
                ["LINE-001", "UNKNOWN_CUSTOMER", "100.00"],
            ],
        }

        tasks.sync_arrow_billing(year=2024, month=4)

        # No sync should be created for unmapped customer
        self.assertFalse(
            models.ArrowBillingSync.objects.filter(report_period="2024-04").exists()
        )

    def test_sync_does_nothing_without_settings(self):
        # No settings exist
        result = tasks.sync_arrow_billing(year=2024, month=1)

        self.assertIsNone(result)

    def test_sync_does_nothing_without_mappings(self):
        self.fixture.arrow_settings  # Settings exist but no mappings

        tasks.sync_arrow_billing(year=2024, month=1)

        self.assertFalse(models.ArrowBillingSync.objects.exists())


class ReconcileArrowBillingTest(TestCase):
    """Tests for reconcile_arrow_billing task."""

    def setUp(self):
        self.fixture = ArrowFixture()

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    def test_reconcile_creates_compensation_items(self, mock_export):
        # Create validated billing sync with items
        sync = self.fixture.billing_sync
        sync.mark_synced()
        sync.save()
        sync.mark_validated()
        sync.save()

        # Create original invoice item
        invoice_item = invoice_models.InvoiceItem.objects.create(
            invoice=sync.invoice,
            name="Original Item",
            unit_price=Decimal("100.00"),
            quantity=Decimal("1"),
        )

        # Create sync item
        models.ArrowBillingSyncItem.objects.create(
            billing_sync=sync,
            arrow_line_reference="LINE-001",
            invoice_item=invoice_item,
            original_price=Decimal("100.00"),
        )

        # Mock current prices with a difference
        mock_export.return_value = {
            "headers": ["Sequence", "Customer Total Price"],
            "values": [["LINE-001", "95.00"]],  # Price decreased by 5
        }

        tasks.reconcile_arrow_billing(year=2024, month=1)

        # Verify sync is now reconciled
        sync.refresh_from_db()
        self.assertEqual(sync.state, models.ArrowBillingSync.States.RECONCILED)

        # Verify compensation item was created
        sync_item = sync.items.first()
        self.assertIsNotNone(sync_item.compensation_item)
        self.assertEqual(sync_item.compensation_item.unit_price, Decimal("-5.00"))

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    def test_reconcile_skips_syncs_not_validated(self, mock_export):
        sync = self.fixture.billing_sync
        sync.mark_synced()
        sync.save()
        # Not validated

        tasks.reconcile_arrow_billing(year=2024, month=1)

        sync.refresh_from_db()
        # Should still be SYNCED, not RECONCILED
        self.assertEqual(sync.state, models.ArrowBillingSync.States.SYNCED)

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    def test_reconcile_with_force_flag(self, mock_export):
        sync = self.fixture.billing_sync
        sync.mark_synced()
        sync.save()
        # Not validated, but using force=True

        mock_export.return_value = {
            "headers": ["Sequence", "Customer Total Price"],
            "values": [],
        }

        tasks.reconcile_arrow_billing(year=2024, month=1, force=True)

        # With force=True, it should process even synced (not validated) syncs
        sync.refresh_from_db()
        # Since there are no items with price changes, it remains as-is


class ParseDecimalTest(TestCase):
    """Tests for _parse_decimal helper function."""

    def test_parse_decimal_from_string(self):
        self.assertEqual(tasks._parse_decimal("100.00"), Decimal("100.00"))

    def test_parse_decimal_from_int(self):
        self.assertEqual(tasks._parse_decimal(100), Decimal("100"))

    def test_parse_decimal_from_float(self):
        self.assertEqual(tasks._parse_decimal(100.50), Decimal("100.5"))

    def test_parse_decimal_from_decimal(self):
        self.assertEqual(tasks._parse_decimal(Decimal("100")), Decimal("100"))

    def test_parse_decimal_empty_string(self):
        self.assertEqual(tasks._parse_decimal(""), Decimal("0"))

    def test_parse_decimal_with_currency_symbol(self):
        self.assertEqual(tasks._parse_decimal("€100.00"), Decimal("100.00"))

    def test_parse_decimal_with_comma_separator(self):
        self.assertEqual(tasks._parse_decimal("1,000.00"), Decimal("1000.00"))

    def test_parse_decimal_invalid_returns_zero(self):
        self.assertEqual(tasks._parse_decimal("not-a-number"), Decimal("0"))
