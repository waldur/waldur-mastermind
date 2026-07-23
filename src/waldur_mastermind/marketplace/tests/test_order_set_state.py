import traceback

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests.fixtures import ProjectRole
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures


class BaseOrderSetStateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager

        self.offering = self.fixture.offering
        self.offering.type = "Marketplace.Slurm"
        self.offering.save()

        self.order = self.fixture.order

        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        OfferingRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        OfferingRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)


@ddt
class OrderSetStateExecutingTest(BaseOrderSetStateTest):
    @data(
        ("staff", OrderStates.PENDING_CONSUMER),
        ("staff", OrderStates.ERRED),
        ("offering_owner", OrderStates.PENDING_CONSUMER),
        ("offering_owner", OrderStates.ERRED),
        ("offering_manager", OrderStates.PENDING_CONSUMER),
        ("offering_manager", OrderStates.ERRED),
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
    @data("staff", "offering_owner", "offering_manager")
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


class OrderSetStateDoneOptionsUpdateTest(test.APITestCase):
    """
    Test that options are correctly updated when set_state_done is called
    for an UPDATE order with new_options attribute.

    This tests the site-agent workflow where the agent directly calls
    set_state_done API instead of going through the process_order flow.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = "Marketplace.Slurm"
        self.offering.save()

        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.UPDATING
        self.resource.options = {"key1": "old_value", "key2": "unchanged"}
        self.resource.save()

        # Create an UPDATE order with new_options
        self.order = factories.OrderFactory(
            resource=self.resource,
            offering=self.offering,
            project=self.fixture.project,
            type=OrderTypes.UPDATE,
            state=OrderStates.EXECUTING,
            attributes={
                "new_options": {"key1": "new_value", "key3": "added_value"},
                "old_options": {"key1": "old_value", "key2": "unchanged"},
            },
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)

    def test_options_are_updated_when_set_state_done_is_called(self):
        """
        Test that resource options are updated from order.attributes['new_options']
        when set_state_done is called via the API.
        """
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OrderFactory.get_url(self.order, "set_state_done")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify order state
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.DONE)

        # Verify resource state is set to OK
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)

        # Verify options are updated correctly (merged with existing)
        expected_options = {
            "key1": "new_value",  # Updated
            "key2": "unchanged",  # Preserved
            "key3": "added_value",  # Added
        }
        self.assertEqual(self.resource.options, expected_options)

    def test_resource_state_transitions_to_ok(self):
        """
        Test that resource state transitions from UPDATING to OK
        when set_state_done is called.
        """
        self.assertEqual(self.resource.state, ResourceStates.UPDATING)

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OrderFactory.get_url(self.order, "set_state_done")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)

    def test_options_update_without_prior_options(self):
        """
        Test that options are set correctly when resource had no prior options.
        """
        self.resource.options = None
        self.resource.save()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OrderFactory.get_url(self.order, "set_state_done")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.resource.refresh_from_db()
        expected_options = {"key1": "new_value", "key3": "added_value"}
        self.assertEqual(self.resource.options, expected_options)

    def test_set_state_done_without_new_options_does_not_change_options(self):
        """
        Test that resource options remain unchanged when order doesn't have new_options.
        """
        # Create order without new_options
        order_without_options = factories.OrderFactory(
            resource=self.resource,
            offering=self.offering,
            project=self.fixture.project,
            type=OrderTypes.UPDATE,
            state=OrderStates.EXECUTING,
            attributes={},  # No new_options
        )

        original_options = self.resource.options.copy()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OrderFactory.get_url(order_without_options, "set_state_done")
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, original_options)


@ddt
class OrderSetStateErredTest(BaseOrderSetStateTest):
    @data("staff", "offering_owner", "offering_manager")
    def test_authorized_user_can_set_erred_state(self, user):
        self.order.state = OrderStates.EXECUTING
        self.order.save()

        error_message = "Resource creation has been failed"
        error_traceback = traceback.format_exc()
        response = self.item_set_state_erred(user, error_message, error_traceback)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.ERRED)
        self.assertEqual(self.order.error_message, error_message)
        self.assertEqual(self.order.error_traceback, error_traceback.strip())
        self.assertIsNotNone(self.order.error_updated_at)

    def test_set_state_erred_from_pending_provider(self):
        self.order.state = OrderStates.PENDING_PROVIDER
        self.order.save()

        error_message = "Backend connection failed"
        error_traceback = traceback.format_exc()
        response = self.item_set_state_erred("staff", error_message, error_traceback)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.ERRED)
        self.assertEqual(self.order.error_message, error_message)

    def test_set_state_erred_transitions_resource_to_erred_for_create(self):
        self.order.type = OrderTypes.CREATE
        self.order.state = OrderStates.EXECUTING
        self.order.save()

        resource = self.order.resource
        resource.state = ResourceStates.CREATING
        resource.backend_id = "backend-id-1"
        resource.save()

        self.item_set_state_erred("staff", "fail", "trace")

        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.ERRED)

    def test_set_state_erred_transitions_resource_to_terminated_for_create_without_backend_id(
        self,
    ):
        """
        Resources without a backend_id were never actually provisioned, so a failed
        Create order should terminate them directly rather than leaving them stuck
        in Erred pending manual recovery.
        """
        self.order.type = OrderTypes.CREATE
        self.order.state = OrderStates.EXECUTING
        self.order.save()

        resource = self.order.resource
        resource.state = ResourceStates.CREATING
        resource.backend_id = ""
        resource.save()

        self.item_set_state_erred("staff", "fail", "trace")

        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.TERMINATED)

    def test_set_state_erred_transitions_resource_to_ok_for_terminate(self):
        self.order.type = OrderTypes.TERMINATE
        self.order.state = OrderStates.EXECUTING
        self.order.save()

        resource = self.order.resource
        resource.state = ResourceStates.TERMINATING
        resource.save()

        self.item_set_state_erred("staff", "fail", "trace")

        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.OK)

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
class OrderCancelTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.offering = factories.OfferingFactory(type=SUPPORT_OFFERING)
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


@ddt
class OrderRetryTest(BaseOrderSetStateTest):
    def _set_order_erred(self, order_type, resource_state):
        self.order.type = order_type
        self.order.state = OrderStates.ERRED
        self.order.error_message = "Something went wrong"
        self.order.error_traceback = "Traceback ..."
        self.order.save()

        resource = self.order.resource
        resource.state = resource_state
        resource.error_message = "Backend error"
        resource.error_traceback = "Traceback ..."
        resource.save()

    def test_retry_create_order(self):
        self._set_order_erred(OrderTypes.CREATE, ResourceStates.ERRED)

        response = self._retry("staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.EXECUTING)
        self.assertEqual(self.order.error_message, "")
        self.assertEqual(self.order.error_traceback, "")
        self.assertIsNone(self.order.completed_at)

        resource = self.order.resource
        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.CREATING)
        self.assertEqual(resource.error_message, "")
        self.assertEqual(resource.error_traceback, "")

    def test_retry_update_order(self):
        self._set_order_erred(OrderTypes.UPDATE, ResourceStates.ERRED)

        response = self._retry("staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.EXECUTING)

        resource = self.order.resource
        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.UPDATING)

    def test_retry_terminate_order(self):
        self._set_order_erred(OrderTypes.TERMINATE, ResourceStates.OK)

        response = self._retry("staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.EXECUTING)

        resource = self.order.resource
        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.TERMINATING)

    @data("staff", "offering_owner", "offering_manager")
    def test_authorized_user_can_retry(self, user):
        self._set_order_erred(OrderTypes.CREATE, ResourceStates.ERRED)

        response = self._retry(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("admin", "manager", "owner")
    def test_unauthorized_user_cannot_retry(self, user):
        self._set_order_erred(OrderTypes.CREATE, ResourceStates.ERRED)

        response = self._retry(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_retry_non_erred_order(self):
        self.order.state = OrderStates.EXECUTING
        self.order.save()

        response = self._retry("staff")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_cannot_retry_unsupported_offering_type(self):
        self._set_order_erred(OrderTypes.CREATE, ResourceStates.ERRED)
        self.offering.type = SUPPORT_OFFERING
        self.offering.save()

        response = self._retry("staff")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def _retry(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, "retry")
        return self.client.post(url)
