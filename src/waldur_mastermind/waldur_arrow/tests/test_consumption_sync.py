"""Tests for Arrow consumption sync and reconciliation functionality."""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.waldur_arrow import models, tasks
from waldur_mastermind.waldur_arrow.backend import ArrowBackendError
from waldur_mastermind.waldur_arrow.tests.fixtures import ArrowFixture


class ArrowConsumptionRecordModelTest(TestCase):
    """Tests for ArrowConsumptionRecord model."""

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.project = structure_factories.ProjectFactory()
        self.resource = marketplace_models.Resource.objects.create(
            name="Test Resource",
            offering=self.offering,
            project=self.project,
            backend_id="test-sub-001",
            state=marketplace_models.Resource.States.OK,
            attributes={"arrow_license_reference": "XSP12345"},
        )

    def test_create_consumption_record(self):
        record = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("100.00"),
            consumed_buy=Decimal("80.00"),
        )

        self.assertEqual(record.resource, self.resource)
        self.assertEqual(record.license_reference, "XSP12345")
        self.assertFalse(record.is_finalized)
        self.assertFalse(record.is_reconciled)
        self.assertIsNone(record.adjustment_amount)

    def test_is_finalized(self):
        record = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("100.00"),
            consumed_buy=Decimal("80.00"),
        )

        self.assertFalse(record.is_finalized)

        record.finalized_at = timezone.now()
        record.save()

        self.assertTrue(record.is_finalized)

    def test_is_reconciled(self):
        record = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("100.00"),
            consumed_buy=Decimal("80.00"),
        )

        self.assertFalse(record.is_reconciled)

        record.reconciled_at = timezone.now()
        record.save()

        self.assertTrue(record.is_reconciled)

    def test_adjustment_amount(self):
        record = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("100.00"),
            consumed_buy=Decimal("80.00"),
        )

        # No adjustment before finalization
        self.assertIsNone(record.adjustment_amount)

        # Set final values
        record.final_sell = Decimal("95.00")
        record.final_buy = Decimal("76.00")
        record.save()

        # Adjustment is final - consumed
        self.assertEqual(record.adjustment_amount, Decimal("-5.00"))

    def test_get_invoice_item_details(self):
        record = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("100.00"),
            consumed_buy=Decimal("80.00"),
        )

        details = record.get_invoice_item_details()

        self.assertEqual(details["source"], "arrow_consumption")
        self.assertEqual(details["license_reference"], "XSP12345")
        self.assertEqual(details["sync_type"], "real_time")

    def test_get_compensation_details(self):
        record = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("100.00"),
            consumed_buy=Decimal("80.00"),
            final_sell=Decimal("95.00"),
            final_buy=Decimal("76.00"),
            reconciled_at=timezone.now(),
        )

        details = record.get_compensation_details()

        self.assertEqual(details["source"], "arrow_reconciliation")
        self.assertEqual(details["original_period"], "2024-12-01")
        self.assertEqual(details["consumed_sell"], "100.00")
        self.assertEqual(details["final_sell"], "95.00")
        self.assertEqual(details["adjustment"], "-5.00")

    def test_unique_together_constraint(self):
        models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("100.00"),
        )

        # Attempting to create duplicate should fail
        with self.assertRaises(Exception):
            models.ArrowConsumptionRecord.objects.create(
                resource=self.resource,
                license_reference="XSP12345",
                billing_period=date(2024, 12, 1),
                consumed_sell=Decimal("200.00"),
            )


