"""One Arrow charge per license and month, and issued invoices stay as issued."""

from datetime import date
from decimal import Decimal
from unittest import mock

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.waldur_arrow import models, tasks
from waldur_mastermind.waldur_arrow.tests.fixtures import ArrowFixture

EXPORT = "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
HEADERS = [
    "Sequence",
    "End User Company Name",
    "ARS Subscription ID",
    "Customer Total Price",
    "Total Wholesale Price",
    "Qty",
    "Vendor Name",
    "Statement Reference",
]
LAST_MONTH = date.today().replace(day=1) - relativedelta(months=1)


def export(*rows):
    return {"headers": HEADERS, "values": [list(row) for row in rows]}


class ArrowBillingTestBase(TestCase):
    def setUp(self):
        self.fixture = ArrowFixture()
        self.settings = self.fixture.arrow_settings
        self.mapping = self.fixture.customer_mapping
        self.company = self.mapping.arrow_company_name
        self.project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        self.resource = marketplace_models.Resource.objects.create(
            name="Azure subscription",
            offering=marketplace_factories.OfferingFactory(),
            project=self.project,
            state=marketplace_models.Resource.States.OK,
            attributes={"arrow_license_reference": "XSP-LIC-1"},
        )

    def invoice_total(self, year, month):
        return sum(
            (
                item.price
                for item in invoice_models.InvoiceItem.objects.filter(
                    invoice__customer=self.fixture.customer,
                    invoice__year=year,
                    invoice__month=month,
                )
            ),
            Decimal("0"),
        )

    def consumption(self, sell, period=LAST_MONTH):
        record, _ = models.ArrowConsumptionRecord.objects.update_or_create(
            resource=self.resource,
            billing_period=period,
            license_reference="XSP-LIC-1",
            defaults={"consumed_sell": Decimal(sell), "consumed_buy": Decimal("0")},
        )
        tasks._update_provisional_invoice_item(record, self.resource)
        record.refresh_from_db()
        return record

    def issue(self, year, month):
        invoice_models.Invoice.objects.filter(
            customer=self.fixture.customer, year=year, month=month
        ).update(state=invoice_models.Invoice.States.CREATED)


class ValidatedBillingCheckTest(ArrowBillingTestBase):
    @mock.patch(
        "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_sync"
    )
    def test_marks_an_ended_month_validated(self, mock_export_page):
        mock_export_page.return_value = {"data": {"headers": [], "values": []}}
        sync = self.fixture.billing_sync
        sync.mark_synced()
        sync.save()

        tasks.check_validated_billing()

        sync.refresh_from_db()
        self.assertEqual(sync.state, models.ArrowBillingSync.States.VALIDATED)


class ConsumptionAndBillingSyncTest(ArrowBillingTestBase):
    @mock.patch(EXPORT)
    def test_billing_sync_skips_licenses_billed_by_consumption(self, mock_export):
        self.consumption("123.45")
        mock_export.return_value = export(
            ("1", self.company, "XSP-LIC-1", "123.45", "100", "1", "Microsoft", "S1"),
            ("2", self.company, "XSP-SEAT-9", "20.00", "16", "1", "Microsoft", "S1"),
        )

        tasks.sync_arrow_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)

        # The consumption item plus the seat licence, nothing for XSP-LIC-1 again.
        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month), Decimal("143.45")
        )

    @mock.patch(EXPORT)
    def test_first_consumption_record_supersedes_a_pending_billing_item(
        self, mock_export
    ):
        mock_export.return_value = export(
            ("1", self.company, "XSP-LIC-1", "100.00", "80", "1", "Microsoft", "S1"),
        )
        tasks.sync_arrow_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)
        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month), Decimal("100.00")
        )

        self.consumption("123.45")

        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month), Decimal("123.45")
        )

    def test_consumption_does_not_change_an_issued_invoice(self):
        record = self.consumption("123.45")
        self.issue(LAST_MONTH.year, LAST_MONTH.month)

        self.consumption("200.00")

        record.refresh_from_db()
        self.assertEqual(record.consumed_sell, Decimal("200.00"))
        self.assertEqual(record.invoice_item.unit_price, Decimal("123.45"))

    def test_consumption_is_not_added_to_an_issued_invoice(self):
        invoice_models.Invoice.objects.create(
            customer=self.fixture.customer,
            year=LAST_MONTH.year,
            month=LAST_MONTH.month,
            state=invoice_models.Invoice.States.CREATED,
        )

        record = self.consumption("123.45")

        self.assertIsNone(record.invoice_item)
        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month), Decimal("0")
        )


