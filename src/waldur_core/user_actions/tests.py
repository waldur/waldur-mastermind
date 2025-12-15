import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from . import models, providers
from .handlers import register_cleanup_handler, schedule_cleanup_for_deleted_object
from .providers import (
    ActionCategory,
    ActionSeverity,
    BaseActionProvider,
    CorrectiveAction,
)
from .tasks import cleanup_actions_for_deleted_object, cleanup_dangling_user_actions

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


class TestModel(django_models.Model):
    """Test model for cleanup testing"""

    name = django_models.CharField(max_length=100)

    class Meta:
        app_label = "user_actions"


class CleanupTasksTests(TestCase):
    """Test cases for cleanup tasks that detect and remove dangling user actions"""

    def setUp(self):
        unique_id = str(uuid.uuid4())[:8]
        self.user = User.objects.create_user(
            username=f"cleanup_testuser_{unique_id}",
            email=f"cleanup_test_{unique_id}@example.com",
        )
        self.user2 = User.objects.create_user(
            username=f"cleanup_testuser2_{unique_id}",
            email=f"cleanup_test2_{unique_id}@example.com",
        )

        # Create content types for testing
        self.user_ct = ContentType.objects.get_for_model(User)

    def test_cleanup_actions_for_deleted_object_removes_specific_actions(self):
        """Test that targeted cleanup removes only actions for specific deleted object"""

        # Create user actions for both users
        action1 = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Action for User 1",
            description="Test action",
            urgency="medium",
            content_type=self.user_ct,
            object_id=self.user.id,
        )

        action2 = models.UserAction.objects.create(
            user=self.user2,
            action_type="test_action",
            title="Action for User 2",
            description="Test action",
            urgency="medium",
            content_type=self.user_ct,
            object_id=self.user2.id,
        )

        # Create action for non-existent object
        dangling_action = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Dangling Action",
            description="Points to deleted object",
            urgency="high",
            content_type=self.user_ct,
            object_id=999999,  # Non-existent user ID
        )

        self.assertEqual(models.UserAction.objects.count(), 3)

        # Run targeted cleanup for the non-existent object
        deleted_count = cleanup_actions_for_deleted_object(self.user_ct.id, 999999)

        # Should delete only the dangling action
        self.assertEqual(deleted_count, 1)
        self.assertEqual(models.UserAction.objects.count(), 2)
        self.assertFalse(
            models.UserAction.objects.filter(id=dangling_action.id).exists()
        )
        self.assertTrue(models.UserAction.objects.filter(id=action1.id).exists())
        self.assertTrue(models.UserAction.objects.filter(id=action2.id).exists())

    def test_cleanup_actions_for_deleted_object_handles_existing_object(self):
        """Test that cleanup doesn't remove actions for existing objects"""

        # Create action for existing user
        models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Valid Action",
            description="Points to existing object",
            urgency="medium",
            content_type=self.user_ct,
            object_id=self.user.id,
        )

        self.assertEqual(models.UserAction.objects.count(), 1)

        # Run cleanup for existing object
        deleted_count = cleanup_actions_for_deleted_object(
            self.user_ct.id, self.user.id
        )

        # Should delete the action since we're cleaning up for "deleted" object
        self.assertEqual(deleted_count, 1)
        self.assertEqual(models.UserAction.objects.count(), 0)

    def test_cleanup_actions_for_deleted_object_handles_invalid_content_type(self):
        """Test cleanup handles invalid content type gracefully"""

        # Try cleanup with non-existent content type
        deleted_count = cleanup_actions_for_deleted_object(999999, 123)

        # Should return 0 and not crash
        self.assertEqual(deleted_count, 0)

    def test_cleanup_dangling_user_actions_detects_broken_references(self):
        """Test that periodic cleanup detects and removes actions with broken object references"""

        # Create valid action
        valid_action = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Valid Action",
            description="Points to existing object",
            urgency="medium",
            content_type=self.user_ct,
            object_id=self.user.id,
        )

        # Create action pointing to deleted object
        dangling_action = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Dangling Action",
            description="Points to deleted object",
            urgency="high",
            content_type=self.user_ct,
            object_id=999999,  # Non-existent user ID
        )

        self.assertEqual(models.UserAction.objects.count(), 2)

        # Run periodic cleanup
        deleted_count = cleanup_dangling_user_actions()

        # Should remove only the dangling action
        self.assertEqual(deleted_count, 1)
        self.assertEqual(models.UserAction.objects.count(), 1)
        self.assertTrue(models.UserAction.objects.filter(id=valid_action.id).exists())
        self.assertFalse(
            models.UserAction.objects.filter(id=dangling_action.id).exists()
        )

    def test_cleanup_dangling_user_actions_handles_multiple_content_types(self):
        """Test cleanup works across different content types"""

        # Create actions for different content types
        user_action = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="User Action",
            description="Valid user action",
            urgency="medium",
            content_type=self.user_ct,
            object_id=self.user.id,
        )

        # Create action for non-existent object with different content type
        content_type_ct = ContentType.objects.get_for_model(ContentType)
        models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Dangling ContentType Action",
            description="Points to deleted ContentType",
            urgency="high",
            content_type=content_type_ct,
            object_id=999999,  # Non-existent ContentType ID
        )

        self.assertEqual(models.UserAction.objects.count(), 2)

        # Run cleanup
        deleted_count = cleanup_dangling_user_actions()

        # Should remove only the dangling action
        self.assertEqual(deleted_count, 1)
        self.assertEqual(models.UserAction.objects.count(), 1)
        self.assertTrue(models.UserAction.objects.filter(id=user_action.id).exists())

    def test_cleanup_dangling_user_actions_with_no_dangling_actions(self):
        """Test cleanup when there are no dangling actions"""

        # Create only valid actions
        models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Valid Action 1",
            description="Valid action",
            urgency="medium",
            content_type=self.user_ct,
            object_id=self.user.id,
        )

        models.UserAction.objects.create(
            user=self.user2,
            action_type="test_action",
            title="Valid Action 2",
            description="Another valid action",
            urgency="low",
            content_type=self.user_ct,
            object_id=self.user2.id,
        )

        self.assertEqual(models.UserAction.objects.count(), 2)

        # Run cleanup
        deleted_count = cleanup_dangling_user_actions()

        # Should not delete any actions
        self.assertEqual(deleted_count, 0)
        self.assertEqual(models.UserAction.objects.count(), 2)


