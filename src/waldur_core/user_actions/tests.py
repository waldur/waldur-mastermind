from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from . import models, providers
from .providers import (
    ActionCategory,
    ActionSeverity,
    BaseActionProvider,
    CorrectiveAction,
)

User = get_user_model()


class TestActionProvider(BaseActionProvider):
    """Test action provider for testing purposes"""

    action_type = "test_action"
    display_name = "Test Actions"

    def get_actions_for_user(self, user):
        return [
            {
                "title": f"Test action for {user.username}",
                "description": "A test action",
                "urgency": "medium",
                "related_object": user,
                "metadata": {"test": True},
            }
        ]

    def get_affected_users(self):
        return User.objects.all()

    def get_corrective_actions(self, user, obj):
        return [
            CorrectiveAction(
                label="View User",
                url=f"/admin/auth/user/{obj.id}/",
                category=ActionCategory.VIEW,
                severity=ActionSeverity.SAFE,
            ),
            CorrectiveAction(
                label="Send Email",
                url=f"mailto:{obj.email}",
                category=ActionCategory.CONTACT,
                severity=ActionSeverity.SAFE,
            ),
        ]


class UserActionModelTests(TestCase):
    """Test cases for user action models"""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com"
        )

    def test_create_user_action(self):
        """Test creating a user action"""
        action = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Test Action",
            description="A test action",
            urgency="medium",
            content_type=ContentType.objects.get_for_model(User),
            object_id=self.user.id,
        )

        self.assertEqual(action.user, self.user)
        self.assertEqual(action.action_type, "test_action")
        self.assertEqual(action.related_object, self.user)
        self.assertFalse(action.is_effectively_silenced)

    def test_silence_action(self):
        """Test silencing an action"""
        action = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Test Action",
            description="A test action",
            urgency="medium",
            content_type=ContentType.objects.get_for_model(User),
            object_id=self.user.id,
        )

        # Test permanent silence
        action.silence()
        self.assertTrue(action.is_effectively_silenced)
        self.assertTrue(action.is_silenced)

        # Test temporary silence
        action.is_silenced = False
        action.save()
        action.silence(duration_days=7)
        self.assertTrue(action.is_effectively_silenced)
        self.assertIsNotNone(action.silenced_until)

    def test_unsilence_action(self):
        """Test unsilencing an action"""
        action = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Test Action",
            description="A test action",
            urgency="medium",
            content_type=ContentType.objects.get_for_model(User),
            object_id=self.user.id,
            is_silenced=True,
        )

        action.unsilence()
        self.assertFalse(action.is_effectively_silenced)
        self.assertFalse(action.is_silenced)


class ActionProviderTests(TestCase):
    """Test cases for action providers"""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com"
        )
        # Clear any existing providers
        providers.clear_providers()
        # Register our test provider
        providers.register_provider(TestActionProvider)

    def tearDown(self):
        providers.clear_providers()

    def test_register_provider(self):
        """Test registering an action provider"""
        provider = providers.get_provider("test_action")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.action_type, "test_action")

    def test_get_actions_for_user(self):
        """Test getting actions for a user"""
        provider = providers.get_provider("test_action")
        actions = provider.get_actions_for_user(self.user)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["title"], f"Test action for {self.user.username}")

    def test_get_corrective_actions(self):
        """Test getting corrective actions"""
        provider = providers.get_provider("test_action")
        actions = provider.get_corrective_actions(self.user, self.user)

        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0].category, ActionCategory.VIEW)
        self.assertEqual(actions[1].category, ActionCategory.CONTACT)


