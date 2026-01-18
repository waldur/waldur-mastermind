from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.user_actions.models import UserAction
from waldur_core.user_actions.tasks import (
    cleanup_stale_actions,
    update_user_actions_for_provider,
)
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


class OrderStateChangeRecalculationTest(APITransactionTestCase):
    """Test that UserActions are recalculated when Order state changes."""

    def setUp(self):
        self.fixture = MarketplaceFixture()
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

    def _create_pending_order_with_action(self):
        """Helper to create a pending order and its UserAction."""
        # Create an order that's been pending for 25 hours
        cutoff = timezone.now() - timedelta(hours=25)
        order = self.fixture.order
        order.state = OrderStates.PENDING_CONSUMER
        order.created = cutoff
        order.save()

        # Run the task to create the UserAction
        update_user_actions_for_provider(self.fixture.user.id, "pending_order")

        return order

    def test_signal_triggered_when_order_leaves_pending_state(self):
        """Test that recalculation task is triggered when order is approved."""
        order = self._create_pending_order_with_action()

        # Verify action exists
        self.assertTrue(
            UserAction.objects.filter(
                user=self.fixture.user,
                action_type="pending_order",
                object_id=order.id,
            ).exists()
        )

        with mock.patch(
            "waldur_core.user_actions.tasks.update_actions_for_provider"
        ) as mock_task:
            # Approve the order (transition out of pending state)
            order.state = OrderStates.EXECUTING
            order.save()

            # Verify task was scheduled (via transaction.on_commit)
            # Note: In test environment, on_commit runs immediately
            mock_task.delay.assert_called_once_with("pending_order")

    def test_signal_not_triggered_for_non_pending_state_changes(self):
        """Test that recalculation is NOT triggered for other state transitions."""
        order = self.fixture.order
        order.state = OrderStates.EXECUTING
        order.save()

        with mock.patch(
            "waldur_core.user_actions.tasks.update_actions_for_provider"
        ) as mock_task:
            # Transition from EXECUTING to DONE (not from pending)
            order.state = OrderStates.DONE
            order.save()

            # Task should NOT be called
            mock_task.delay.assert_not_called()

    def test_signal_not_triggered_on_order_creation(self):
        """Test that recalculation is NOT triggered on order creation."""
        from waldur_mastermind.marketplace.tests import factories

        with mock.patch(
            "waldur_core.user_actions.tasks.update_actions_for_provider"
        ) as mock_task:
            # Create a new order using factory (which sets up all required relations)
            factories.OrderFactory(
                project=self.fixture.project,
                state=OrderStates.PENDING_CONSUMER,
                created_by=self.fixture.user,
            )

            # Task should NOT be called on creation
            mock_task.delay.assert_not_called()

    def test_signal_triggered_for_all_pending_states(self):
        """Test that recalculation is triggered for all pending state transitions."""
        pending_states = [
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.PENDING_PROJECT,
            OrderStates.PENDING_START_DATE,
        ]

        for pending_state in pending_states:
            with self.subTest(pending_state=pending_state):
                order = self.fixture.order
                order.state = pending_state
                order.save()

                with mock.patch(
                    "waldur_core.user_actions.tasks.update_actions_for_provider"
                ) as mock_task:
                    # Transition to executing
                    order.state = OrderStates.EXECUTING
                    order.save()

                    mock_task.delay.assert_called_once_with("pending_order")

    def test_action_removed_after_order_approved(self):
        """Integration test: verify action is actually removed after order approval."""
        order = self._create_pending_order_with_action()

        # Verify action exists
        self.assertTrue(
            UserAction.objects.filter(
                user=self.fixture.user,
                action_type="pending_order",
                object_id=order.id,
            ).exists()
        )

        # Approve the order
        order.state = OrderStates.EXECUTING
        order.save()

        # Run the cleanup task (simulating what would happen after provider update)
        cleanup_stale_actions(self.fixture.user.id, "pending_order")

        # Verify action is removed (order is no longer pending, so provider won't return it)
        self.assertFalse(
            UserAction.objects.filter(
                user=self.fixture.user,
                action_type="pending_order",
                object_id=order.id,
            ).exists()
        )

    def test_action_removed_after_order_rejected(self):
        """Test that action is removed when order is rejected."""
        order = self._create_pending_order_with_action()

        # Verify action exists
        self.assertTrue(
            UserAction.objects.filter(
                user=self.fixture.user,
                action_type="pending_order",
                object_id=order.id,
            ).exists()
        )

        # Reject the order
        order.state = OrderStates.REJECTED
        order.save()

        # Run the cleanup task
        cleanup_stale_actions(self.fixture.user.id, "pending_order")

        # Verify action is removed
        self.assertFalse(
            UserAction.objects.filter(
                user=self.fixture.user,
                action_type="pending_order",
                object_id=order.id,
            ).exists()
        )

    def test_action_removed_after_order_cancelled(self):
        """Test that action is removed when order is cancelled."""
        order = self._create_pending_order_with_action()

        # Verify action exists before cancellation
        self.assertTrue(
            UserAction.objects.filter(
                user=self.fixture.user,
                action_type="pending_order",
                object_id=order.id,
            ).exists()
        )

        # Cancel the order
        order.state = OrderStates.CANCELED
        order.save()

        # Run the cleanup task
        cleanup_stale_actions(self.fixture.user.id, "pending_order")

        # Verify action is removed
        self.assertFalse(
            UserAction.objects.filter(
                user=self.fixture.user,
                action_type="pending_order",
                object_id=order.id,
            ).exists()
        )