class SignalHandlerTests(TestCase):
    """Test cases for signal handlers"""

    def setUp(self):
        unique_id = str(uuid.uuid4())[:8]
        self.user = User.objects.create_user(
            username=f"signal_testuser_{unique_id}",
            email=f"signal_test_{unique_id}@example.com",
        )

    @patch("waldur_core.user_actions.tasks.cleanup_actions_for_deleted_object.delay")
    def test_schedule_cleanup_for_deleted_object_queues_task(self, mock_task):
        """Test that signal handler properly queues cleanup task"""

        # Mock sender and instance
        class MockSender:
            __name__ = "TestModel"

        class MockInstance:
            pk = 123

        sender = MockSender()
        instance = MockInstance()

        # Call the handler
        schedule_cleanup_for_deleted_object(sender, instance)

        # Should queue the cleanup task
        self.assertTrue(mock_task.called)
        call_args = mock_task.call_args[0]
        self.assertEqual(len(call_args), 2)  # content_type_id, object_id
        self.assertEqual(call_args[1], 123)  # object_id

    @patch("waldur_core.user_actions.tasks.cleanup_actions_for_deleted_object.delay")
    def test_schedule_cleanup_handles_errors_gracefully(self, mock_task):
        """Test that signal handler handles errors without crashing"""

        # Mock task to raise an exception
        mock_task.side_effect = Exception("Celery error")

        class MockSender:
            __name__ = "TestModel"

        class MockInstance:
            pk = 123

        # Should not raise exception
        try:
            schedule_cleanup_for_deleted_object(MockSender(), MockInstance())
        except Exception:
            self.fail("Signal handler should handle errors gracefully")

    @patch("django.db.models.signals.post_delete.connect")
    def test_register_cleanup_handler_registers_signals(self, mock_connect):
        """Test that convenience helper registers signal handlers"""

        # Register handlers for multiple models
        register_cleanup_handler(User, ContentType)

        # Should call connect twice (once for each model)
        self.assertEqual(mock_connect.call_count, 2)

        # Check that correct dispatch_uids are used
        call_args_list = mock_connect.call_args_list
        dispatch_uids = [call[1]["dispatch_uid"] for call in call_args_list]

        self.assertIn("cleanup_user_actions_for_auth.user", dispatch_uids)
        self.assertIn(
            "cleanup_user_actions_for_contenttypes.contenttype", dispatch_uids
        )


