"""Preview and apply Arrow billing actions through billing_changes."""

import threading
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from dateutil.relativedelta import relativedelta
from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.waldur_arrow import billing_changes, models, tasks
from waldur_mastermind.waldur_arrow.backend import ArrowBackendError
from waldur_mastermind.waldur_arrow.tests.fixtures import ArrowFixture

URL = "/api/admin/arrow/billing-syncs/billing_changes/"
JOB_URL = "/api/admin/arrow/billing-syncs/billing_changes_job/"
EXPORT = "waldur_mastermind.waldur_arrow.backend.ArrowClient.export_billing_all_pages"
CONSUMPTION = (
    "waldur_mastermind.waldur_arrow.backend.ArrowClient.get_monthly_consumption"
)
PARSE = "waldur_mastermind.waldur_arrow.backend.ArrowClient.parse_consumption_to_dicts"
LAST_MONTH = date.today().replace(day=1) - relativedelta(months=1)
HEADERS = [
    "Sequence",
    "End User Company Name",
    "ARS Subscription ID",
    "Customer Total Price",
    "Total Wholesale Price",
    "Qty",
]


class BillingChangesTest(test.APITestCase):
    def setUp(self):
        self.fixture = ArrowFixture()
        self.fixture.arrow_settings
        self.mapping = self.fixture.customer_mapping
        self.client.force_authenticate(self.fixture.user)

    def start(self, **body):
        return self.client.post(
            URL,
            {"year": LAST_MONTH.year, "month": LAST_MONTH.month, **body},
            format="json",
        )

    def post(self, **body):
        """Start a job, run it as the worker would, and return its report."""
        with (
            mock.patch.object(
                tasks.run_arrow_billing_changes,
                "delay",
                side_effect=lambda **kw: tasks.run_arrow_billing_changes(**kw),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            started = self.start(**body)
        if started.status_code != status.HTTP_202_ACCEPTED:
            return started
        job = self.client.get(JOB_URL, {"job_id": started.data["job_id"]})
        self.assertEqual(job.status_code, status.HTTP_200_OK, job.data)
        self.assertEqual(job.data["state"], "done", job.data)
        job.data = job.data["report"]
        return job

    def export(self, amount):
        return {
            "headers": HEADERS,
            "values": [
                ["1", self.mapping.arrow_company_name, "XSP-SEAT", amount, "1", "1"]
            ],
        }

    @mock.patch(EXPORT)
    def test_dry_run_reports_and_keeps_nothing(self, mock_export):
        mock_export.return_value = self.export("100.00")

        response = self.post(action="sync_billing")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["dry_run"])
        [invoice] = response.data["invoices"]
        self.assertEqual(invoice["customer_name"], self.fixture.customer.name)
        self.assertEqual(Decimal(invoice["total_before"]), Decimal("0"))
        self.assertEqual(Decimal(invoice["total_after"]), Decimal("100.00"))
        [item] = invoice["items"]
        self.assertEqual(item["change"], "created")
        self.assertEqual(Decimal(item["amount_after"]), Decimal("100.00"))
        self.assertFalse(invoice_models.InvoiceItem.objects.exists())
        self.assertFalse(models.ArrowBillingSync.objects.exists())

    @mock.patch(EXPORT)
    def test_apply_keeps_the_previewed_changes(self, mock_export):
        mock_export.return_value = self.export("100.00")
        preview = self.post(action="sync_billing").data

        applied = self.post(action="sync_billing", dry_run=False)

        self.assertEqual(applied.status_code, status.HTTP_200_OK, applied.data)
        self.assertFalse(applied.data["dry_run"])
        self.assertEqual(
            [i["items"][0]["amount_after"] for i in applied.data["invoices"]],
            [i["items"][0]["amount_after"] for i in preview["invoices"]],
        )
        self.assertEqual(invoice_models.InvoiceItem.objects.count(), 1)

    @mock.patch(EXPORT)
    def test_updated_item_shows_old_and_new_amount(self, mock_export):
        mock_export.return_value = self.export("100.00")
        self.post(action="sync_billing", dry_run=False)
        mock_export.return_value = self.export("130.00")

        response = self.post(action="sync_billing")

        [invoice] = response.data["invoices"]
        [item] = invoice["items"]
        self.assertEqual(item["change"], "updated")
        self.assertEqual(Decimal(item["amount_before"]), Decimal("100.00"))
        self.assertEqual(Decimal(item["amount_after"]), Decimal("130.00"))
        self.assertEqual(
            invoice_models.InvoiceItem.objects.get().unit_price, Decimal("100.00")
        )

    @mock.patch(PARSE)
    @mock.patch(CONSUMPTION)
    def test_consumption_sync_can_be_limited_to_one_resource(
        self, mock_consumption, mock_parse
    ):
        mock_consumption.return_value = {"headers": [], "lines": []}
        mock_parse.return_value = [
            {"Total sell price": "12.00", "Total buy price": "9.00"}
        ]
        project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        offering = marketplace_factories.OfferingFactory()
        resources = [
            marketplace_models.Resource.objects.create(
                name=f"Sub {i}",
                offering=offering,
                project=project,
                state=marketplace_models.Resource.States.OK,
                attributes={"arrow_license_reference": f"XSP-{i}"},
            )
            for i in range(2)
        ]

        response = self.post(
            action="sync_consumption", resource_uuid=resources[0].uuid.hex
        )

        [invoice] = response.data["invoices"]
        self.assertEqual(
            [i["resource_name"] for i in invoice["items"]], [resources[0].name]
        )
        self.assertFalse(models.ArrowConsumptionRecord.objects.exists())

    def test_running_month_cannot_be_settled(self):
        today = date.today()

        response = self.client.post(
            URL,
            {"action": "settle_month", "year": today.year, "month": today.month},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_settling_cannot_be_limited_to_a_resource(self):
        response = self.start(
            action="settle_month",
            resource_uuid=marketplace_factories.ResourceFactory().uuid.hex,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("resource_uuid", response.data)

    def test_resource_without_license_is_rejected(self):
        # Dropping the filter would sync every customer instead.
        resource = marketplace_factories.ResourceFactory(backend_id="sub-1")

        response = self.start(action="sync_billing", resource_uuid=resource.uuid.hex)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("resource_uuid", response.data)

    @mock.patch(EXPORT)
    def test_failed_arrow_call_is_reported(self, mock_export):
        mock_export.side_effect = ArrowBackendError("Arrow is down")

        response = self.post(action="sync_billing")

        self.assertEqual(response.data["invoices"], [])
        self.assertTrue(
            any("Arrow is down" in p for p in response.data["problems"]),
            response.data["problems"],
        )

    def test_one_job_at_a_time(self):
        models.ArrowBillingChangesJob.objects.create(
            action="sync_billing", year=LAST_MONTH.year, month=LAST_MONTH.month
        )

        response = self.start(action="sync_billing")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_job_killed_with_its_worker_is_reported_failed(self):
        job = models.ArrowBillingChangesJob.objects.create(
            action="sync_billing", year=LAST_MONTH.year, month=LAST_MONTH.month
        )
        models.ArrowBillingChangesJob.objects.filter(pk=job.pk).update(
            created=timezone.now() - billing_changes.STALE_AFTER - timedelta(minutes=1)
        )

        response = self.client.get(JOB_URL, {"job_id": job.uuid.hex})

        self.assertEqual(response.data["state"], "failed")
        self.assertTrue(response.data["error"])
        # A dead job does not block the next one.
        self.assertEqual(
            self.start(action="sync_billing").status_code, status.HTTP_202_ACCEPTED
        )

    @mock.patch(PARSE)
    @mock.patch(CONSUMPTION)
    def test_legacy_license_in_backend_id_is_synced(self, mock_consumption, mock_parse):
        mock_consumption.return_value = {"headers": [], "lines": []}
        mock_parse.return_value = [
            {"Total sell price": "12.00", "Total buy price": "9.00"}
        ]
        resource = marketplace_models.Resource.objects.create(
            name="Legacy link",
            offering=marketplace_factories.OfferingFactory(),
            project=structure_factories.ProjectFactory(customer=self.fixture.customer),
            state=marketplace_models.Resource.States.OK,
            backend_id="XSP-LEGACY",
        )

        response = self.post(action="sync_consumption", resource_uuid=resource.uuid.hex)

        [invoice] = response.data["invoices"]
        self.assertEqual(invoice["items"][0]["resource_name"], "Legacy link")
        self.assertEqual(
            mock_consumption.call_args.kwargs["license_reference"], "XSP-LEGACY"
        )

    def test_unknown_job_is_not_found(self):
        response = self.client.get(JOB_URL, {"job_id": "0" * 32})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_recorder_ignores_other_threads(self):
        invoice = invoice_models.Invoice.objects.create(
            customer=self.fixture.customer, year=LAST_MONTH.year, month=LAST_MONTH.month
        )
        with billing_changes.InvoiceChangeRecorder() as recorder:
            worker = threading.Thread(
                target=lambda: recorder._post_save(
                    invoice_models.InvoiceItem,
                    invoice_models.InvoiceItem(pk=1, invoice=invoice),
                    created=True,
                )
            )
            worker.start()
            worker.join()

        self.assertEqual(recorder.created, set())

    def test_staff_only(self):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.post(action="sync_billing")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