class SyncResourceConsumptionTest(TestCase):
    """Tests for _sync_resource_consumption function."""

    def setUp(self):
        self.fixture = ArrowFixture()
        self.fixture.arrow_settings

        self.offering = marketplace_factories.OfferingFactory()
        # Create cloud_cost component
        marketplace_models.OfferingComponent.objects.create(
            offering=self.offering,
            type="cloud_cost",
            name="Cloud Cost",
            billing_type=BillingTypes.USAGE,
            measured_unit="EUR",
        )

        self.project = structure_factories.ProjectFactory()
        self.resource = marketplace_models.Resource.objects.create(
            name="Test Azure Sub",
            offering=self.offering,
            project=self.project,
            backend_id="azure-sub-001",
            state=marketplace_models.Resource.States.OK,
            attributes={"arrow_license_reference": "XSP12345"},
        )

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.get_monthly_consumption"
    )
    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.parse_consumption_to_dicts"
    )
    def test_creates_consumption_record(self, mock_parse, mock_get_consumption):
        mock_get_consumption.return_value = {"headers": [], "lines": []}
        mock_parse.return_value = [
            {"Total sell price": "100.00", "Total buy price": "80.00"},
            {"Total sell price": "50.00", "Total buy price": "40.00"},
        ]

        from waldur_mastermind.waldur_arrow.backend import ArrowClient, ArrowCredentials

        credentials = ArrowCredentials(api_url="https://test.api", api_key="key")
        client = ArrowClient(credentials)

        tasks._sync_resource_consumption(
            client=client,
            resource=self.resource,
            license_ref="XSP12345",
            billing_period=date(2024, 12, 1),
            period="2024-12",
        )

        record = models.ArrowConsumptionRecord.objects.get(
            resource=self.resource,
            billing_period=date(2024, 12, 1),
        )

        self.assertEqual(record.consumed_sell, Decimal("150.00"))
        self.assertEqual(record.consumed_buy, Decimal("120.00"))
        self.assertEqual(record.license_reference, "XSP12345")
        self.assertIsNotNone(record.last_sync_at)

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.get_monthly_consumption"
    )
    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.parse_consumption_to_dicts"
    )
    def test_updates_existing_record(self, mock_parse, mock_get_consumption):
        # Create existing record
        existing = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("50.00"),
            consumed_buy=Decimal("40.00"),
        )

        mock_get_consumption.return_value = {"headers": [], "lines": []}
        mock_parse.return_value = [
            {"Total sell price": "150.00", "Total buy price": "120.00"},
        ]

        from waldur_mastermind.waldur_arrow.backend import ArrowClient, ArrowCredentials

        credentials = ArrowCredentials(api_url="https://test.api", api_key="key")
        client = ArrowClient(credentials)

        tasks._sync_resource_consumption(
            client=client,
            resource=self.resource,
            license_ref="XSP12345",
            billing_period=date(2024, 12, 1),
            period="2024-12",
        )

        existing.refresh_from_db()
        self.assertEqual(existing.consumed_sell, Decimal("150.00"))
        self.assertEqual(existing.consumed_buy, Decimal("120.00"))

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.get_consumption_prediction"
    )
    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.get_monthly_consumption"
    )
    def test_falls_back_to_prediction_api(self, mock_get_consumption, mock_prediction):
        mock_get_consumption.side_effect = ArrowBackendError("API unavailable")
        mock_prediction.return_value = {
            "values": [
                {"consumed": {"sell": "100.00", "buy": "80.00"}},
                {"consumed": {"sell": "50.00", "buy": "40.00"}},
            ]
        }

        from waldur_mastermind.waldur_arrow.backend import ArrowClient, ArrowCredentials

        credentials = ArrowCredentials(api_url="https://test.api", api_key="key")
        client = ArrowClient(credentials)

        tasks._sync_resource_consumption(
            client=client,
            resource=self.resource,
            license_ref="XSP12345",
            billing_period=date(2024, 12, 1),
            period="2024-12",
        )

        record = models.ArrowConsumptionRecord.objects.get(
            resource=self.resource,
            billing_period=date(2024, 12, 1),
        )

        self.assertEqual(record.consumed_sell, Decimal("150.00"))
        self.assertEqual(record.consumed_buy, Decimal("120.00"))


