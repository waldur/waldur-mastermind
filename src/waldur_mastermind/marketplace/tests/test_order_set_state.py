import traceback

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests.fixtures import ProjectRole
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace_support import PLUGIN_NAME


class BaseOrderSetStateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager

        self.offering = self.fixture.offering
        self.offering.type = "Marketplace.Slurm"
        self.offering.save()

        self.order = self.fixture.order

        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)


@ddt
class OrderSetStateExecutingTest(BaseOrderSetStateTest):
    @data(
        ("staff", OrderStates.PENDING_CONSUMER),
        ("staff", OrderStates.ERRED),
        ("offering_owner", OrderStates.PENDING_CONSUMER),
        ("offering_owner", OrderStates.ERRED),
    )
    def test_authorized_user_can_set_executing_state(self, user_and_state):
        user, state = user_and_state
        self.order.state = state
        self.order.save()

        response = self.item_set_state_executing(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.EXECUTING)

    @data("admin", "manager", "owner")
    def test_user_cannot_set_executing_state(self, user):
        response = self.item_set_state_executing(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def item_set_state_executing(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, "set_state_executing")
        return self.client.post(url)


@ddt
class OrderSetStateDoneTest(BaseOrderSetStateTest):
    @data("staff", "offering_owner")
    def test_authorized_user_can_set_done_state(self, user):
        self.order.state = OrderStates.EXECUTING
        self.order.save()

        response = self.item_set_state_done(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.DONE)

    @data("admin", "manager", "owner")
    def test_user_cannot_set_done_state(self, user):
        response = self.item_set_state_done(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def item_set_state_done(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, "set_state_done")
        return self.client.post(url)


@ddt
class OrderSetStateErredTest(BaseOrderSetStateTest):
    @data("staff", "offering_owner", "service_manager")
    def test_authorized_user_can_set_erred_state(self, user):
        self.order.state = OrderStates.EXECUTING
        self.order.save()

        error_message = "Resource creation has been failed"
        error_traceback = traceback.format_exc()
        user = "staff"
        response = self.item_set_state_erred(user, error_message, error_traceback)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.ERRED)
        self.assertEqual(self.order.error_message, error_message)
        self.assertEqual(self.order.error_traceback, error_traceback.strip())

    @data("admin", "manager", "owner")
    def test_user_cannot_set_erred_state(self, user):
        response = self.item_set_state_erred(
            user, "Resource creation has been failed", traceback.format_exc()
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def item_set_state_erred(self, user, error_message, error_traceback):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, "set_state_erred")
        return self.client.post(
            url, {"error_message": error_message, "error_traceback": error_traceback}
        )


@ddt
class OrderCancelTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.offering = factories.OfferingFactory(type=PLUGIN_NAME)
        self.order = factories.OrderFactory(
            offering=self.offering, project=self.project, created_by=self.manager
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.CANCEL_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CANCEL_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CANCEL_ORDER)

    @data("staff", "owner", "admin", "manager")
    def test_authorized_user_can_cancel_order(self, user):
        response = self.cancel_order(user)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.CANCELED)

    @data(
        OrderStates.DONE,
        OrderStates.ERRED,
        OrderStates.CANCELED,
    )
    def test_order_cannot_be_cancelled_if_it_is_in_terminal_state(self, state):
        self.order.state = state
        self.order.save()
        response = self.cancel_order("staff")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_cannot_cancel_order_if_it_is_not_supported_by_offering(self):
        self.offering.type = "OpenStack.Tenant"
        self.offering.save()
        response = self.cancel_order("staff")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def cancel_order(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, "cancel")
        return self.client.post(url)