class CurrentMonthTest(ArrowBillingTestBase):
    @mock.patch(EXPORT)
    def test_current_month_is_not_reconciled(self, mock_export):
        today = date.today()
        record = self.consumption("50.00", period=today.replace(day=1))
        mock_export.return_value = export(
            ("1", self.company, "XSP-LIC-1", "50.00", "40", "1", "Microsoft", "S1"),
        )

        result = tasks.check_and_reconcile_billing(year=today.year, month=today.month)

        self.assertEqual(result, {"status": "period_not_ended"})
        record.refresh_from_db()
        self.assertFalse(record.is_finalized)
        mock_export.assert_not_called()

    @override_config(ARROW_CONSUMPTION_SYNC_ENABLED=True)
    @mock.patch(
        "waldur_mastermind.waldur_arrow.tasks.check_and_reconcile_billing.delay"
    )
    def test_scheduled_check_only_reconciles_the_previous_month(self, mock_delay):
        tasks.check_billing_export_scheduled()

        mock_delay.assert_called_once_with(year=LAST_MONTH.year, month=LAST_MONTH.month)


class BillingSyncReconciliationTest(ArrowBillingTestBase):
    @mock.patch(EXPORT)
    def test_price_change_before_reconciliation_is_charged_once(self, mock_export):
        def line(price):
            return export(
                ("1", self.company, "XSP-SEAT-9", price, "0", "1", "Microsoft", "S1")
            )

        mock_export.return_value = line("100.00")
        tasks.sync_arrow_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)
        mock_export.return_value = line("150.00")
        tasks.sync_arrow_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)
        sync = models.ArrowBillingSync.objects.get()
        sync.mark_validated()
        sync.save()

        tasks.reconcile_arrow_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)

        today = date.today()
        charged = self.invoice_total(LAST_MONTH.year, LAST_MONTH.month)
        if (today.year, today.month) != (LAST_MONTH.year, LAST_MONTH.month):
            charged += self.invoice_total(today.year, today.month)
        self.assertEqual(charged, Decimal("150.00"))

    @mock.patch(EXPORT)
    def test_forced_reconcile_of_an_unvalidated_sync(self, mock_export):
        mock_export.return_value = export(
            ("1", self.company, "XSP-SEAT-9", "100.00", "0", "1", "Microsoft", "S1"),
        )
        tasks.sync_arrow_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)
        self.issue(LAST_MONTH.year, LAST_MONTH.month)
        mock_export.return_value = export(
            ("1", self.company, "XSP-SEAT-9", "120.00", "0", "1", "Microsoft", "S1"),
        )

        tasks.reconcile_arrow_billing(
            year=LAST_MONTH.year, month=LAST_MONTH.month, force=True
        )

        sync = models.ArrowBillingSync.objects.get()
        self.assertEqual(sync.state, models.ArrowBillingSync.States.RECONCILED)


class ImportedMonthTest(ArrowBillingTestBase):
    @mock.patch(EXPORT)
    def test_imported_month_is_not_charged_again(self, mock_export):
        tasks._create_invoices_for_customer(
            waldur_customer=self.fixture.customer,
            mapping=self.mapping,
            subscriptions={
                "vendor-sub-1": {  # keyed by Vendor Subscription ID
                    "license_reference": "XSP-LIC-1",
                    "name": "Azure",
                    "vendor": "Microsoft",
                    "periods": {
                        LAST_MONTH.strftime("%Y-%m"): {
                            "sell": "80.00",
                            "buy": "60.00",
                            "items": [
                                {
                                    "description": "Compute",
                                    "sell": "80.00",
                                    "buy": "60.00",
                                    "offer": "Azure plan",
                                }
                            ],
                        }
                    },
                }
            },
        )
        record = models.ArrowConsumptionRecord.objects.get(resource=self.resource)
        self.assertTrue(record.is_finalized)

        mock_export.return_value = export(
            ("1", self.company, "XSP-LIC-1", "80.00", "60", "1", "Microsoft", "S1"),
        )
        tasks.check_and_reconcile_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)

        today = date.today()
        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month)
            + self.invoice_total(today.year, today.month),
            Decimal("80.00"),
        )

    def test_running_month_is_not_imported(self):
        today = date.today()
        result = tasks._create_invoices_for_customer(
            waldur_customer=self.fixture.customer,
            mapping=self.mapping,
            subscriptions={
                "XSP-LIC-1": {
                    "name": "Azure",
                    "vendor": "Microsoft",
                    "periods": {
                        today.strftime("%Y-%m"): {
                            "sell": "10",
                            "buy": "8",
                            "items": [
                                {
                                    "description": "Compute",
                                    "sell": "10",
                                    "buy": "8",
                                    "offer": "Azure plan",
                                }
                            ],
                        }
                    },
                }
            },
        )

        self.assertEqual(result["items_created"], 0)
        self.assertFalse(models.ArrowConsumptionRecord.objects.exists())


