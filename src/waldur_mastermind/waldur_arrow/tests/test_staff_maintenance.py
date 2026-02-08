"""Tests for Arrow staff maintenance API."""

from datetime import date
from decimal import Decimal
from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.waldur_arrow import models
from waldur_mastermind.waldur_arrow.tests.fixtures import ArrowFixture


class StaffMaintenanceFixture(ArrowFixture):
    """Extended fixture for staff maintenance tests."""

    @property
    def project(self):
        if not hasattr(self, "_project"):
            self._project = structure_factories.ProjectFactory(customer=self.customer)
        return self._project

    @property
    def offering(self):
        if not hasattr(self, "_offering"):
            self._offering = marketplace_factories.OfferingFactory(
                type="Marketplace.Basic",
            )
        return self._offering

    @property
    def resource(self):
        if not hasattr(self, "_resource"):
            self._resource = marketplace_factories.ResourceFactory(
                project=self.project,
                offering=self.offering,
                attributes={"arrow_license_reference": "XSP12345"},
            )
        return self._resource

    @property
    def consumption_record(self):
        if not hasattr(self, "_consumption_record"):
            self._consumption_record = models.ArrowConsumptionRecord.objects.create(
                resource=self.resource,
                license_reference="XSP12345",
                billing_period=date(2024, 1, 1),
                consumed_sell=Decimal("100.00"),
                consumed_buy=Decimal("80.00"),
            )
        return self._consumption_record

    @property
    def billing_sync_item(self):
        if not hasattr(self, "_billing_sync_item"):
            from waldur_mastermind.invoices import models as invoice_models

            invoice_item = invoice_models.InvoiceItem.objects.create(
                invoice=self.billing_sync.invoice,
                name="Test Item",
                unit_price=Decimal("100.00"),
            )
            self._billing_sync_item = models.ArrowBillingSyncItem.objects.create(
                billing_sync=self.billing_sync,
                arrow_line_reference="LINE-001",
                invoice_item=invoice_item,
                original_price=Decimal("100.00"),
                vendor_name="Microsoft",
                subscription_reference="SUB-001",
                classification="IAAS",
                description="Test line item",
            )
        return self._billing_sync_item