class UserActionAPITests(APITestCase):
    """Test cases for the user actions API"""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", is_staff=True
        )
        self.client.force_authenticate(user=self.user)

        self.action = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Test Action",
            description="A test action",
            urgency="medium",
            content_type=ContentType.objects.get_for_model(User),
            object_id=self.user.id,
        )

    def test_list_user_actions(self):
        """Test listing user actions"""
        response = self.client.get("/api/user-actions/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["title"], "Test Action")

    def test_get_user_action_detail(self):
        """Test getting user action details"""
        response = self.client.get(f"/api/user-actions/{self.action.uuid}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["title"], "Test Action")

    def test_silence_action(self):
        """Test silencing an action via API"""
        response = self.client.post(
            f"/api/user-actions/{self.action.uuid}/silence/", {"duration_days": 7}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Refresh from database
        self.action.refresh_from_db()
        self.assertTrue(self.action.is_effectively_silenced)

    def test_unsilence_action(self):
        """Test unsilencing an action via API"""
        self.action.silence()

        response = self.client.post(f"/api/user-actions/{self.action.uuid}/unsilence/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Refresh from database
        self.action.refresh_from_db()
        self.assertFalse(self.action.is_effectively_silenced)

    def test_get_summary(self):
        """Test getting action summary"""
        # Create some additional actions with different urgencies
        models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="High Priority Action",
            description="A high priority action",
            urgency="high",
            content_type=ContentType.objects.get_for_model(User),
            object_id=self.user.id,
        )

        response = self.client.get("/api/user-actions/summary/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        summary = response.data
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_urgency"]["medium"], 1)
        self.assertEqual(summary["by_urgency"]["high"], 1)
        self.assertEqual(summary["by_type"]["test_action"], 2)

    def test_filter_by_urgency(self):
        """Test filtering actions by urgency"""
        models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Low Priority Action",
            description="A low priority action",
            urgency="low",
            content_type=ContentType.objects.get_for_model(User),
            object_id=self.user.id,
        )

        response = self.client.get("/api/user-actions/?urgency=medium")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["urgency"], "medium")

    def test_filter_silenced_actions(self):
        """Test filtering silenced actions"""
        self.action.silence()

        # Default should exclude silenced
        response = self.client.get("/api/user-actions/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 0)

        # Explicit include should show silenced
        response = self.client.get("/api/user-actions/?include_silenced=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_user_can_only_see_own_actions(self):
        """Test that users can only see their own actions"""
        # Create another user and action
        other_user = User.objects.create_user(
            username="otheruser", email="other@example.com", is_staff=True
        )
        other_action = models.UserAction.objects.create(
            user=other_user,
            action_type="test_action",
            title="Other User Action",
            description="This should not be visible",
            urgency="high",
            content_type=ContentType.objects.get_for_model(User),
            object_id=other_user.id,
        )

        # Current user should only see their own action
        response = self.client.get("/api/user-actions/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["title"], "Test Action")

        # Should not be able to access other user's action by UUID
        response = self.client.get(f"/api/user-actions/{other_action.uuid}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_access_denied(self):
        """Test that unauthenticated users cannot access actions"""
        # Clear authentication
        self.client.force_authenticate(user=None)

        response = self.client.get("/api/user-actions/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        response = self.client.get(f"/api/user-actions/{self.action.uuid}/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TaskTests(TestCase):
    """Test cases for Celery tasks"""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com"
        )
        # Clear and register test provider
        providers.clear_providers()
        providers.register_provider(TestActionProvider)

    def tearDown(self):
        providers.clear_providers()

    @patch("waldur_core.user_actions.tasks.update_user_actions_for_provider.delay")
    def test_update_actions_for_provider(self, mock_task):
        """Test updating actions for a provider"""
        from . import tasks

        # Create a provider record
        provider_record = models.UserActionProvider.objects.create(
            action_type="test_action",
            app_name="user_actions",
            provider_class="TestActionProvider",
        )

        tasks.update_actions_for_provider("test_action")

        # Should queue a task for each user
        self.assertTrue(mock_task.called)

        # Check provider status was updated
        provider_record.refresh_from_db()
        self.assertIn("COMPLETED", provider_record.last_execution_status)

    def test_update_user_actions_for_provider(self):
        """Test updating user actions for a specific provider"""
        from . import tasks

        # Initially no actions
        self.assertEqual(models.UserAction.objects.count(), 0)

        # Run the task
        tasks.update_user_actions_for_provider(self.user.id, "test_action")

        # Should create one action
        self.assertEqual(models.UserAction.objects.count(), 1)

        action = models.UserAction.objects.first()
        self.assertEqual(action.user, self.user)
        self.assertEqual(action.action_type, "test_action")
        self.assertEqual(action.related_object, self.user)

    def test_cleanup_stale_actions(self):
        """Test cleaning up stale actions"""
        from . import tasks

        # Create an action manually that won't be returned by provider
        stale_action = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Stale Action",
            description="This will be cleaned up",
            urgency="low",
            content_type=ContentType.objects.get_for_model(User),
            object_id=999,  # Non-existent user ID
        )

        # Create a valid action via the task
        tasks.update_user_actions_for_provider(self.user.id, "test_action")

        # Should have 2 actions
        self.assertEqual(models.UserAction.objects.count(), 2)

        # Run cleanup
        tasks.cleanup_stale_actions(self.user.id, "test_action")

        # Should remove the stale action, keep the valid one
        self.assertEqual(models.UserAction.objects.count(), 1)
        self.assertFalse(models.UserAction.objects.filter(id=stale_action.id).exists())
