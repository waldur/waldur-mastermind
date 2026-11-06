from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import BASIC_OFFERING, OrderStates
from waldur_mastermind.marketplace.tests import factories, fixtures


class OfferingManagerOrderReviewTest(test.APITestCase):
    """An offering manager reviews and completes the orders of their offering,
    without any role on the provider organization."""

    def setUp(self):
        for permission in (
            PermissionEnum.LIST_ORDERS,
            PermissionEnum.APPROVE_ORDER,
            PermissionEnum.REJECT_ORDER,
        ):
            OfferingRole.MANAGER.add_permission(permission)
        self.fixture = fixtures.MarketplaceFixture()
        provider_customer = self.fixture.service_provider.customer

        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING, customer=provider_customer
        )
        self.manager = structure_factories.UserFactory()
        self.offering.add_user(self.manager, OfferingRole.MANAGER)
        self.order = self.make_order(self.offering)

        # Same provider, an offering the manager does not manage.
        self.other_order = self.make_order(
            factories.OfferingFactory(type=BASIC_OFFERING, customer=provider_customer)
        )

    def make_order(self, offering, state=OrderStates.PENDING_PROVIDER):
        return factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            offering=offering,
            resource=factories.ResourceFactory(
                offering=offering, project=self.fixture.project
            ),
            state=state,
        )

    def pending_provider_orders(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.OrderFactory.get_list_url(), {"can_approve_as_provider": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {order["uuid"] for order in response.json()}

    def test_manager_sees_pending_orders_of_own_offering_only(self):
        self.assertEqual(
            self.pending_provider_orders(self.manager), {self.order.uuid.hex}
        )

    def test_offering_role_without_approve_sees_no_pending_orders(self):
        OfferingRole.MANAGER.delete_permission(PermissionEnum.APPROVE_ORDER)
        self.assertEqual(self.pending_provider_orders(self.manager), set())

    def test_manager_approves_order_of_own_offering(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.OrderFactory.get_url(self.order, "approve_by_provider")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.EXECUTING)

    def test_manager_can_not_approve_order_of_another_offering(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.OrderFactory.get_url(self.other_order, "approve_by_provider")
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.other_order.refresh_from_db()
        self.assertEqual(self.other_order.state, OrderStates.PENDING_PROVIDER)

    def test_manager_marks_executing_order_as_done(self):
        order = self.make_order(self.offering, state=OrderStates.EXECUTING)
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.OrderFactory.get_url(order, "set_state_done")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)