class CloudCostPlanTest(TestCase):
    def test_cloud_cost_usage_is_not_priced(self):
        offering = marketplace_factories.OfferingFactory()
        tasks._ensure_arrow_offering_component(offering)

        plan = tasks._ensure_arrow_plan(offering)

        self.assertEqual(
            list(plan.components.values_list("price", flat=True)), [Decimal("0")]
        )


class CleanupKeepsIssuedChargesTest(ArrowBillingTestBase, test.APITestCase):
    def test_records_on_issued_invoices_are_kept(self):
        self.client.force_authenticate(self.fixture.user)
        record = self.consumption("123.45")
        self.issue(LAST_MONTH.year, LAST_MONTH.month)

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/cleanup_consumption/",
            {"dry_run": False},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["records_kept_issued"], 1)
        self.assertEqual(response.data["records_deleted"], 0)
        self.assertTrue(
            models.ArrowConsumptionRecord.objects.filter(pk=record.pk).exists()
        )

    def test_deleting_a_record_removes_its_pending_item(self):
        self.client.force_authenticate(self.fixture.user)
        self.consumption("123.45")

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/cleanup_consumption/",
            {"dry_run": False},
            format="json",
        )

        self.assertEqual(response.data["records_deleted"], 1)
        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month), Decimal("0")
        )


class ReviewScenariosTest(ArrowBillingTestBase):
    """Paths the code review found charging a license-month twice or never."""

    @mock.patch(EXPORT)
    def test_license_is_billed_in_a_month_without_a_consumption_record(
        self, mock_export
    ):
        two_months_ago = LAST_MONTH - relativedelta(months=1)
        self.consumption("10.00", period=two_months_ago)
        mock_export.return_value = export(
            ("1", self.company, "XSP-LIC-1", "40.00", "30", "1", "Microsoft", "S1"),
        )

        tasks.sync_arrow_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)

        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month), Decimal("40.00")
        )

    @mock.patch(EXPORT)
    def test_billing_item_without_resource_is_superseded(self, mock_export):
        self.resource.attributes = {}
        self.resource.save()
        mock_export.return_value = export(
            ("1", self.company, "XSP-LIC-1", "50.00", "40", "1", "Microsoft", "S1"),
        )
        tasks.sync_arrow_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)
        self.resource.attributes = {"arrow_license_reference": "XSP-LIC-1"}
        self.resource.save()

        self.consumption("50.00")

        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month), Decimal("50.00")
        )

    @mock.patch(EXPORT)
    def test_issued_billing_item_counts_in_reconciliation(self, mock_export):
        mock_export.return_value = export(
            ("1", self.company, "XSP-LIC-1", "50.00", "40", "1", "Microsoft", "S1"),
        )
        tasks.sync_arrow_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)
        self.issue(LAST_MONTH.year, LAST_MONTH.month)
        # A later consumption record for the issued month adds nothing there…
        self.consumption("50.00")

        tasks.check_and_reconcile_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)

        # …and reconciliation sees the issued billing item: nothing owed.
        record = models.ArrowConsumptionRecord.objects.get(resource=self.resource)
        self.assertIsNone(record.compensation_item)
        self.assertEqual(tasks._record_invoiced_amount(record), Decimal("50.00"))

    @mock.patch(EXPORT)
    def test_forced_reconcile_does_not_freeze_the_running_month(self, mock_export):
        today = date.today()
        record = self.consumption("50.00", period=today.replace(day=1))

        result = tasks.check_and_reconcile_billing(
            year=today.year, month=today.month, force_reconcile=True
        )

        self.assertEqual(result, {"status": "period_not_ended"})
        record.refresh_from_db()
        self.assertFalse(record.is_finalized)

    def test_finalized_record_gets_no_new_provisional_item(self):
        record = self.consumption("80.00")
        record.invoice_item.delete()
        record.refresh_from_db()
        record.finalized_at = record.modified
        record.save()

        tasks._update_provisional_invoice_item(record, self.resource)

        record.refresh_from_db()
        self.assertIsNone(record.invoice_item)

    def test_import_skips_a_month_billed_by_consumption(self):
        self.consumption("100.00")

        result = tasks._create_invoices_for_customer(
            waldur_customer=self.fixture.customer,
            mapping=self.mapping,
            subscriptions={
                "vendor-sub-1": {
                    "license_reference": "XSP-LIC-1",
                    "name": "Azure",
                    "vendor": "Microsoft",
                    "periods": {
                        LAST_MONTH.strftime("%Y-%m"): {
                            "sell": "100.00",
                            "buy": "80.00",
                            "items": [
                                {
                                    "description": "Compute",
                                    "sell": "100.00",
                                    "buy": "80.00",
                                    "offer": "Azure plan",
                                }
                            ],
                        }
                    },
                }
            },
        )

        self.assertEqual(result["items_created"], 0)
        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month), Decimal("100.00")
        )


