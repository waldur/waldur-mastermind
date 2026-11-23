"""Tests for Arrow Celery tasks."""

from datetime import date
from decimal import Decimal
from importlib import import_module
from unittest import mock

from django.apps import apps as django_apps
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
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
    def test_line_total_is_not_multiplied_by_quantity(self, mock_export):
        self.fixture.arrow_settings
        self.fixture.customer_mapping

        mock_export.return_value = {
            "headers": [
                "Sequence",
                "End User Company Name",
                "Customer Unit Price",
                "Customer Total Price",
                "Qty",
            ],
            "values": [
                [
                    "LINE-001",
                    self.fixture.customer_mapping.arrow_company_name,
                    "100.00",
                    "300.00",
                    "3",
                ],
            ],
        }

        tasks.sync_arrow_billing(year=2024, month=5)

        invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer, year=2024, month=5
        )
        item = invoice.items.get()
        self.assertEqual(item.price, Decimal("300.00"))
        self.assertEqual(item.details["arrow_quantity"], "3")
        sync = models.ArrowBillingSync.objects.get(report_period="2024-05")
        self.assertEqual(sync.sell_total, item.price)

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    def test_resync_bills_a_legacy_item_once(self, mock_export):
        """Items created before line totals were billed once carry quantity=Qty."""
        self.fixture.arrow_settings
        mapping = self.fixture.customer_mapping
        mock_export.return_value = {
            "headers": [
                "Sequence",
                "End User Company Name",
                "Customer Total Price",
                "Qty",
            ],
            "values": [["LINE-001", mapping.arrow_company_name, "300.00", "3"]],
        }
        tasks.sync_arrow_billing(year=2024, month=6)
        item = invoice_models.InvoiceItem.objects.get(
            invoice__year=2024, invoice__month=6
        )
        item.quantity = Decimal("3")  # as the old code stored it
        item.save()

        tasks.sync_arrow_billing(year=2024, month=6)

        item.refresh_from_db()
        self.assertEqual(item.quantity, Decimal("1"))
        self.assertEqual(item.price, Decimal("300.00"))

    def _line(self, total, qty="1"):
        mapping = self.fixture.customer_mapping
        return {
            "headers": [
                "Sequence",
                "End User Company Name",
                "Customer Total Price",
                "Qty",
            ],
            "values": [["LINE-001", mapping.arrow_company_name, total, qty]],
        }

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    def test_resync_leaves_an_issued_invoice_alone(self, mock_export):
        self.fixture.arrow_settings
        mock_export.return_value = self._line("100.00")
        tasks.sync_arrow_billing(year=2024, month=7)
        invoice = invoice_models.Invoice.objects.get(year=2024, month=7)
        invoice.state = invoice_models.Invoice.States.CREATED
        invoice.save()

        mock_export.return_value = self._line("120.00")
        tasks.sync_arrow_billing(year=2024, month=7)

        self.assertEqual(invoice.items.get().price, Decimal("100.00"))

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    def test_resync_keeps_a_manual_price_correction(self, mock_export):
        self.fixture.arrow_settings
        mock_export.return_value = self._line("100.00")
        tasks.sync_arrow_billing(year=2024, month=8)
        item = invoice_models.InvoiceItem.objects.get(
            invoice__year=2024, invoice__month=8
        )
        item.unit_price = Decimal("90.00")  # staff discount
        item.save()

        tasks.sync_arrow_billing(year=2024, month=8)
        item.refresh_from_db()
        self.assertEqual(item.unit_price, Decimal("90.00"))

        mock_export.return_value = self._line("130.00")  # Arrow changed it
        tasks.sync_arrow_billing(year=2024, month=8)
        item.refresh_from_db()
        self.assertEqual(item.unit_price, Decimal("130.00"))

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
        self.assertTrue(
            sync_item.compensation_item.name.startswith(
                f"Arrow consumption adjustment for {tasks._month_name(sync.report_period)}"
            ),
            sync_item.compensation_item.name,
        )

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


class MatchVendorOfferingTest(TestCase):
    def test_catalog_name_matches_the_export_spelling(self):
        offering = object()
        mapping = {"Amazon": offering, "Microsoft": object()}

        self.assertIs(
            tasks._match_vendor_offering(mapping, "Amazon Web Services"), offering
        )
        self.assertIsNone(tasks._match_vendor_offering({"IBM": offering}, "IBMX Corp"))
        longest = object()
        self.assertIs(
            tasks._match_vendor_offering(
                {"Microsoft": offering, "Microsoft Azure": longest},
                "Microsoft Azure Plan",
            ),
            longest,
        )
        self.assertIs(tasks._match_vendor_offering(mapping, "Amazon"), offering)
        self.assertIsNone(tasks._match_vendor_offering(mapping, "Red Hat"))
        self.assertIsNone(tasks._match_vendor_offering(mapping, ""))


class BillLineTotalsOnceMigrationTest(TestCase):
    def test_fixes_open_invoices_and_their_totals_only(self):
        migration = import_module(
            "waldur_mastermind.waldur_arrow.migrations.0008_bill_arrow_line_totals_once"
        )
        customer = structure_factories.CustomerFactory()
        open_invoice = invoice_models.Invoice.objects.create(
            customer=customer, year=2024, month=9
        )
        issued = invoice_models.Invoice.objects.create(
            customer=customer,
            year=2024,
            month=8,
            state=invoice_models.Invoice.States.CREATED,
        )
        for invoice in (open_invoice, issued):
            invoice_models.InvoiceItem.objects.create(
                invoice=invoice,
                name="line",
                unit_price=Decimal("300"),
                quantity=Decimal("3"),
                unit=invoice_models.InvoiceItem.Units.QUANTITY,
                details={"source": "arrow", "arrow_line_reference": "L1"},
            )
            invoice.update_cache()

        # A real migration runs on historical models, without the signal
        # that refreshes an invoice's cached totals on item save.
        with mock.patch.object(invoice_models.Invoice, "update_cache"):
            migration.bill_line_totals_once(django_apps, None)

        open_invoice.refresh_from_db()
        issued.refresh_from_db()
        self.assertEqual(open_invoice.items.get().quantity, Decimal("1"))
        self.assertEqual(open_invoice.total_price, Decimal("300.00"))
        self.assertEqual(issued.items.get().quantity, Decimal("3"))
        self.assertEqual(issued.total_price, Decimal("900.00"))


class AdjustmentMonthNameTest(TestCase):
    def test_month_name(self):
        self.assertEqual(tasks._month_name(date(2026, 10, 1)), "October 2026")
        self.assertEqual(tasks._month_name("2026-10"), "October 2026")
        self.assertEqual(tasks._month_name("unknown"), "unknown")