class IntegrationTests(TestCase):
    """Integration tests that simulate real deletion scenarios"""

    def setUp(self):
        unique_id = str(uuid.uuid4())[:8]
        self.user = User.objects.create_user(
            username=f"integ_testuser_{unique_id}",
            email=f"integ_test_{unique_id}@example.com",
        )
        self.user2 = User.objects.create_user(
            username=f"integ_testuser2_{unique_id}",
            email=f"integ_test2_{unique_id}@example.com",
        )

    def test_user_deletion_cleanup_workflow(self):
        """Test complete workflow: create user actions, delete user, cleanup dangling actions"""

        user_ct = ContentType.objects.get_for_model(User)

        # Create actions for both users
        action1 = models.UserAction.objects.create(
            user=self.user,
            action_type="user_expiry",
            title="User Expiring Soon",
            description=f"User {self.user.username} expires soon",
            urgency="high",
            content_type=user_ct,
            object_id=self.user.id,
        )

        models.UserAction.objects.create(
            user=self.user2,
            action_type="user_expiry",
            title="Another User Expiring",
            description=f"User {self.user2.username} expires soon",
            urgency="medium",
            content_type=user_ct,
            object_id=self.user2.id,
        )

        # Create action where first user has action about second user
        models.UserAction.objects.create(
            user=self.user,  # first user sees this action
            action_type="review_user",
            title="Review User Account",
            description=f"Review account for {self.user2.username}",
            urgency="low",
            content_type=user_ct,
            object_id=self.user2.id,  # but it's about second user
        )

        self.assertEqual(models.UserAction.objects.count(), 3)

        # Simulate user2 deletion by capturing their ID first
        user2_id = self.user2.id
        self.user2.delete()

        # Now run targeted cleanup for the deleted user
        deleted_count = cleanup_actions_for_deleted_object(user_ct.id, user2_id)

        # Should remove actions that pointed to deleted user (action2 and action3)
        self.assertEqual(deleted_count, 2)
        self.assertEqual(models.UserAction.objects.count(), 1)

        # Only action1 should remain (about existing user, for existing user)
        remaining_action = models.UserAction.objects.first()
        self.assertEqual(remaining_action.id, action1.id)
        self.assertEqual(remaining_action.object_id, self.user.id)

    def test_bulk_cleanup_with_mixed_valid_invalid_actions(self):
        """Test cleanup with mix of valid and invalid actions across multiple content types"""

        user_ct = ContentType.objects.get_for_model(User)
        content_type_ct = ContentType.objects.get_for_model(ContentType)

        # Create valid actions
        valid_actions = []
        for i in range(3):
            action = models.UserAction.objects.create(
                user=self.user,
                action_type=f"test_action_{i}",
                title=f"Valid Action {i}",
                description="Valid action",
                urgency="medium",
                content_type=user_ct,
                object_id=self.user.id,
            )
            valid_actions.append(action)

        # Create invalid actions pointing to non-existent objects
        invalid_actions = []
        for i in range(2):
            action = models.UserAction.objects.create(
                user=self.user,
                action_type=f"invalid_action_{i}",
                title=f"Invalid Action {i}",
                description="Invalid action",
                urgency="high",
                content_type=user_ct,
                object_id=999000 + i,  # Non-existent IDs
            )
            invalid_actions.append(action)

        # Create invalid action with different content type
        invalid_ct_action = models.UserAction.objects.create(
            user=self.user,
            action_type="invalid_ct_action",
            title="Invalid ContentType Action",
            description="Points to non-existent ContentType",
            urgency="high",
            content_type=content_type_ct,
            object_id=888888,  # Non-existent ContentType ID
        )
        invalid_actions.append(invalid_ct_action)

        # Total: 3 valid + 3 invalid = 6 actions
        self.assertEqual(models.UserAction.objects.count(), 6)

        # Run periodic cleanup
        deleted_count = cleanup_dangling_user_actions()

        # Should delete all 3 invalid actions
        self.assertEqual(deleted_count, 3)
        self.assertEqual(models.UserAction.objects.count(), 3)

        # All valid actions should remain
        for action in valid_actions:
            self.assertTrue(models.UserAction.objects.filter(id=action.id).exists())

        # All invalid actions should be gone
        for action in invalid_actions:
            self.assertFalse(models.UserAction.objects.filter(id=action.id).exists())