class CleanupKeepsImportedRecordsTest(ArrowBillingTestBase, test.APITestCase):
    def test_imported_record_is_kept(self):
        self.client.force_authenticate(self.fixture.user)
        models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            billing_period=LAST_MONTH,
            license_reference="XSP-LIC-1",
            consumed_sell=Decimal("80"),
            consumed_buy=Decimal("60"),
            raw_data={"source": "arrow_import"},
        )

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/cleanup_consumption/",
            {"dry_run": False},
            format="json",
        )

        self.assertEqual(response.data["records_kept_imported"], 1)
        self.assertTrue(models.ArrowConsumptionRecord.objects.exists())


class SecondReviewScenariosTest(ArrowBillingTestBase):
    """Ledger gaps found in the second review."""

    def _billing_item(self, period, invoice, amount, license_ref="XSP-LIC-1", **extra):
        mapping = self.mapping
        sync, _ = models.ArrowBillingSync.objects.get_or_create(
            customer_mapping=mapping,
            statement_reference=f"S-{period}",
            defaults={"report_period": period, "invoice": invoice},
        )
        details = {"source": "arrow", "arrow_line_reference": f"L-{period}"}
        if license_ref:
            details["license_reference"] = license_ref
        item = invoice_models.InvoiceItem.objects.create(
            invoice=invoice,
            name="line",
            unit_price=Decimal(amount),
            quantity=Decimal("1"),
            unit=invoice_models.InvoiceItem.Units.QUANTITY,
            details=details,
            **extra,
        )
        return models.ArrowBillingSyncItem.objects.create(
            billing_sync=sync,
            arrow_line_reference=f"L-{period}",
            invoice_item=item,
            original_price=Decimal(amount),
            subscription_reference=extra.pop("subscription", "")
            if "subscription" in extra
            else "",
        )

    def _invoice(self, period):
        return invoice_models.Invoice.objects.get_or_create(
            customer=self.fixture.customer, year=period.year, month=period.month
        )[0]

    def test_late_billing_item_counts_only_for_its_own_month(self):
        earlier = LAST_MONTH - relativedelta(months=1)
        # Billed late: earlier month's line on LAST_MONTH's invoice.
        self._billing_item(
            earlier.strftime("%Y-%m"), self._invoice(LAST_MONTH), "40.00"
        )

        items = tasks.license_month_items(
            self.fixture.customer, "XSP-LIC-1", LAST_MONTH, resource=self.resource
        )

        self.assertFalse(items.exists())

    def test_billing_compensation_is_in_the_ledger(self):
        period = LAST_MONTH.strftime("%Y-%m")
        sync_item = self._billing_item(period, self._invoice(LAST_MONTH), "50.00")
        compensation = invoice_models.InvoiceItem.objects.create(
            invoice=self._invoice(date.today().replace(day=1)),
            name="adjustment",
            unit_price=Decimal("10.00"),
            quantity=Decimal("1"),
            unit=invoice_models.InvoiceItem.Units.QUANTITY,
            details={"source": "arrow_reconciliation", "original_period": period},
        )
        sync_item.compensation_item = compensation
        sync_item.save()

        items = tasks.license_month_items(
            self.fixture.customer, "XSP-LIC-1", LAST_MONTH, resource=self.resource
        )

        self.assertEqual(sum(i.price for i in items), Decimal("60.00"))

    def test_legacy_billing_item_found_by_vendor_subscription(self):
        self.resource.backend_id = "vendor-sub-1"
        self.resource.save()
        sync_item = self._billing_item(
            LAST_MONTH.strftime("%Y-%m"),
            self._invoice(LAST_MONTH),
            "50.00",
            license_ref="",
        )
        sync_item.subscription_reference = "vendor-sub-1"
        sync_item.save()

        items = tasks.license_month_items(
            self.fixture.customer, "XSP-LIC-1", LAST_MONTH, resource=self.resource
        )

        self.assertEqual(sum(i.price for i in items), Decimal("50.00"))

    def test_import_skips_a_month_the_billing_sync_charged(self):
        self._billing_item(
            LAST_MONTH.strftime("%Y-%m"), self._invoice(LAST_MONTH), "100.00"
        )

        result = tasks._create_invoices_for_customer(
            waldur_customer=self.fixture.customer,
            mapping=self.mapping,
            subscriptions={
                "vendor-sub-1": {
                    "license_reference": "XSP-LIC-1",
                    "name": "Azure",
                    "vendor": "Microsoft",
                    "periods": {
                        LAST_MONTH.strftime("%Y-%m"): {
                            "sell": "100.00",
                            "buy": "80.00",
                            "items": [
                                {
                                    "description": "Compute",
                                    "sell": "100.00",
                                    "buy": "80.00",
                                    "offer": "Azure plan",
                                }
                            ],
                        }
                    },
                }
            },
        )

        self.assertEqual(result["items_created"], 0)
        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month), Decimal("100.00")
        )

    def test_settled_record_keeps_its_import_marker(self):
        record = models.ArrowConsumptionRecord.objects.create(
            resource=self.resource,
            billing_period=LAST_MONTH,
            license_reference="XSP-LIC-1",
            consumed_sell=Decimal("80"),
            consumed_buy=Decimal("60"),
            finalized_at=self.resource.created,
            raw_data={"source": "arrow_import"},
        )
        client = mock.Mock()
        client.get_monthly_consumption.return_value = {"headers": [], "lines": []}
        client.parse_consumption_to_dicts.return_value = [
            {"Total sell price": "999", "Total buy price": "1"}
        ]

        synced = tasks._sync_resource_consumption(
            client=client,
            resource=self.resource,
            license_ref="XSP-LIC-1",
            billing_period=LAST_MONTH,
            period=LAST_MONTH.strftime("%Y-%m"),
        )

        record.refresh_from_db()
        self.assertFalse(synced)
        self.assertEqual(record.raw_data, {"source": "arrow_import"})
        self.assertEqual(record.consumed_sell, Decimal("80"))

    @mock.patch(EXPORT)
    def test_per_resource_billing_sync_matches_the_license(self, mock_export):
        self.resource.backend_id = "vendor-sub-1"
        self.resource.save()
        mock_export.return_value = export(
            ("1", self.company, "XSP-LIC-1", "70.00", "50", "1", "Microsoft", "S1"),
            ("2", self.company, "XSP-OTHER", "30.00", "20", "1", "Microsoft", "S1"),
        )

        tasks.sync_arrow_billing(
            year=LAST_MONTH.year,
            month=LAST_MONTH.month,
            resource_uuid=self.resource.uuid.hex,
        )

        self.assertEqual(
            self.invoice_total(LAST_MONTH.year, LAST_MONTH.month), Decimal("70.00")
        )

    @mock.patch(EXPORT)
    def test_sync_with_only_consumption_billed_lines_is_reconciled(self, mock_export):
        self.consumption("50.00")
        mock_export.return_value = export(
            ("1", self.company, "XSP-LIC-1", "50.00", "40", "1", "Microsoft", "S1"),
        )
        tasks.sync_arrow_billing(year=LAST_MONTH.year, month=LAST_MONTH.month)
        sync = models.ArrowBillingSync.objects.get()

        tasks.reconcile_arrow_billing(
            year=LAST_MONTH.year, month=LAST_MONTH.month, force=True
        )

        sync.refresh_from_db()
        self.assertEqual(sync.state, models.ArrowBillingSync.States.RECONCILED)