class ExecuteActionCleanupTest(APITransactionTestCase):
    """Test that cleanup is triggered after executing a corrective action."""

    def setUp(self):
        self.fixture = MarketplaceFixture()
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

    def _create_user_action(self):
        """Helper to create a UserAction for testing."""
        from django.contrib.contenttypes.models import ContentType

        order = self.fixture.order
        order.state = OrderStates.PENDING_CONSUMER
        order.created = timezone.now() - timedelta(hours=25)
        order.save()

        content_type = ContentType.objects.get_for_model(order)

        action = UserAction.objects.create(
            user=self.fixture.user,
            action_type="pending_order",
            title="Test action",
            description="Test description",
            urgency="high",
            content_type=content_type,
            object_id=order.id,
            corrective_actions=[
                {
                    "label": "View Order Details",
                    "category": "view",
                    "severity": "safe",
                    "method": "GET",
                    "api_endpoint": None,
                    "confirmation_required": False,
                    "permissions_required": [],
                    "metadata": {},
                    "route_name": "marketplace-orders.details",
                    "route_params": {"order_uuid": str(order.uuid)},
                }
            ],
        )
        return action, order

    def test_cleanup_triggered_after_successful_execute_action(self):
        """Test that cleanup_stale_actions is called after successful action execution."""
        action, order = self._create_user_action()

        self.client.force_authenticate(self.fixture.user)

        with mock.patch(
            "waldur_core.user_actions.tasks.cleanup_stale_actions"
        ) as mock_cleanup:
            response = self.client.post(
                f"/api/user-actions/{action.uuid}/execute_action/",
                {"action_label": "View Order Details"},
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Verify cleanup was scheduled
            mock_cleanup.delay.assert_called_once_with(
                self.fixture.user.id, "pending_order"
            )

    def test_cleanup_not_triggered_after_failed_execute_action(self):
        """Test that cleanup is NOT called if action execution fails."""
        action, order = self._create_user_action()

        self.client.force_authenticate(self.fixture.user)

        with mock.patch(
            "waldur_core.user_actions.tasks.cleanup_stale_actions"
        ) as mock_cleanup:
            # Try to execute non-existent action
            response = self.client.post(
                f"/api/user-actions/{action.uuid}/execute_action/",
                {"action_label": "Non-existent Action"},
            )

            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

            # Cleanup should NOT be called
            mock_cleanup.delay.assert_not_called()

    def test_cleanup_uses_correct_action_type(self):
        """Test that cleanup is called with the correct action_type."""
        from django.contrib.contenttypes.models import ContentType

        # Create an action with a different action_type
        resource = self.fixture.resource
        content_type = ContentType.objects.get_for_model(resource)

        action = UserAction.objects.create(
            user=self.fixture.user,
            action_type="expiring_resource",
            title="Expiring resource",
            description="Resource expires soon",
            urgency="medium",
            content_type=content_type,
            object_id=resource.id,
            corrective_actions=[
                {
                    "label": "View Resource",
                    "category": "view",
                    "severity": "safe",
                    "method": "GET",
                    "api_endpoint": None,
                    "confirmation_required": False,
                    "permissions_required": [],
                    "metadata": {},
                    "route_name": "marketplace-resources.details",
                    "route_params": {"resource_uuid": str(resource.uuid)},
                }
            ],
        )

        self.client.force_authenticate(self.fixture.user)

        with mock.patch(
            "waldur_core.user_actions.tasks.cleanup_stale_actions"
        ) as mock_cleanup:
            response = self.client.post(
                f"/api/user-actions/{action.uuid}/execute_action/",
                {"action_label": "View Resource"},
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Verify cleanup was called with correct action_type
            mock_cleanup.delay.assert_called_once_with(
                self.fixture.user.id, "expiring_resource"
            )
