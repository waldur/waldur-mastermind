from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import BASIC_OFFERING, OrderStates
from waldur_mastermind.marketplace.tests import factories, fixtures


class OfferingManagerProviderViewsTest(test.APITestCase):
    """The provider workspace's Orders and Resources lists show an offering
    manager the orders and resources of the offerings they manage, and none of
    the provider's other offerings."""

    def setUp(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_ORDERS)
        self.fixture = fixtures.MarketplaceFixture()
        self.provider_customer = self.fixture.service_provider.customer

        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING, customer=self.provider_customer
        )
        self.other_offering = factories.OfferingFactory(
            type=BASIC_OFFERING, customer=self.provider_customer
        )
        self.manager = structure_factories.UserFactory()
        self.offering.add_user(self.manager, OfferingRole.MANAGER)

        self.order = self.make_order(self.offering)
        self.other_order = self.make_order(self.other_offering)

        self.owner = structure_factories.UserFactory()
        self.provider_customer.add_user(self.owner, CustomerRole.OWNER)
        self.outsider = structure_factories.UserFactory()

    def make_order(self, offering):
        return factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            offering=offering,
            resource=factories.ResourceFactory(
                offering=offering, project=self.fixture.project
            ),
            state=OrderStates.PENDING_PROVIDER,
        )

    def list_uuids(self, user, url, **params):
        self.client.force_authenticate(user)
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.json()}

    def provider_resources(self, user):
        return self.list_uuids(
            user,
            factories.ResourceFactory.get_provider_resource_list_url(),
            provider_uuid=self.provider_customer.uuid.hex,
        )

    def provider_orders(self, user):
        return self.list_uuids(
            user,
            factories.OrderFactory.get_list_url(),
            provider_uuid=self.provider_customer.uuid.hex,
        )

    def test_manager_lists_resources_of_own_offering_only(self):
        self.assertEqual(
            self.provider_resources(self.manager), {self.order.resource.uuid.hex}
        )

    def test_manager_opens_resource_of_own_offering_only(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(
            factories.ResourceFactory.get_provider_resource_url(self.order.resource)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.get(
            factories.ResourceFactory.get_provider_resource_url(
                self.other_order.resource
            )
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_manager_lists_orders_of_own_offering_only(self):
        self.assertEqual(self.provider_orders(self.manager), {self.order.uuid.hex})

    def test_owner_still_lists_every_offering(self):
        self.assertLessEqual(
            {self.order.resource.uuid.hex, self.other_order.resource.uuid.hex},
            self.provider_resources(self.owner),
        )
        self.assertLessEqual(
            {self.order.uuid.hex, self.other_order.uuid.hex},
            self.provider_orders(self.owner),
        )

    def test_outsider_lists_nothing(self):
        self.assertEqual(self.provider_resources(self.outsider), set())
        self.assertEqual(self.provider_orders(self.outsider), set())

    def count_queries(self, url, user=None, **params):
        self.client.force_authenticate(user or self.manager)
        with CaptureQueriesContext(connection) as context:
            response = self.client.get(url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(context.captured_queries)

    def test_query_count_does_not_grow_with_other_offerings_rows(self):
        """Rows of offerings the manager does not manage are filtered out in
        SQL, so they cost the manager's list nothing."""
        resources_url = factories.ResourceFactory.get_provider_resource_list_url()
        orders_url = factories.OrderFactory.get_list_url()
        # The first request warms per-process caches (content types, roles).
        self.count_queries(resources_url)
        self.count_queries(orders_url)
        before = (
            self.count_queries(resources_url),
            self.count_queries(orders_url),
        )
        for _ in range(3):
            self.make_order(self.other_offering)
        after = (
            self.count_queries(resources_url),
            self.count_queries(orders_url),
        )
        self.assertEqual(before, after)
