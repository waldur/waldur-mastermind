import datetime
from unittest import mock

from django.utils import timezone
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.logging import enums as logging_enums
from waldur_core.logging.tests import factories as logging_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent import tasks
from waldur_mastermind.marketplace_site_agent.tests import (
    fixtures as site_agent_fixtures,
)


class SendMessagesAboutPendingOrdersTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.save()

        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            attributes={"name": "item_name", "description": "Description"},
            plan=self.fixture.plan,
            resource=self.fixture.resource,
            state=OrderStates.PENDING_PROVIDER,
        )
        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[
                {"object_type": logging_enums.ObservableObjectType.ORDER.value}
            ],
        )

        # Create subscription queue (required for messages to be sent)
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=self.offering.uuid,
            object_type=logging_enums.ObservableObjectType.ORDER.value,
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_send_messages_about_pending_orders(
        self,
        mocked_publish_messages,
    ):
        # Arrange
        one_hour_ago = timezone.now() - datetime.timedelta(hours=1)
        self.order.created = one_hour_ago - datetime.timedelta(minutes=1)
        self.order.save()

        # Act
        tasks.send_messages_about_pending_orders()

        # Assert
        mocked_publish_messages.assert_called_once()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_send_messages_about_done_orders(
        self,
        mocked_publish_messages,
    ):
        """
        This test checks that when an order is in DONE state, the STOMP message is sent.
        """
        # Arrange
        self.order.state = OrderStates.EXECUTING
        self.order.complete()
        # Act
        self.order.save(update_fields=["state"])
        # Assert: every state transition emits (EXECUTING, then DONE); the
        # final message must carry the done state.
        self.assertEqual(mocked_publish_messages.call_count, 2)
        last_messages = mocked_publish_messages.call_args_list[-1].args[0]
        self.assertIn('"order_state": "done"', last_messages[0]["payload"])


class AllocationCreationFailureTest(test.APITestCase):
    def setUp(self):
        self.fixture = site_agent_fixtures.MarketplaceSiteAgentFixture()
        self.offering = self.fixture.offering

        # Add APPROVE_ORDER permission to staff user
        from waldur_core.permissions.enums import PermissionEnum
        from waldur_core.permissions.fixtures import CustomerRole

        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)

        # Create order in PENDING_PROVIDER state first
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=OrderStates.PENDING_PROVIDER,
            offering=self.offering,
            type=OrderTypes.CREATE,
            attributes={"name": "failed-allocation"},
        )

    def test_resource_with_failed_order_and_no_backend_id_is_terminated(self):
        """
        This test checks that when a resource with no backend_id (i.e. never
        provisioned) fails to be created, the order is set to ERRED and the
        resource is terminated directly rather than left stuck in ERRED.
        """

        self.client.force_authenticate(self.fixture.staff)
        url = marketplace_factories.OrderFactory.get_url(
            self.order, "approve_by_provider"
        )
        response = self.client.post(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Response status code should be 200, but got {response.status_code}",
        )

        # Refresh order to get latest state after API call
        self.order.refresh_from_db()
        # The order should be EXECUTING after being approved by the provider
        self.assertEqual(
            self.order.state,
            OrderStates.EXECUTING,
            f"Order {self.order.id} should be EXECUTING, but got {self.order.state}",
        )

        # Step 3: Set the backend_id to empty to simulate failed resource creation
        self.order.resource.backend_id = ""
        self.order.resource.save()

        # Step 4: Simulate agent processing failure and setting order to ERRED
        self.client.force_authenticate(self.fixture.staff)
        url = marketplace_factories.OrderFactory.get_url(self.order, "set_state_erred")
        response = self.client.post(
            url,
            {
                "error_message": "Resource creation failed: empty backend_id",
                "error_traceback": "Traceback (most recent call last):\n  ...",
            },
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Response status code should be 200, but got {response.status_code}",
        )

        # Step 5: Verify final states
        self.order.resource.refresh_from_db()
        self.order.refresh_from_db()

        # Resource has no backend_id, i.e. it was never provisioned, so it should
        # be terminated directly instead of getting stuck in ERRED - see
        # update_resource_state_on_order_rejection_error_or_cancellation in
        # marketplace/handlers.py.
        self.assertEqual(
            self.order.resource.state,
            ResourceStates.TERMINATED,
            f"Resource {self.order.resource.id} should be TERMINATED, but got {self.order.resource.state}",
        )
        # Order should be ERRED
        self.assertEqual(
            self.order.state,
            OrderStates.ERRED,
            f"Order {self.order.id} should be ERRED, but got {self.order.state}",
        )


