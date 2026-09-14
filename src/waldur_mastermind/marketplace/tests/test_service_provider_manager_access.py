"""A service provider manager holds their role on the ServiceProvider, not on its
customer. Provider-side reads must accept that scope, not only a customer role."""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.structure.serializers import CustomerSerializer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.models import ServiceProvider
from waldur_mastermind.marketplace.tests import factories, fixtures


class ServiceProviderManagerAccessTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.offering = self.fixture.offering

        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.GET_SERVICE_PROVIDER_REVENUE
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMER_PROJECTS
        )

        self.manager = structure_factories.UserFactory()
        self.service_provider.add_user(self.manager, ServiceProviderRole.MANAGER)

        # Manages a different provider, so holds the same role but not here.
        self.other_manager = structure_factories.UserFactory()
        factories.ServiceProviderFactory().add_user(
            self.other_manager, ServiceProviderRole.MANAGER
        )

    def get_provider_offerings(self, user):
        self.client.force_authenticate(user)
        return self.client.get(
            factories.OfferingFactory.get_list_url(),
            {"customer_uuid": self.service_provider.customer.uuid.hex},
        )

    def test_manager_lists_own_provider_offerings(self):
        response = self.get_provider_offerings(self.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["uuid"] for item in response.data], [self.offering.uuid.hex]
        )

    def test_manager_of_another_provider_does_not_list_offerings(self):
        response = self.get_provider_offerings(self.other_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_manager_gets_own_provider_stat_and_revenue(self):
        self.client.force_authenticate(self.manager)
        for action in ("stat", "revenue"):
            response = self.client.get(
                factories.ServiceProviderFactory.get_url(self.service_provider, action)
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, action)

    def test_manager_of_another_provider_is_denied_stat_and_revenue(self):
        self.client.force_authenticate(self.other_manager)
        for action in ("stat", "revenue"):
            response = self.client.get(
                factories.ServiceProviderFactory.get_url(self.service_provider, action)
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, action)

    def get_provider_tab_urls(self):
        return {
            "customers": f"/api/marketplace-service-providers/{self.service_provider.uuid.hex}/customers/",
            # Each row of the Customers tab expands into this one.
            "customer_projects": (
                f"/api/marketplace-service-providers/{self.service_provider.uuid.hex}"
                f"/customer_projects/?project_customer_uuid={self.fixture.customer.uuid.hex}"
            ),
            "compliance": factories.ServiceProviderFactory.get_compliance_url(
                self.service_provider, "compliance-overview"
            ),
        }

    def test_manager_gets_own_provider_tabs(self):
        self.client.force_authenticate(self.manager)
        for tab, url in self.get_provider_tab_urls().items():
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK, tab)

    def test_manager_of_another_provider_is_denied_tabs(self):
        self.client.force_authenticate(self.other_manager)
        for tab, url in self.get_provider_tab_urls().items():
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, tab)


class ServiceProviderManagerOrganizationVisibilityTest(test.APITestCase):
    """The manager must be able to find the provider's organization in the
    portal, but see no more of it than the provider listing already shows."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.customer = self.service_provider.customer
        self.customer.email = "billing@provider.example.com"
        self.customer.vat_code = "EE123456789"
        self.customer.save()

        self.manager = structure_factories.UserFactory()
        self.service_provider.add_user(self.manager, ServiceProviderRole.MANAGER)

        self.other_manager = structure_factories.UserFactory()
        factories.ServiceProviderFactory().add_user(
            self.other_manager, ServiceProviderRole.MANAGER
        )

        self.list_url = structure_factories.CustomerFactory.get_list_url()
        self.detail_url = structure_factories.CustomerFactory.get_url(self.customer)

    def assert_restricted(self, data):
        self.assertEqual(data["uuid"], self.customer.uuid.hex)
        self.assertEqual(
            str(data["service_provider_uuid"]), self.service_provider.uuid.hex
        )
        self.assertTrue(data["is_service_provider"])
        self.assertTrue(data["is_service_provider_manager_only"])
        self.assertLessEqual(
            set(data), set(CustomerSerializer.SERVICE_PROVIDER_MANAGER_FIELDS)
        )
        for hidden in ("email", "vat_code", "projects_count", "users_count"):
            self.assertNotIn(hidden, data)

    def test_manager_lists_provider_organization_with_restricted_fields(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assert_restricted(response.data[0])

    def test_manager_finds_provider_organization_among_own_organizations(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.list_url, {"user_uuid": self.manager.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["uuid"] for item in response.data], [self.customer.uuid.hex]
        )

    def test_manager_retrieves_provider_organization_with_restricted_fields(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_restricted(response.data)

    def test_manager_of_another_provider_does_not_see_organization(self):
        self.client.force_authenticate(self.other_manager)
        response = self.client.get(self.list_url)
        self.assertNotIn(
            self.customer.uuid.hex, [item["uuid"] for item in response.data]
        )
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_visibility_grants_no_other_customer_action(self):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(self.detail_url, {"name": "Renamed"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        for action in ("stats", "providers", "list_users", "history"):
            response = self.client.get(f"{self.detail_url}{action}/")
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                action,
            )
        self.customer.refresh_from_db()
        self.assertNotEqual(self.customer.name, "Renamed")

    def test_manager_who_is_also_owner_gets_full_organization(self):
        self.customer.add_user(self.manager, CustomerRole.OWNER)
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], self.customer.email)
        self.assertFalse(response.data["is_service_provider_manager_only"])

    def test_staff_gets_full_organization(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["vat_code"], self.customer.vat_code)
        self.assertFalse(response.data["is_service_provider_manager_only"])

    def test_visibility_does_not_extend_to_financial_reports(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get("/api/financial-reports/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(
            self.customer.uuid.hex, [item["uuid"] for item in response.data]
        )
        response = self.client.get(f"/api/financial-reports/{self.customer.uuid.hex}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_custom_service_provider_role_grants_visibility(self):
        user = structure_factories.UserFactory()
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(ServiceProvider)
        )
        self.service_provider.add_user(user, role)
        self.client.force_authenticate(user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_restricted(response.data)

    def test_offering_role_does_not_lift_restriction(self):
        # An offering role on one of the provider's offerings confers no
        # organization visibility, so the row stays restricted and flagged.
        self.assertEqual(self.fixture.offering.customer, self.customer)
        self.fixture.offering.add_user(self.manager, OfferingRole.MANAGER)
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_restricted(response.data)
