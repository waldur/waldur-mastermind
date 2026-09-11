"""A service provider manager holds their role on the ServiceProvider, not on its
customer. Provider-side reads must accept that scope, not only a customer role."""

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ServiceProviderRole
from waldur_core.structure.tests import factories as structure_factories
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