class ReconcileConsumptionRecordTest(TestCase):
    """
    _reconcile_consumption_record settles final billing against what was
    invoiced, on the current invoice.
    """

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.project = structure_factories.ProjectFactory()
        self.resource = marketplace_models.Resource.objects.create(
            name="Test Azure Sub",
            offering=self.offering,
            project=self.project,
            backend_id="azure-sub-001",
            state=marketplace_models.Resource.States.OK,
            attributes={"arrow_license_reference": "XSP12345"},
        )

    def _billed_record(self, sell="100.00", buy="80.00", price_source="sell"):
        """A record invoiced the way the consumption sync invoices it."""
        record = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal(sell),
            consumed_buy=Decimal(buy),
        )
        tasks._update_provisional_invoice_item(
            record, self.resource, price_source=price_source
        )
        record.refresh_from_db()
        return record

    def _reconcile(self, record, sell, buy, price_source="sell"):
        return tasks._reconcile_consumption_record(
            record=record,
            billing_info={"sell_total": Decimal(sell), "buy_total": Decimal(buy)},
            price_source=price_source,
        )

    def test_reconciles_with_adjustment(self):
        record = self._billed_record()

        self.assertTrue(self._reconcile(record, "95.00", "76.00"))

        record.refresh_from_db()
        self.assertTrue(record.is_finalized)
        self.assertTrue(record.is_reconciled)
        self.assertEqual(record.final_sell, Decimal("95.00"))
        self.assertEqual(record.final_buy, Decimal("76.00"))
        comp_item = record.compensation_item
        self.assertEqual(comp_item.unit_price, Decimal("-5.00"))
        self.assertIn("credit", comp_item.name.lower())
        today = date.today()
        self.assertEqual(
            (comp_item.invoice.year, comp_item.invoice.month),
            (today.year, today.month),
        )

    def test_reconciles_with_additional_charge(self):
        record = self._billed_record()

        self.assertTrue(self._reconcile(record, "110.00", "88.00"))

        record.refresh_from_db()
        self.assertEqual(record.final_sell, Decimal("110.00"))
        self.assertEqual(record.compensation_item.unit_price, Decimal("10.00"))
        self.assertIn("additional charge", record.compensation_item.name.lower())

    def test_no_compensation_when_amounts_match(self):
        record = self._billed_record()

        self.assertFalse(self._reconcile(record, "100.00", "80.00"))

        record.refresh_from_db()
        self.assertTrue(record.is_finalized)
        self.assertTrue(record.is_reconciled)
        self.assertIsNone(record.compensation_item)

    def test_unbilled_record_is_charged_in_full(self):
        """A record that never reached an invoice owes the whole final amount."""
        record = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("100.00"),
            consumed_buy=Decimal("80.00"),
        )

        self.assertTrue(self._reconcile(record, "95.00", "76.00"))

        record.refresh_from_db()
        self.assertEqual(record.compensation_item.unit_price, Decimal("95.00"))

    def test_compares_with_invoiced_amount_not_consumption(self):
        """An issued item keeps its amount; consumption may have moved on."""
        record = self._billed_record()
        record.invoice_item.invoice.state = invoice_models.Invoice.States.CREATED
        record.invoice_item.invoice.save()
        record.consumed_sell = Decimal("140.00")
        record.save()

        self.assertTrue(self._reconcile(record, "150.00", "120.00"))

        record.refresh_from_db()
        self.assertEqual(record.invoice_item.unit_price, Decimal("100.00"))
        self.assertEqual(record.compensation_item.unit_price, Decimal("50.00"))

    def test_rereconcile_updates_pending_compensation(self):
        record = self._billed_record()
        self._reconcile(record, "95.00", "76.00")

        self.assertTrue(self._reconcile(record, "90.00", "72.00"))

        record.refresh_from_db()
        self.assertEqual(record.final_sell, Decimal("90.00"))
        self.assertEqual(record.compensation_item.unit_price, Decimal("-10.00"))
        self.assertEqual(
            invoice_models.InvoiceItem.objects.filter(
                details__source="arrow_reconciliation"
            ).count(),
            1,
        )

    def test_issued_compensation_gets_a_follow_up(self):
        """Once a compensation is issued, a later difference gets its own."""
        record = self._billed_record()
        self._reconcile(record, "95.00", "76.00")
        record.refresh_from_db()
        first = record.compensation_item
        first.invoice.state = invoice_models.Invoice.States.CREATED
        first.invoice.save()

        self.assertTrue(self._reconcile(record, "90.00", "72.00"))

        record.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(first.unit_price, Decimal("-5.00"))
        self.assertNotEqual(record.compensation_item, first)
        self.assertEqual(record.compensation_item.unit_price, Decimal("-5.00"))
        self.assertEqual(tasks._record_invoiced_amount(record), Decimal("90.00"))

    def test_reconciles_using_buy_price_source(self):
        record = self._billed_record(price_source="buy")

        self.assertTrue(self._reconcile(record, "95.00", "70.00", price_source="buy"))

        record.refresh_from_db()
        self.assertEqual(record.final_buy, Decimal("70.00"))
        self.assertEqual(record.compensation_item.unit_price, Decimal("-10.00"))
        self.assertIn("credit", record.compensation_item.name.lower())

    def test_no_compensation_buy_price_when_buy_amounts_match(self):
        record = self._billed_record(price_source="buy")

        self.assertFalse(self._reconcile(record, "95.00", "80.00", price_source="buy"))

        record.refresh_from_db()
        self.assertTrue(record.is_finalized)
        self.assertIsNone(record.compensation_item)


