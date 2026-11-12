from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIRequestFactory, APITestCase

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.user_actions.providers import (
    ActionCategory,
)
from waldur_mastermind.marketplace.enums import OrderTypes, ResourceStates
from waldur_mastermind.marketplace.models import Order
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_mastermind.marketplace.user_actions import ExpiringResourceProvider


class ConfigurableExpiringResourceTest(APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.provider = ExpiringResourceProvider()
        self.factory = APIRequestFactory()

    def test_default_threshold_behavior(self):
        """Test default 30-day threshold"""
        # Mark component as prepaid (required for expiration tracking)
        self.fixture.offering_component.is_prepaid = True
        self.fixture.offering_component.save()

        # Resource expiring in 29 days (should trigger)
        expire_date = timezone.now() + timedelta(days=29)
        resource = self.fixture.resource
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()

        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)
        actions = self.provider.get_actions_for_user(self.fixture.user)
        self.assertEqual(len(actions), 1)

    def test_custom_threshold_exclusion(self):
        """Test that custom small threshold excludes resources expiring later"""
        # Configure offering with 7 day threshold
        self.fixture.offering.plugin_options = {"resource_expiration_threshold": 7}
        self.fixture.offering.save()

        # Resource expiring in 10 days (should NOT trigger)
        expire_date = timezone.now() + timedelta(days=10)
        resource = self.fixture.resource
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()

        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)
        actions = self.provider.get_actions_for_user(self.fixture.user)
        self.assertEqual(len(actions), 0)

    def test_custom_threshold_inclusion(self):
        """Test that custom small threshold includes resources expiring sooner"""
        # Mark component as prepaid (required for expiration tracking)
        self.fixture.offering_component.is_prepaid = True
        self.fixture.offering_component.save()

        # Configure offering with 7 day threshold
        self.fixture.offering.plugin_options = {"resource_expiration_threshold": 7}
        self.fixture.offering.save()

        # Resource expiring in 5 days (should trigger)
        expire_date = timezone.now() + timedelta(days=5)
        resource = self.fixture.resource
        resource.end_date = expire_date.date()
        resource.state = ResourceStates.OK
        resource.save()

        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)
        actions = self.provider.get_actions_for_user(self.fixture.user)
        self.assertEqual(len(actions), 1)

    def test_terminate_action_creation(self):
        """Test generation of Terminate corrective action"""
        resource = self.fixture.resource
        # Add termination permission
        ProjectRole.ADMIN.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

        actions = self.provider.get_corrective_actions(self.fixture.user, resource)

        terminate_action = next(
            (a for a in actions if a.category == ActionCategory.TERMINATE), None
        )
        self.assertIsNotNone(terminate_action)
        self.assertEqual(terminate_action.label, "Terminate Resource")
        self.assertTrue(terminate_action.api_endpoint)

    def test_execute_terminate_action(self):
        """Test execution of Terminate action acknowledges (silences) the action"""
        resource = self.fixture.resource
        ProjectRole.ADMIN.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        self.fixture.project.add_user(self.fixture.user, ProjectRole.ADMIN)

        # Mock request
        request = self.factory.post("/")
        request.user = self.fixture.user

        # Get the action object
        actions = self.provider.get_corrective_actions(self.fixture.user, resource)
        terminate_action = next(
            (a for a in actions if a.category == ActionCategory.TERMINATE), None
        )

        # Mock user_action
        user_action = mock.Mock()

        # Execute
        result = self.provider.execute_action(
            self.fixture.user,
            terminate_action,
            resource,
            request=request,
            user_action=user_action,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "completed")
        self.assertIn("acknowledged", result["message"])

        # Verify action was silenced
        user_action.silence.assert_called_once()

        # Verify NO order was created
        self.assertFalse(
            Order.objects.filter(resource=resource, type=OrderTypes.TERMINATE).exists()
        )


class ReminderScheduleParsingTest(APITestCase):
    def test_dashboard_keeps_the_schedule_as_listed(self):
        # Urgency is worked out from the schedule's length, so duplicates stay.
        fixture = MarketplaceFixture()
        fixture.offering.plugin_options = {
            "resource_expiration_reminders": [7, 30, 1, 7]
        }
        self.assertEqual(
            ExpiringResourceProvider()._get_reminder_schedule(fixture.offering),
            [30, 7, 7, 1],
        )

    @override_config(USER_ACTIONS_DEFAULT_EXPIRATION_REMINDERS=[30, 14, 7, 1])
    def test_dashboard_ignores_a_schedule_with_too_many_days(self):
        fixture = MarketplaceFixture()
        fixture.offering.plugin_options = {
            "resource_expiration_reminders": list(range(1, 3651))
        }
        self.assertEqual(
            ExpiringResourceProvider()._get_reminder_schedule(fixture.offering),
            [30, 14, 7, 1],
        )

    def test_day_past_the_calendar_does_not_break_the_dashboard(self):
        fixture = MarketplaceFixture()
        fixture.offering_component.is_prepaid = True
        fixture.offering_component.save()
        fixture.offering.plugin_options = {"resource_expiration_reminders": [3000000]}
        fixture.offering.save()
        resource = fixture.resource
        resource.end_date = (timezone.now() + timedelta(days=5)).date()
        resource.state = ResourceStates.OK
        resource.save()
        fixture.project.add_user(fixture.user, ProjectRole.ADMIN)

        actions = ExpiringResourceProvider().get_actions_for_user(fixture.user)

        self.assertEqual(len(actions), 1)
