"""Tests for Arrow API views."""

from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.waldur_arrow import models
from waldur_mastermind.waldur_arrow.tests.fixtures import ArrowFixture


class ArrowSettingsViewSetTest(test.APITestCase):
    """Tests for ArrowSettingsViewSet."""

    def setUp(self):
        self.fixture = ArrowFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_list_requires_staff(self):
        non_staff = structure_factories.UserFactory(is_staff=False)
        self.client.force_authenticate(non_staff)

        response = self.client.get("/api/admin/arrow/settings/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_settings(self):
        self.fixture.arrow_settings

        response = self.client.get("/api/admin/arrow/settings/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_retrieve_settings(self):
        settings = self.fixture.arrow_settings

        response = self.client.get(f"/api/admin/arrow/settings/{settings.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], str(settings.uuid))

    @mock.patch("waldur_mastermind.waldur_arrow.backend.ArrowClient.ping")
    def test_validate_credentials(self, mock_ping):
        mock_ping.return_value = {
            "valid": True,
            "data": {
                "reference": "XSP12345",
                "companyName": "Test Partner",
            },
        }

        response = self.client.post(
            "/api/admin/arrow/settings/validate_credentials/",
            {
                "api_url": "https://api.arrow.test/",
                "api_key": "test-key",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["valid"])

    @mock.patch("waldur_mastermind.waldur_arrow.backend.ArrowClient.ping")
    def test_validate_credentials_invalid(self, mock_ping):
        mock_ping.return_value = {
            "valid": False,
            "error": "Invalid API key",
        }

        response = self.client.post(
            "/api/admin/arrow/settings/validate_credentials/",
            {
                "api_url": "https://api.arrow.test/",
                "api_key": "invalid-key",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["valid"])

    @mock.patch("waldur_mastermind.waldur_arrow.backend.ArrowClient.list_all_customers")
    @mock.patch("waldur_mastermind.waldur_arrow.backend.ArrowClient.ping")
    def test_discover_customers(self, mock_ping, mock_list_customers):
        mock_ping.return_value = {"valid": True, "data": {}}
        mock_list_customers.return_value = [
            {"reference": "XSP001", "companyName": "Customer One"},
            {"reference": "XSP002", "companyName": "Customer Two"},
        ]

        response = self.client.post(
            "/api/admin/arrow/settings/discover_customers/",
            {
                "api_url": "https://api.arrow.test/",
                "api_key": "test-key",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["arrow_customers"]), 2)
        self.assertIn("waldur_customers", response.data)
        self.assertIn("suggestions", response.data)

    @mock.patch("waldur_mastermind.waldur_arrow.backend.ArrowClient.ping")
    def test_save_settings(self, mock_ping):
        mock_ping.return_value = {
            "valid": True,
            "data": {
                "reference": "XSP12345",
                "companyName": "Test Partner",
            },
        }

        response = self.client.post(
            "/api/admin/arrow/settings/save_settings/",
            {
                "api_url": "https://api.arrow.test/",
                "api_key": "test-key",
                "export_type_reference": "TYPE1",
                "classification_filter": "IAAS",
                "sync_enabled": True,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("settings_uuid", response.data)

        # Verify settings were created
        settings = models.ArrowSettings.objects.get(uuid=response.data["settings_uuid"])
        self.assertEqual(settings.api_url, "https://api.arrow.test/")
        self.assertTrue(settings.is_active)


class ArrowCustomerMappingViewSetTest(test.APITestCase):
    """Tests for ArrowCustomerMappingViewSet."""

    def setUp(self):
        self.fixture = ArrowFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_list_requires_staff(self):
        non_staff = structure_factories.UserFactory(is_staff=False)
        self.client.force_authenticate(non_staff)

        response = self.client.get("/api/admin/arrow/customer-mappings/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_mappings(self):
        self.fixture.customer_mapping

        response = self.client.get("/api/admin/arrow/customer-mappings/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_create_mapping(self):
        settings = self.fixture.arrow_settings
        customer = structure_factories.CustomerFactory()

        response = self.client.post(
            "/api/admin/arrow/customer-mappings/",
            {
                "settings": str(settings.uuid),
                "arrow_reference": "XSP99999",
                "arrow_company_name": "New Company",
                "waldur_customer": str(customer.uuid),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            models.ArrowCustomerMapping.objects.filter(
                arrow_reference="XSP99999"
            ).count(),
            1,
        )

    def test_filter_by_settings(self):
        mapping = self.fixture.customer_mapping

        response = self.client.get(
            f"/api/admin/arrow/customer-mappings/?settings_uuid={mapping.settings.uuid}"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


class ArrowBillingSyncViewSetTest(test.APITestCase):
    """Tests for ArrowBillingSyncViewSet."""

    def setUp(self):
        self.fixture = ArrowFixture()
        self.client.force_authenticate(self.fixture.user)

    def test_list_requires_staff(self):
        non_staff = structure_factories.UserFactory(is_staff=False)
        self.client.force_authenticate(non_staff)

        response = self.client.get("/api/admin/arrow/billing-syncs/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_syncs(self):
        self.fixture.billing_sync

        response = self.client.get("/api/admin/arrow/billing-syncs/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_retrieve_sync(self):
        sync = self.fixture.billing_sync

        response = self.client.get(f"/api/admin/arrow/billing-syncs/{sync.uuid}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], str(sync.uuid))
        self.assertEqual(response.data["report_period"], "2024-01")

    @mock.patch("waldur_mastermind.waldur_arrow.tasks.sync_arrow_billing.delay")
    def test_trigger_sync(self, mock_delay):
        response = self.client.post(
            "/api/admin/arrow/billing-syncs/trigger_sync/",
            {
                "year": 2024,
                "month": 1,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_delay.assert_called_once()

    @mock.patch("waldur_mastermind.waldur_arrow.tasks.reconcile_arrow_billing.delay")
    def test_reconcile(self, mock_delay):
        response = self.client.post(
            "/api/admin/arrow/billing-syncs/reconcile/",
            {
                "year": 2024,
                "month": 1,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_delay.assert_called_once()

    def test_filter_by_report_period(self):
        self.fixture.billing_sync

        response = self.client.get(
            "/api/admin/arrow/billing-syncs/?report_period=2024-01"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_create_is_disabled(self):
        response = self.client.post(
            "/api/admin/arrow/billing-syncs/",
            {"customer_mapping": str(self.fixture.customer_mapping.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