class TriggerConsumptionSyncTest(test.APITestCase):
    """Tests for trigger_consumption_sync action."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_requires_staff(self):
        non_staff = structure_factories.UserFactory(is_staff=False)
        self.client.force_authenticate(non_staff)

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/trigger_consumption_sync/",
            {"year": 2024, "month": 1},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_mastermind.waldur_arrow.tasks.sync_arrow_consumption.delay")
    def test_trigger_consumption_sync(self, mock_delay):
        response = self.client.post(
            "/api/admin/arrow/billing-syncs/trigger_consumption_sync/",
            {"year": 2024, "month": 1},
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_delay.assert_called_once()

    def test_validates_period(self):
        response = self.client.post(
            "/api/admin/arrow/billing-syncs/trigger_consumption_sync/",
            {"year": 2024, "month": 13},  # Invalid month
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TriggerReconciliationTest(test.APITestCase):
    """Tests for trigger_reconciliation action."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    @mock.patch(
        "waldur_mastermind.waldur_arrow.tasks.check_and_reconcile_billing.delay"
    )
    def test_trigger_reconciliation(self, mock_delay):
        response = self.client.post(
            "/api/admin/arrow/billing-syncs/trigger_reconciliation/",
            {"year": 2024, "month": 1},
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_delay.assert_called_once()


class CleanupConsumptionTest(test.APITestCase):
    """Tests for cleanup_consumption action."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_dry_run_by_default(self):
        self.fixture.consumption_record

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/cleanup_consumption/",
            {},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["dry_run"])
        self.assertEqual(response.data["records_to_delete"], 1)
        self.assertEqual(response.data["records_deleted"], 0)

        # Record should still exist
        self.assertEqual(models.ArrowConsumptionRecord.objects.count(), 1)

    def test_actual_delete(self):
        self.fixture.consumption_record

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/cleanup_consumption/",
            {"dry_run": False},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["dry_run"])
        self.assertEqual(response.data["records_deleted"], 1)

        # Record should be deleted
        self.assertEqual(models.ArrowConsumptionRecord.objects.count(), 0)

    def test_filter_by_period(self):
        self.fixture.consumption_record  # 2024-01

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/cleanup_consumption/",
            {"period_from": "2024-02", "dry_run": True},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["records_to_delete"], 0)

    def test_filter_finalized_only(self):
        record = self.fixture.consumption_record
        # Mark as finalized
        from django.utils import timezone

        record.finalized_at = timezone.now()
        record.save()

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/cleanup_consumption/",
            {"only_finalized": True, "dry_run": True},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["records_to_delete"], 1)

    def test_validates_mutually_exclusive_flags(self):
        response = self.client.post(
            "/api/admin/arrow/billing-syncs/cleanup_consumption/",
            {"only_finalized": True, "only_unfinalized": True},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PauseResumeSyncTest(test.APITestCase):
    """Tests for pause_sync and resume_sync actions."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_pause_settings_sync(self):
        settings = self.fixture.arrow_settings
        self.assertTrue(settings.sync_enabled)

        response = self.client.post("/api/admin/arrow/billing-syncs/pause_sync/", {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        settings.refresh_from_db()
        self.assertFalse(settings.sync_enabled)

    def test_resume_settings_sync(self):
        settings = self.fixture.arrow_settings
        settings.sync_enabled = False
        settings.save()

        response = self.client.post("/api/admin/arrow/billing-syncs/resume_sync/", {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        settings.refresh_from_db()
        self.assertTrue(settings.sync_enabled)

    @mock.patch("waldur_mastermind.waldur_arrow.views.config")
    def test_pause_global_sync(self, mock_config):
        response = self.client.post(
            "/api/admin/arrow/billing-syncs/pause_sync/",
            {"pause_global": True},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("global", response.data["paused"])

    def test_pause_specific_settings(self):
        settings = self.fixture.arrow_settings

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/pause_sync/",
            {"settings_uuid": str(settings.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        settings.refresh_from_db()
        self.assertFalse(settings.sync_enabled)

    def test_pause_nonexistent_settings(self):
        response = self.client.post(
            "/api/admin/arrow/billing-syncs/pause_sync/",
            {"settings_uuid": "00000000-0000-0000-0000-000000000000"},
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ConsumptionStatusTest(test.APITestCase):
    """Tests for consumption_status action."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_get_consumption_status(self):
        self.fixture.arrow_settings

        response = self.client.get("/api/admin/arrow/billing-syncs/consumption_status/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("global_sync_enabled", response.data)
        self.assertIn("settings_sync_enabled", response.data)
        self.assertIn("settings_uuid", response.data)


class ConsumptionStatisticsTest(test.APITestCase):
    """Tests for consumption_statistics action."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_get_consumption_statistics(self):
        self.fixture.consumption_record

        response = self.client.get(
            "/api/admin/arrow/billing-syncs/consumption_statistics/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_records"], 1)
        self.assertEqual(response.data["pending_records"], 1)
        self.assertEqual(response.data["finalized_records"], 0)
        # DRF DecimalField serializes to string
        self.assertEqual(response.data["total_consumed_sell"], "100.00")


class PendingRecordsTest(test.APITestCase):
    """Tests for pending_records action."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_list_pending_records(self):
        record = self.fixture.consumption_record

        response = self.client.get("/api/admin/arrow/billing-syncs/pending_records/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(record.uuid))

    def test_excludes_finalized_records(self):
        record = self.fixture.consumption_record
        from django.utils import timezone

        record.finalized_at = timezone.now()
        record.save()

        response = self.client.get("/api/admin/arrow/billing-syncs/pending_records/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class FetchConsumptionTest(test.APITestCase):
    """Tests for fetch_consumption action."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_requires_settings(self):
        # No active settings
        models.ArrowSettings.objects.all().delete()

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/fetch_consumption/",
            {"license_reference": "XSP12345", "period": "2024-01"},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_mastermind.waldur_arrow.views.get_arrow_client")
    def test_fetch_consumption(self, mock_get_client):
        self.fixture.arrow_settings
        mock_client = mock.MagicMock()
        mock_client.get_monthly_consumption.return_value = {
            "columns": ["Date", "Total sell price"],
            "data": [["2024-01-01", "100.00"]],
        }
        mock_client.parse_consumption_to_dicts.return_value = [
            {"Date": "2024-01-01", "Total sell price": "100.00"}
        ]
        mock_get_client.return_value = mock_client

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/fetch_consumption/",
            {"license_reference": "XSP12345", "period": "2024-01"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["row_count"], 1)


class FetchBillingExportTest(test.APITestCase):
    """Tests for fetch_billing_export action."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    @mock.patch("waldur_mastermind.waldur_arrow.views.get_arrow_client")
    def test_fetch_billing_export(self, mock_get_client):
        self.fixture.arrow_settings
        mock_client = mock.MagicMock()
        mock_client.export_billing_all_pages.return_value = [
            {"line_reference": "LINE-001", "sell_price": "100.00"}
        ]
        mock_get_client.return_value = mock_client

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/fetch_billing_export/",
            {"period_from": "2024-01", "period_to": "2024-01"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["row_count"], 1)


class FetchLicenseInfoTest(test.APITestCase):
    """Tests for fetch_license_info action."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    @mock.patch("waldur_mastermind.waldur_arrow.views.get_arrow_client")
    def test_fetch_license_info(self, mock_get_client):
        self.fixture.arrow_settings
        mock_client = mock.MagicMock()
        mock_client.get_license.return_value = {
            "data": {"license": {"reference": "XSP12345", "status": "active"}}
        }
        mock_get_client.return_value = mock_client

        response = self.client.post(
            "/api/admin/arrow/billing-syncs/fetch_license_info/",
            {"license_reference": "XSP12345"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ArrowConsumptionRecordViewSetTest(test.APITestCase):
    """Tests for ArrowConsumptionRecordViewSet."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_list_requires_staff(self):
        non_staff = structure_factories.UserFactory(is_staff=False)
        self.client.force_authenticate(non_staff)

        response = self.client.get("/api/admin/arrow/consumption-records/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_consumption_records(self):
        self.fixture.consumption_record

        response = self.client.get("/api/admin/arrow/consumption-records/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_retrieve_consumption_record(self):
        record = self.fixture.consumption_record

        response = self.client.get(
            f"/api/admin/arrow/consumption-records/{record.uuid}/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], str(record.uuid))
        self.assertEqual(response.data["license_reference"], "XSP12345")

    def test_filter_by_resource(self):
        record = self.fixture.consumption_record

        response = self.client.get(
            f"/api/admin/arrow/consumption-records/?resource_uuid={record.resource.uuid}"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_filter_by_billing_period(self):
        self.fixture.consumption_record

        response = self.client.get(
            "/api/admin/arrow/consumption-records/?billing_period_from=2024-01-01"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_filter_is_finalized(self):
        record = self.fixture.consumption_record
        from django.utils import timezone

        record.finalized_at = timezone.now()
        record.save()

        response = self.client.get(
            "/api/admin/arrow/consumption-records/?is_finalized=true"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        response = self.client.get(
            "/api/admin/arrow/consumption-records/?is_finalized=false"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_create_is_disabled(self):
        self.fixture.resource

        response = self.client.post(
            "/api/admin/arrow/consumption-records/",
            {"resource": str(self.fixture.resource.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class ArrowBillingSyncItemViewSetTest(test.APITestCase):
    """Tests for ArrowBillingSyncItemViewSet."""

    def setUp(self):
        self.fixture = StaffMaintenanceFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_list_requires_staff(self):
        non_staff = structure_factories.UserFactory(is_staff=False)
        self.client.force_authenticate(non_staff)

        response = self.client.get("/api/admin/arrow/billing-sync-items/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_billing_sync_items(self):
        self.fixture.billing_sync_item

        response = self.client.get("/api/admin/arrow/billing-sync-items/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_retrieve_billing_sync_item(self):
        item = self.fixture.billing_sync_item

        response = self.client.get(f"/api/admin/arrow/billing-sync-items/{item.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], str(item.uuid))
        self.assertEqual(response.data["vendor_name"], "Microsoft")

    def test_filter_by_billing_sync(self):
        item = self.fixture.billing_sync_item

        response = self.client.get(
            f"/api/admin/arrow/billing-sync-items/?billing_sync_uuid={item.billing_sync.uuid}"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_filter_by_vendor_name(self):
        self.fixture.billing_sync_item

        response = self.client.get(
            "/api/admin/arrow/billing-sync-items/?vendor_name=Microsoft"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_filter_by_classification(self):
        self.fixture.billing_sync_item

        response = self.client.get(
            "/api/admin/arrow/billing-sync-items/?classification=IAAS"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_filter_has_compensation(self):
        self.fixture.billing_sync_item  # No compensation

        response = self.client.get(
            "/api/admin/arrow/billing-sync-items/?has_compensation=false"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        response = self.client.get(
            "/api/admin/arrow/billing-sync-items/?has_compensation=true"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_create_is_disabled(self):
        self.fixture.billing_sync

        response = self.client.post(
            "/api/admin/arrow/billing-sync-items/",
            {"billing_sync": str(self.fixture.billing_sync.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