class AllocationCleanupTest(test.APITestCase):
    def setUp(self):
        self.fixture = site_agent_fixtures.MarketplaceSiteAgentFixture()
        self.resource = self.fixture.resource
        self.project = self.fixture.project

        # Create system robot for resource termination
        self.system_robot = core_utils.get_system_robot()

        # Set resource to ERRED state with backend_id
        self.resource.backend_id = "something"
        self.resource.state = ResourceStates.ERRED
        self.resource.save()

        # Set project end date to past date
        self.project.end_date = timezone.now() - timezone.timedelta(days=10)
        self.project.save()

    def test_erred_resource_triggers_cleanup(self):
        """
        This test verifies that when a project's end date is reached:
        1. The cleanup task finds erred resources with backend_id
        2. A new termination order is created for these resources
        3. The order goes into ERRED state due to missing backend_id
        """
        # Trigger the cleanup task
        from waldur_mastermind.marketplace import tasks

        tasks.terminate_resources_if_project_end_date_has_been_reached()
        # Simulate agent response
        termination_order = marketplace_models.Order.objects.filter(
            resource=self.resource,
            type=OrderTypes.TERMINATE,
        ).first()

        # First approve the order by provider to move it to EXECUTING state
        self.client.force_authenticate(self.fixture.staff)
        url = marketplace_factories.OrderFactory.get_url(
            termination_order, "approve_by_provider"
        )
        self.client.post(url)

        # Then simulate agent failure due to empty backend_id in allocation
        url = marketplace_factories.OrderFactory.get_url(
            termination_order, "set_state_erred"
        )
        self.client.post(
            url,
            {
                "error_message": "Empty backend_id for allocation",
                "error_traceback": "Traceback (most recent call last):\n  ...",
            },
        )

        # Verify that a new termination order was created and went into ERRED state
        termination_orders = marketplace_models.Order.objects.filter(
            resource=self.resource,
            type=OrderTypes.TERMINATE,
            state=OrderStates.ERRED,
        )
        self.assertEqual(
            termination_orders.count(),
            1,
            f"Expected 1 termination order, got {termination_orders.count()}",
        )

        # Verify the error message
        order = termination_orders.first()
        self.assertEqual(order.error_message, "Empty backend_id for allocation")

    def test_erred_resource_without_backend_id_with_failed_create_order_and_termination_order_is_deleted(
        self,
    ):
        """
        This test verifies that when a resource fails to be created, has no backend_id and has a failed termination order as well as a failed create order, the resource is deleted by the cleanup task.
        """
        # Set resource to ERRED state with empty backend_id
        self.resource.backend_id = ""
        self.resource.state = ResourceStates.ERRED
        self.resource.save()
        # Create a failed create order
        failed_create_order = marketplace_factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.CREATE,
            state=OrderStates.ERRED,
        )

        # Create a failed termination order
        failed_termination_order = marketplace_factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.TERMINATE,
            state=OrderStates.ERRED,
        )

        # Assert that the resource is erred
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.state,
            ResourceStates.ERRED,
            f"Resource {self.resource.id} should be ERRED, but got {self.resource.state}",
        )

        # Assert that there are two orders in ERRED state
        self.assertEqual(
            marketplace_models.Order.objects.filter(state=OrderStates.ERRED).count(),
            2,
            f"Expected 2 orders in ERRED state, got {marketplace_models.Order.objects.filter(state=OrderStates.ERRED).count()}",
        )

        # Assert that the failed create order and failed termination order are associated with the resource
        failed_create_order.refresh_from_db()
        failed_termination_order.refresh_from_db()
        self.assertEqual(failed_create_order.resource, self.resource)
        self.assertEqual(failed_termination_order.resource, self.resource)

        # Trigger the cleanup task
        from waldur_mastermind.marketplace import tasks

        tasks.terminate_resources_in_state_erred_without_backend_id_and_failed_terminate_order()

        with self.assertRaises(
            marketplace_models.Resource.DoesNotExist,
            msg=f"Resource {self.resource.id} should be deleted, but it exists",
        ):
            self.resource.refresh_from_db()

        # Assert that the orders are deleted
        self.assertEqual(
            marketplace_models.Order.objects.filter(state=OrderStates.ERRED).count(),
            0,
            f"Expected 0 orders in ERRED state, got {marketplace_models.Order.objects.filter(state=OrderStates.ERRED).count()}",
        )

    def test_erred_resource_with_backend_id_and_failed_termination_order_is_not_deleted(
        self,
    ):
        """
        This test verifies that when a resource has a backend_id and a failed termination order, the resource is not deleted by the cleanup task.
        """
        # Resource already in ERRED state with backend_id from setUp

        # Create a failed termination order
        failed_termination_order = marketplace_factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.TERMINATE,
            state=OrderStates.ERRED,
        )

        # Assert that the resource is erred
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.state,
            ResourceStates.ERRED,
            f"Resource {self.resource.id} should be ERRED, but got {self.resource.state}",
        )

        # Assert that there is only one order in ERRED state and that it is the failed termination order
        self.assertEqual(
            marketplace_models.Order.objects.filter(state=OrderStates.ERRED).count(),
            1,
            f"Expected 1 order in ERRED state, got {marketplace_models.Order.objects.filter(state=OrderStates.ERRED).count()}",
        )
        self.assertEqual(
            marketplace_models.Order.objects.filter(state=OrderStates.ERRED).first(),
            failed_termination_order,
            f"The order in ERRED state should be the failed termination order, but got {marketplace_models.Order.objects.filter(state=OrderStates.ERRED).first()}",
        )

        # Trigger the cleanup task
        from waldur_mastermind.marketplace import tasks

        tasks.terminate_resources_in_state_erred_without_backend_id_and_failed_terminate_order()

        # Verify that the resource is not deleted
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.state,
            ResourceStates.ERRED,
            f"Resource {self.resource.id} should not be deleted, but it is",
        )

    def test_erred_resource_with_backend_id_but_executing_terminate_order_is_not_deleted(
        self,
    ):
        """
        This test verifies that when a resource has a backend_id and an executing terminate order, the resource is not deleted by the cleanup task.
        """
        # Resource already in ERRED state with backend_id from setUp

        # Create an executing terminate order
        executing_terminate_order = marketplace_factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.TERMINATE,
            state=OrderStates.EXECUTING,
        )

        # Assert that the resource is erred
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.state,
            ResourceStates.ERRED,
            f"Resource {self.resource.id} should be ERRED, but got {self.resource.state}",
        )

        # Assert that there is one order in EXECUTING state and that it is the executing terminate order
        self.assertEqual(
            marketplace_models.Order.objects.filter(
                state=OrderStates.EXECUTING
            ).count(),
            1,
            f"Expected 1 order in EXECUTING state, got {marketplace_models.Order.objects.filter(state=OrderStates.EXECUTING).count()}",
        )
        self.assertEqual(
            marketplace_models.Order.objects.filter(
                state=OrderStates.EXECUTING
            ).first(),
            executing_terminate_order,
            f"The order in EXECUTING state should be the executing terminate order, but got {marketplace_models.Order.objects.filter(state=OrderStates.EXECUTING).first()}",
        )

        # Trigger the cleanup task
        from waldur_mastermind.marketplace import tasks

        tasks.terminate_resources_in_state_erred_without_backend_id_and_failed_terminate_order()

        # Verify that the resource is not deleted
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.state,
            ResourceStates.ERRED,
            f"Resource {self.resource.id} should not be deleted, but it is",
        )