class GroupBillingBySubscriptionTest(TestCase):
    """Tests for _group_billing_by_subscription function."""

    def test_groups_by_subscription_id(self):
        billing_lines = [
            {
                "Vendor Subscription ID": "sub-001",
                "Customer Total Price": "100.00",
                "Total Wholesale Price": "80.00",
            },
            {
                "Vendor Subscription ID": "sub-001",
                "Customer Total Price": "50.00",
                "Total Wholesale Price": "40.00",
            },
            {
                "Vendor Subscription ID": "sub-002",
                "Customer Total Price": "200.00",
                "Total Wholesale Price": "160.00",
            },
        ]

        result = tasks._group_billing_by_subscription(billing_lines)

        self.assertEqual(len(result), 2)
        self.assertIn("sub-001", result)
        self.assertIn("sub-002", result)

        self.assertEqual(result["sub-001"]["sell_total"], Decimal("150.00"))
        self.assertEqual(result["sub-001"]["buy_total"], Decimal("120.00"))
        self.assertEqual(len(result["sub-001"]["lines"]), 2)

        self.assertEqual(result["sub-002"]["sell_total"], Decimal("200.00"))
        self.assertEqual(result["sub-002"]["buy_total"], Decimal("160.00"))

    def test_skips_empty_subscription_id(self):
        billing_lines = [
            {
                "Vendor Subscription ID": "",
                "Customer Total Price": "100.00",
            },
            {
                "Vendor Subscription ID": "sub-001",
                "Customer Total Price": "50.00",
            },
        ]

        result = tasks._group_billing_by_subscription(billing_lines)

        self.assertEqual(len(result), 1)
        self.assertIn("sub-001", result)


class SyncArrowConsumptionTaskTest(TestCase):
    """Tests for sync_arrow_consumption task."""

    def setUp(self):
        self.fixture = ArrowFixture()
        self.fixture.arrow_settings

        self.offering = marketplace_factories.OfferingFactory()
        # Create cloud_cost component
        marketplace_models.OfferingComponent.objects.create(
            offering=self.offering,
            type="cloud_cost",
            name="Cloud Cost",
            billing_type=BillingTypes.USAGE,
            measured_unit="EUR",
        )

        self.project = structure_factories.ProjectFactory()

        # Create resources with arrow_license_reference
        self.resource1 = marketplace_models.Resource.objects.create(
            name="Azure Sub 1",
            offering=self.offering,
            project=self.project,
            backend_id="azure-sub-001",
            state=marketplace_models.Resource.States.OK,
            attributes={"arrow_license_reference": "XSP12345"},
        )

        self.resource2 = marketplace_models.Resource.objects.create(
            name="Azure Sub 2",
            offering=self.offering,
            project=self.project,
            backend_id="azure-sub-002",
            state=marketplace_models.Resource.States.OK,
            attributes={"arrow_license_reference": "XSP67890"},
        )

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.get_monthly_consumption"
    )
    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.parse_consumption_to_dicts"
    )
    def test_syncs_all_resources_with_license_reference(
        self, mock_parse, mock_get_consumption
    ):
        mock_get_consumption.return_value = {"headers": [], "lines": []}
        mock_parse.return_value = [
            {"Total sell price": "100.00", "Total buy price": "80.00"},
        ]

        result = tasks.sync_arrow_consumption(year=2024, month=12)

        self.assertEqual(result["synced"], 2)
        self.assertEqual(len(result["errors"]), 0)

        # Check records created for both resources
        self.assertTrue(
            models.ArrowConsumptionRecord.objects.filter(
                resource=self.resource1,
                billing_period=date(2024, 12, 1),
            ).exists()
        )
        self.assertTrue(
            models.ArrowConsumptionRecord.objects.filter(
                resource=self.resource2,
                billing_period=date(2024, 12, 1),
            ).exists()
        )

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.get_monthly_consumption"
    )
    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.parse_consumption_to_dicts"
    )
    def test_skips_finalized_records(self, mock_parse, mock_get_consumption):
        # Create finalized record
        models.ArrowConsumptionRecord.objects.create(
            resource=self.resource1,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("100.00"),
            consumed_buy=Decimal("80.00"),
            finalized_at=timezone.now(),
        )

        mock_get_consumption.return_value = {"headers": [], "lines": []}
        mock_parse.return_value = [
            {"Total sell price": "150.00", "Total buy price": "120.00"},
        ]

        result = tasks.sync_arrow_consumption(year=2024, month=12)

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["skipped_finalized"], 1)

    def test_returns_error_without_settings(self):
        # Remove settings
        models.ArrowSettings.objects.all().delete()

        result = tasks.sync_arrow_consumption(year=2024, month=12)

        self.assertIn("error", result)


