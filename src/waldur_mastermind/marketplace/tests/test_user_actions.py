from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITransactionTestCase

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.user_actions.providers import (
    ActionCategory,
    ActionSeverity,
    get_all_providers,
)
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_mastermind.marketplace.user_actions import (
    ExpiringResourceProvider,
    PendingOrderProvider,
)


class PendingOrderProviderTest(APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.provider = PendingOrderProvider()

    def test_get_actions_for_user_with_pending_order(self):
        """Test that pending orders are detected for project admins"""
        # Add APPROVE_ORDER permission to admin role
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)

        # Create an order that's been pending for 25 hours
        cutoff = timezone.now() - timedelta(hours=25)
        order = self.fixture.order
        order.state = OrderStates.PENDING_CONSUMER
        order.created = cutoff
        order.save()

        # Add admin role for user
        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

        actions = self.provider.get_actions_for_user(self.fixture.user)

        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIn("Approve pending order", action["title"])
        self.assertIn(order.offering.name, action["title"])
        self.assertEqual(action["urgency"], "high")
        self.assertEqual(action["related_object"], order)
        self.assertEqual(action["offering_uuid"], order.offering.uuid)
        self.assertIn("1 days", action["description"])

    def test_get_actions_for_user_no_pending_orders(self):
        """Test that no actions are returned when no orders are pending > 24h"""
        # Create a recent order (< 24 hours)
        recent_time = timezone.now() - timedelta(hours=12)
        order = self.fixture.order
        order.state = OrderStates.PENDING_CONSUMER
        order.created = recent_time
        order.save()

        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

        actions = self.provider.get_actions_for_user(self.fixture.user)
        self.assertEqual(len(actions), 0)

    def test_get_actions_for_user_no_permissions(self):
        """Test that no actions are returned for users without project permissions"""
        cutoff = timezone.now() - timedelta(hours=25)
        order = self.fixture.order
        order.state = OrderStates.PENDING_CONSUMER
        order.created = cutoff
        order.save()

        # Don't add any permissions for the user
        actions = self.provider.get_actions_for_user(self.fixture.user)
        self.assertEqual(len(actions), 0)

    def test_get_corrective_actions_for_admin(self):
        """Test corrective actions are provided for project admin"""
        # Add APPROVE_ORDER permission to admin role
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)

        order = self.fixture.order
        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

        actions = self.provider.get_corrective_actions(self.fixture.user, order)

        # Should have multiple actions: view, approve, reject, contact
        self.assertGreaterEqual(len(actions), 2)

        # Check approve action
        approve_action = next((a for a in actions if a.label == "Approve Order"), None)
        self.assertIsNotNone(approve_action)
        self.assertEqual(approve_action.category, ActionCategory.APPROVE)
        self.assertEqual(approve_action.severity, ActionSeverity.LOW)
        self.assertEqual(approve_action.method, "POST")
        self.assertTrue(approve_action.confirmation_required)
        # For API actions, we just verify they're properly marked as API endpoints
        self.assertTrue(approve_action.api_endpoint)

        # Check view action
        view_action = next(
            (a for a in actions if a.label == "View Order Details"), None
        )
        self.assertIsNotNone(view_action)
        self.assertEqual(view_action.category, ActionCategory.VIEW)
        self.assertEqual(view_action.severity, ActionSeverity.SAFE)

    def test_get_corrective_actions_no_permissions(self):
        """Test corrective actions for users without permissions"""
        order = self.fixture.order
        # Don't add permissions

        actions = self.provider.get_corrective_actions(self.fixture.user, order)

        # Should only have view action, no approve/reject
        view_actions = [a for a in actions if a.category == ActionCategory.VIEW]
        approve_actions = [a for a in actions if a.category == ActionCategory.APPROVE]

        self.assertGreater(len(view_actions), 0)
        self.assertEqual(len(approve_actions), 0)


class ExpiringResourceProviderTest(APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.provider = ExpiringResourceProvider()

    def test_get_actions_for_user_with_expiring_resource(self):
        """Test that expiring resources are detected"""
        # Create a resource expiring in 15 days
        expire_date = timezone.now() + timedelta(days=15)
        resource = self.fixture.resource
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()

        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

        actions = self.provider.get_actions_for_user(self.fixture.user)

        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIn("Resource expiring", action["title"])
        self.assertIn(resource.name, action["title"])
        self.assertEqual(action["related_object"], resource)
        self.assertEqual(action["offering_uuid"], resource.offering.uuid)
        # Check that it mentions some number of days (could be 14-15 due to timing)
        self.assertRegex(action["description"], r"\b1[4-5] days\b")

    def test_get_actions_for_user_no_expiring_resources(self):
        """Test no actions for resources expiring > 30 days"""
        # Create a resource expiring in 45 days
        expire_date = timezone.now() + timedelta(days=45)
        resource = self.fixture.resource
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()

        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

        actions = self.provider.get_actions_for_user(self.fixture.user)
        self.assertEqual(len(actions), 0)


class MarketplaceUserActionsIntegrationTest(APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()

    def test_marketplace_providers_are_registered(self):
        """Test that marketplace providers are properly registered"""
        providers = get_all_providers()
        provider_types = list(providers.keys())

        self.assertIn("pending_order", provider_types)
        self.assertIn("expiring_resource", provider_types)

    def test_action_routing_is_correctly_configured(self):
        """Test that actions have proper route configuration"""
        # Add APPROVE_ORDER permission to admin role
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)

        order = self.fixture.order
        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

        provider = PendingOrderProvider()
        actions = provider.get_corrective_actions(self.fixture.user, order)

        # Check that view action uses proper route configuration (for frontend)
        view_action = next(
            (a for a in actions if a.category == ActionCategory.VIEW), None
        )
        self.assertIsNotNone(view_action)
        self.assertEqual(view_action.route_name, "marketplace-orders.details")
        self.assertEqual(view_action.route_params["order_uuid"], str(order.uuid))

        # Check that approve action is properly configured for API calls if user has permissions
        approve_action = next(
            (a for a in actions if a.category == ActionCategory.APPROVE), None
        )
        if approve_action:
            # Backend API actions should be marked as api_endpoint
            self.assertTrue(approve_action.api_endpoint)
            self.assertEqual(approve_action.method, "POST")
            self.assertTrue(approve_action.confirmation_required)
