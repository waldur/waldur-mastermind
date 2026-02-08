from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APITestCase

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


class PendingOrderProviderTest(APITestCase):
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
        self.assertEqual(action["offering_uuid"], str(order.offering.uuid))
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

    @mock.patch("waldur_mastermind.marketplace.user_actions.config")
    def test_configurable_pending_order_hours(self, mock_config):
        """Test that pending order hours can be configured via constance"""
        # Add APPROVE_ORDER permission to admin role
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)

        # Configure to 12 hours instead of default 24
        mock_config.USER_ACTIONS_PENDING_ORDER_HOURS = 12

        # Create an order that's been pending for 15 hours (> 12 but < 24)
        cutoff = timezone.now() - timedelta(hours=15)
        order = self.fixture.order
        order.state = OrderStates.PENDING_CONSUMER
        order.created = cutoff
        order.save()

        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

        actions = self.provider.get_actions_for_user(self.fixture.user)

        # Should detect the order since 15h > 12h configured threshold
        self.assertEqual(len(actions), 1)

    @mock.patch("waldur_mastermind.marketplace.user_actions.config")
    def test_configurable_pending_order_hours_excludes_recent(self, mock_config):
        """Test that orders within configured hours are not shown"""
        # Add APPROVE_ORDER permission to admin role
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)

        # Configure to 48 hours instead of default 24
        mock_config.USER_ACTIONS_PENDING_ORDER_HOURS = 48

        # Create an order that's been pending for 30 hours (< 48)
        cutoff = timezone.now() - timedelta(hours=30)
        order = self.fixture.order
        order.state = OrderStates.PENDING_CONSUMER
        order.created = cutoff
        order.save()

        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

        actions = self.provider.get_actions_for_user(self.fixture.user)

        # Should NOT detect the order since 30h < 48h configured threshold
        self.assertEqual(len(actions), 0)