class CheckAndReconcileBillingTaskTest(TestCase):
    """Tests for check_and_reconcile_billing task."""

    def setUp(self):
        self.fixture = ArrowFixture()
        self.fixture.arrow_settings

        self.offering = marketplace_factories.OfferingFactory()
        self.project = structure_factories.ProjectFactory()

        self.resource = marketplace_models.Resource.objects.create(
            name="Azure Sub 1",
            offering=self.offering,
            project=self.project,
            backend_id="azure-sub-001",
            state=marketplace_models.Resource.States.OK,
            attributes={"arrow_license_reference": "XSP12345"},
        )

        # Create unfinalized consumption record
        self.record = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            license_reference="XSP12345",
            billing_period=date(2024, 12, 1),
            consumed_sell=Decimal("100.00"),
            consumed_buy=Decimal("80.00"),
        )

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.parse_billing_export_to_dicts"
    )
    def test_reconciles_with_billing_data(self, mock_parse, mock_export):
        mock_export.return_value = {"headers": [], "values": []}
        mock_parse.return_value = [
            {
                "Vendor Subscription ID": "azure-sub-001",
                "Customer Total Price": "95.00",
                "Total Wholesale Price": "76.00",
                "ARS Subscription ID": "XSP12345",
            },
        ]

        result = tasks.check_and_reconcile_billing(year=2024, month=12)

        self.assertEqual(result["finalized"], 1)
        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(result["compensation_items_created"], 1)

        self.record.refresh_from_db()
        self.assertTrue(self.record.is_finalized)
        self.assertTrue(self.record.is_reconciled)
        self.assertIsNotNone(self.record.compensation_item)

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.parse_billing_export_to_dicts"
    )
    def test_handles_no_billing_data(self, mock_parse, mock_export):
        mock_export.return_value = {"headers": [], "values": []}
        mock_parse.return_value = []

        result = tasks.check_and_reconcile_billing(year=2024, month=12)

        self.assertEqual(result["status"], "no_data")

        self.record.refresh_from_db()
        self.assertFalse(self.record.is_finalized)

    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
    )
    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.parse_billing_export_to_dicts"
    )
    def test_matches_by_license_reference(self, mock_parse, mock_export):
        mock_export.return_value = {"headers": [], "values": []}
        mock_parse.return_value = [
            {
                "Vendor Subscription ID": "azure-sub-999",
                "Customer Total Price": "95.00",
                "Total Wholesale Price": "76.00",
                "ARS Subscription ID": "XSP12345",
            },
        ]

        result = tasks.check_and_reconcile_billing(year=2024, month=12)

        self.assertEqual(result["finalized"], 1)

        self.record.refresh_from_db()
        self.assertTrue(self.record.is_finalized)

    def test_returns_error_without_settings(self):
        models.ArrowSettings.objects.all().delete()

        result = tasks.check_and_reconcile_billing(year=2024, month=12)

        self.assertIn("error", result)


class ExtractPredictionTotalsTest(TestCase):
    """Tests for _extract_prediction_totals function."""

    def test_extracts_consumed_totals(self):
        prediction_data = {
            "values": [
                {"consumed": {"sell": "100.00", "buy": "80.00"}},
                {"consumed": {"sell": "50.00", "buy": "40.00"}},
            ]
        }

        sell, buy = tasks._extract_prediction_totals(prediction_data)

        self.assertEqual(sell, Decimal("150.00"))
        self.assertEqual(buy, Decimal("120.00"))

    def test_handles_missing_consumed(self):
        prediction_data = {
            "values": [
                {"estimatedMin": {"sell": "100.00"}},  # No consumed
            ]
        }

        sell, buy = tasks._extract_prediction_totals(prediction_data)

        self.assertEqual(sell, Decimal("0"))
        self.assertEqual(buy, Decimal("0"))

    def test_handles_empty_values(self):
        prediction_data = {"values": []}

        sell, buy = tasks._extract_prediction_totals(prediction_data)

        self.assertEqual(sell, Decimal("0"))
        self.assertEqual(buy, Decimal("0"))
