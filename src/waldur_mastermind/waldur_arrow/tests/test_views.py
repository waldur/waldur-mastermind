"""Tests for Arrow API views."""

from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.waldur_arrow import models, tasks
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

    def test_api_key_is_not_returned(self):
        settings = self.fixture.arrow_settings

        list_response = self.client.get("/api/admin/arrow/settings/")
        detail_response = self.client.get(f"/api/admin/arrow/settings/{settings.uuid}/")

        self.assertNotIn("api_key", list_response.data[0])
        self.assertNotIn("api_key", detail_response.data)

    @mock.patch("waldur_mastermind.waldur_arrow.backend.ArrowClient.ping")
    def test_blank_api_key_on_update_keeps_current_key(self, mock_ping):
        mock_ping.return_value = {"valid": False, "error": "offline"}
        settings = self.fixture.arrow_settings
        original_key = settings.api_key

        response = self.client.patch(
            f"/api/admin/arrow/settings/{settings.uuid}/",
            {"api_key": "", "sync_enabled": True},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn("api_key", response.data)
        settings.refresh_from_db()
        self.assertEqual(settings.api_key, original_key)
        self.assertTrue(settings.sync_enabled)

    @mock.patch("waldur_mastermind.waldur_arrow.backend.ArrowClient.ping")
    def test_api_key_on_update_replaces_key(self, mock_ping):
        mock_ping.return_value = {"valid": False, "error": "offline"}
        settings = self.fixture.arrow_settings

        response = self.client.patch(
            f"/api/admin/arrow/settings/{settings.uuid}/", {"api_key": "new-key"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        settings.refresh_from_db()
        self.assertEqual(settings.api_key, "new-key")

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

    @mock.patch("waldur_mastermind.waldur_arrow.backend.ArrowClient.list_all_customers")
    def test_available_customers_excludes_inactive_mappings(self, mock_list):
        mapping = self.fixture.customer_mapping
        mapping.is_active = False
        mapping.save()
        mock_list.return_value = [
            {"Reference": mapping.arrow_reference, "CompanyName": "Mapped Co"},
            {"Reference": "XSP-NEW", "CompanyName": "New Co"},
        ]

        response = self.client.get(
            "/api/admin/arrow/customer-mappings/available_customers/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [c["reference"] for c in response.data["arrow_customers"]], ["XSP-NEW"]
        )

    def test_query_matches_organization_name(self):
        mapping = self.fixture.customer_mapping

        found = self.client.get(
            "/api/admin/arrow/customer-mappings/",
            {"query": mapping.waldur_customer.name[:5]},
        )
        missed = self.client.get(
            "/api/admin/arrow/customer-mappings/", {"query": "zzzz"}
        )

        self.assertEqual([m["uuid"] for m in found.data], [mapping.uuid.hex])
        self.assertEqual(missed.data, [])

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

    def test_query_matches_company_name(self):
        self.fixture.billing_sync
        company = self.fixture.customer_mapping.arrow_company_name

        found = self.client.get(
            "/api/admin/arrow/billing-syncs/", {"query": company[:6]}
        )
        missed = self.client.get("/api/admin/arrow/billing-syncs/", {"query": "zzzz"})

        self.assertEqual(len(found.data), 1)
        self.assertEqual(len(missed.data), 0)

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


class ArrowLicenseLinkingTest(test.APITestCase):
    """Linked and imported licenses must be found by the consumption sync."""

    def setUp(self):
        self.fixture = ArrowFixture()
        self.client.force_authenticate(self.fixture.user)
        self.mapping = self.fixture.customer_mapping
        self.offering = marketplace_factories.OfferingFactory()
        self.project = structure_factories.ProjectFactory(
            customer=self.mapping.waldur_customer
        )
        self.resource = marketplace_models.Resource.objects.create(
            name="Azure subscription",
            offering=self.offering,
            project=self.project,
            state=marketplace_models.Resource.States.OK,
        )
        self.url = f"/api/admin/arrow/customer-mappings/{self.mapping.uuid}/"

    def _sync(self):
        with (
            mock.patch(
                "waldur_mastermind.waldur_arrow.backend.ArrowClient.get_monthly_consumption",
                return_value={"headers": [], "lines": []},
            ),
            mock.patch(
                "waldur_mastermind.waldur_arrow.backend.ArrowClient.parse_consumption_to_dicts",
                return_value=[{"Total sell price": "10.00", "Total buy price": "8.00"}],
            ),
        ):
            return tasks.sync_arrow_consumption(year=2026, month=10)

    def test_linked_resource_is_synced(self):
        response = self.client.post(
            self.url + "link_resource/",
            {"resource_uuid": self.resource.uuid.hex, "license_reference": "XSP-LIC-1"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.backend_id, "XSP-LIC-1")
        self.assertEqual(
            self.resource.attributes["arrow_license_reference"], "XSP-LIC-1"
        )
        self.assertEqual(self._sync()["synced"], 1)
        self.assertTrue(
            models.ArrowConsumptionRecord.objects.filter(
                resource=self.resource, license_reference="XSP-LIC-1"
            ).exists()
        )

    def test_license_cannot_be_linked_to_a_second_resource(self):
        other = marketplace_models.Resource.objects.create(
            name="Other",
            offering=self.offering,
            project=self.project,
            state=marketplace_models.Resource.States.OK,
            attributes={"arrow_license_reference": "XSP-LIC-1"},
        )

        response = self.client.post(
            self.url + "link_resource/",
            {"resource_uuid": self.resource.uuid.hex, "license_reference": "XSP-LIC-1"},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(other.attributes["arrow_license_reference"] == "XSP-LIC-1")
        self.resource.refresh_from_db()
        self.assertNotIn("arrow_license_reference", self.resource.attributes or {})

    def test_license_linked_the_old_way_blocks_a_second_link(self):
        marketplace_models.Resource.objects.create(
            name="Linked before",
            offering=self.offering,
            project=self.project,
            state=marketplace_models.Resource.States.OK,
            backend_id="XSP-LIC-1",
        )

        response = self.client.post(
            self.url + "link_resource/",
            {"resource_uuid": self.resource.uuid.hex, "license_reference": "XSP-LIC-1"},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_imported_license_is_synced(self):
        response = self.client.post(
            self.url + "import_license/",
            {
                "license_reference": "XSP-LIC-2",
                "offering_uuid": self.offering.uuid.hex,
                "project_uuid": self.project.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        resource = marketplace_models.Resource.objects.get(
            uuid=response.data["resource_uuid"]
        )
        self.assertEqual(resource.attributes["arrow_license_reference"], "XSP-LIC-2")
        self.assertEqual(self._sync()["synced"], 1)


class DiscoverLicensesTest(test.APITestCase):
    def setUp(self):
        self.fixture = ArrowFixture()
        self.client.force_authenticate(self.fixture.user)
        self.mapping = self.fixture.customer_mapping

    @mock.patch("waldur_mastermind.waldur_arrow.backend.ArrowClient.list_all_licenses")
    @mock.patch("waldur_mastermind.waldur_arrow.backend.ArrowClient.list_all_customers")
    def test_vendor_comes_from_program(self, mock_customers, mock_licenses):
        """Shaped like licenses/v2: the vendor is `program`, service_ref a code."""
        mock_customers.return_value = [
            {"Reference": self.mapping.arrow_reference, "CompanyName": "Test"}
        ]
        mock_licenses.return_value = [
            {
                "customer_ref": self.mapping.arrow_reference,
                "license_id": "XSP-1",
                "program": "Amazon",
                "service_ref": "AWS_AMAZON_SUBSCRIPTION",
                "name": "AWS account",
                "sku": "SKU",
                "friendlyName": "Prod",
            }
        ]

        response = self.client.get(
            f"/api/admin/arrow/customer-mappings/{self.mapping.uuid}/discover_licenses/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["arrow_licenses"][0]["vendor_name"], "Amazon")