class ExpiringResourceProviderTest(APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.provider = ExpiringResourceProvider()

        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

    def test_get_actions_for_user_with_expiring_resource(self):
        """Test that expiring resources are detected"""
        # Create a resource expiring in 15 days
        expire_date = timezone.now() + timedelta(days=15)
        resource = self.fixture.resource
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()
        resource.offering.components.create(
            type="ram",
            name="RAM",
            measured_unit="GB",
            billing_type="fixed",
            is_prepaid=True,
        )

        actions = self.provider.get_actions_for_user(self.fixture.user)

        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIn("Resource expiring", action["title"])
        self.assertIn(resource.name, action["title"])
        self.assertEqual(action["related_object"], resource)
        self.assertEqual(action["offering_uuid"], str(resource.offering.uuid))
        # Check that it mentions some number of days (could be 14-15 due to timing)
        self.assertRegex(action["description"], r"\b1[4-5] days\b")

    def test_get_actions_for_user_with_non_prepaid_resource_ignores_it(self):
        """Test that non-prepaid expiring resources are ignored"""
        # Create a resource expiring in 15 days
        expire_date = timezone.now() + timedelta(days=15)
        resource = self.fixture.resource
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()
        # Ensure no prepaid components exist
        resource.offering.components.filter(is_prepaid=True).delete()

        actions = self.provider.get_actions_for_user(self.fixture.user)
        self.assertEqual(len(actions), 0)

    def test_get_actions_excludes_resources_with_pending_orders(self):
        """Test that resources with pending orders are excluded from expiring resources"""
        # Create an expiring resource
        expire_date = timezone.now() + timedelta(days=15)
        resource = self.fixture.resource
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()
        resource.offering.components.update_or_create(
            type="storage",
            defaults={
                "name": "Storage",
                "measured_unit": "GB",
                "billing_type": "fixed",
                "is_prepaid": True,
            },
        )

        # Initially should show expiring resource action
        actions = self.provider.get_actions_for_user(self.fixture.user)
        self.assertEqual(len(actions), 1)

        # Create a pending order for the resource
        order = self.fixture.order
        order.resource = resource
        order.state = OrderStates.PENDING_CONSUMER
        order.save()

        # Now the resource should be excluded since it has a pending order
        actions = self.provider.get_actions_for_user(self.fixture.user)
        self.assertEqual(len(actions), 0)

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

    @mock.patch("waldur_mastermind.marketplace.user_actions.config")
    def test_per_offering_reminder_schedule(self, mock_config):
        """Test that offering-specific reminder schedules are used"""
        mock_config.USER_ACTIONS_DEFAULT_EXPIRATION_REMINDERS = [30, 14, 7, 1]

        # Configure offering with annual subscription reminder schedule
        resource = self.fixture.resource
        resource.offering.plugin_options = {
            "resource_expiration_reminders": [90, 60, 30, 14, 7, 1]
        }
        resource.offering.save()

        # Create a resource expiring in 25 days (within 90-day threshold)
        # For [90, 60, 30, 14, 7, 1], 25 days → active = [90, 60, 30], position = 2
        # total = 6, third = 2, position 2 >= 2 but < 4 → medium urgency
        expire_date = timezone.now() + timedelta(days=25)
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()
        resource.offering.components.update_or_create(
            type="cpu",
            defaults={
                "name": "CPU",
                "measured_unit": "cores",
                "billing_type": "fixed",
                "is_prepaid": True,
            },
        )

        actions = self.provider.get_actions_for_user(self.fixture.user)

        # Should detect the resource since 25 days is within 90-day reminder schedule
        self.assertEqual(len(actions), 1)
        action = actions[0]
        # 25 days is in the middle third of schedule → medium urgency
        self.assertEqual(action["urgency"], "medium")

    @mock.patch("waldur_mastermind.marketplace.user_actions.config")
    def test_per_offering_reminder_schedule_high_urgency(self, mock_config):
        """Test that urgency is high for resources nearing expiration"""
        mock_config.USER_ACTIONS_DEFAULT_EXPIRATION_REMINDERS = [30, 14, 7, 1]

        # Configure offering with annual subscription reminder schedule
        resource = self.fixture.resource
        resource.offering.plugin_options = {
            "resource_expiration_reminders": [90, 60, 30, 14, 7, 1]
        }
        resource.offering.save()

        # Create a resource expiring in 5 days (near the end of the schedule)
        expire_date = timezone.now() + timedelta(days=5)
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()
        resource.offering.components.update_or_create(
            type="mem",
            defaults={
                "name": "Memory",
                "measured_unit": "GB",
                "billing_type": "fixed",
                "is_prepaid": True,
            },
        )

        actions = self.provider.get_actions_for_user(self.fixture.user)

        self.assertEqual(len(actions), 1)
        action = actions[0]
        # 5 days: active = [90, 60, 30, 14, 7], position = 4, which is >= 2*third → high urgency
        self.assertEqual(action["urgency"], "high")

    @mock.patch("waldur_mastermind.marketplace.user_actions.config")
    def test_resource_outside_reminder_schedule_not_shown(self, mock_config):
        """Test that resources outside reminder schedule are not shown"""
        mock_config.USER_ACTIONS_DEFAULT_EXPIRATION_REMINDERS = [30, 14, 7, 1]

        # Configure offering with monthly subscription (max 30 days)
        resource = self.fixture.resource
        resource.offering.plugin_options = {
            "resource_expiration_reminders": [30, 14, 7, 1]
        }
        resource.offering.save()

        # Create a resource expiring in 45 days (outside 30-day threshold)
        expire_date = timezone.now() + timedelta(days=45)
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()
        resource.offering.components.create(
            type="disk",
            name="Disk",
            measured_unit="GB",
            billing_type="fixed",
            is_prepaid=True,
        )

        actions = self.provider.get_actions_for_user(self.fixture.user)

        # Should NOT show resource since 45 days > max(30, 14, 7, 1) = 30
        self.assertEqual(len(actions), 0)

    def test_urgency_from_schedule_calculation(self):
        """Test the urgency calculation based on position in reminder schedule"""
        provider = ExpiringResourceProvider()

        # Test with 6-item schedule [90, 60, 30, 14, 7, 1]
        reminders = [90, 60, 30, 14, 7, 1]

        # First third (positions 0-1): 90 and 60 days → low
        self.assertEqual(provider._get_urgency_from_schedule(85, reminders), "low")
        self.assertEqual(provider._get_urgency_from_schedule(55, reminders), "low")

        # Middle third (positions 2-3): 30 and 14 days → medium
        self.assertEqual(provider._get_urgency_from_schedule(25, reminders), "medium")
        self.assertEqual(provider._get_urgency_from_schedule(10, reminders), "medium")

        # Last third (positions 4-5): 7 and 1 days → high
        self.assertEqual(provider._get_urgency_from_schedule(5, reminders), "high")
        self.assertEqual(provider._get_urgency_from_schedule(0, reminders), "high")

    def test_reminder_schedule_defaults_to_global_config(self):
        """Test that missing offering config falls back to global default"""
        provider = ExpiringResourceProvider()

        # Create a mock offering without reminder config
        offering = self.fixture.offering
        offering.plugin_options = {}  # No reminder config
        offering.save()

        with mock.patch(
            "waldur_mastermind.marketplace.user_actions.config"
        ) as mock_config:
            mock_config.USER_ACTIONS_DEFAULT_EXPIRATION_REMINDERS = [60, 30, 7]
            reminders = provider._get_reminder_schedule(offering)

        self.assertEqual(reminders, [60, 30, 7])


class MarketplaceUserActionsIntegrationTest(APITestCase):
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
