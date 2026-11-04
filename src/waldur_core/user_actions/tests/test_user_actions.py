import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, OperationalError
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.configuration.testing import override_config
from waldur_core.user_actions import models, providers, tasks
from waldur_core.user_actions.handlers import (
    register_cleanup_handler,
    schedule_cleanup_for_deleted_object,
)
from waldur_core.user_actions.providers import (
    ActionCategory,
    ActionSeverity,
    BaseActionProvider,
    CorrectiveAction,
)
from waldur_core.user_actions.tasks import (
    UserActionsTaskError,
    cleanup_actions_for_deleted_object,
    cleanup_dangling_user_actions,
)

User = get_user_model()


class FakeActionProvider(BaseActionProvider):
    """Action provider used by the tests"""

    action_type = "test_action"
    display_name = "Test Actions"

    def get_actions_for_user(self, user):
        return [
            {
                "title": f"Test action for {user.username}",
                "description": "A test action",
                "urgency": "medium",
                "related_object": user,
            }
        ]

    def get_affected_users(self):
        return User.objects.all()

    def get_corrective_actions(self, user, obj):
        return [
            CorrectiveAction(
                label="View User",
                category=ActionCategory.VIEW,
                severity=ActionSeverity.SAFE,
                route_name="users.details",
                route_params={"user_uuid": obj.uuid.hex},
            ),
            CorrectiveAction(
                label="Send Email",
                category=ActionCategory.CONTACT,
                severity=ActionSeverity.SAFE,
                metadata={"email": obj.email},
            ),
        ]


class FailingActionProvider(FakeActionProvider):
    """A provider whose every query breaks, as a provider regression would."""

    action_type = "failing_action"
    display_name = "Failing Actions"

    def get_actions_for_user(self, user):
        raise TypeError("provider is broken")

    def get_affected_users(self):
        raise TypeError("provider is broken")


def use_only_fake_provider(test_case, *extra_providers):
    """Register only FakeActionProvider for the duration of the test.

    The registry is process-wide and the apps fill it in ready(), so it is
    emptied in place and restored afterwards: clearing it for good would take
    the real providers away from every test that runs later in the process.
    """
    registry = patch.dict(providers._providers, clear=True)
    registry.start()
    test_case.addCleanup(registry.stop)
    providers.register_provider(FakeActionProvider)
    for provider_class in extra_providers:
        providers.register_provider(provider_class)


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
        use_only_fake_provider(self)

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
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["title"], "Test Action")

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
        # One action per user, type and object: this one is about someone else.
        subject = User.objects.create_user(username="subject")
        models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="High Priority Action",
            description="A high priority action",
            urgency="high",
            content_type=ContentType.objects.get_for_model(User),
            object_id=subject.id,
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
        # One action per user, type and object: this one is about someone else.
        subject = User.objects.create_user(username="subject")
        models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Low Priority Action",
            description="A low priority action",
            urgency="low",
            content_type=ContentType.objects.get_for_model(User),
            object_id=subject.id,
        )

        response = self.client.get("/api/user-actions/?urgency=medium")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["urgency"], "medium")

    def test_filter_silenced_actions(self):
        """Test filtering silenced actions"""
        self.action.silence()

        # Default should exclude silenced
        response = self.client.get("/api/user-actions/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        # Explicit include should show silenced
        response = self.client.get("/api/user-actions/?include_silenced=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

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
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["title"], "Test Action")

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
        use_only_fake_provider(self)

    @patch("waldur_core.user_actions.tasks.update_user_actions_for_provider.delay")
    def test_update_actions_for_provider(self, mock_task):
        """Test updating actions for a provider"""
        from waldur_core.user_actions import tasks

        # Create a provider record
        provider_record = models.UserActionProvider.objects.create(
            action_type="test_action",
            app_name="user_actions",
            provider_class="FakeActionProvider",
        )

        tasks.update_actions_for_provider("test_action")

        # Should queue a task for each user
        self.assertTrue(mock_task.called)

        # Check provider status was updated
        provider_record.refresh_from_db()
        self.assertIn("COMPLETED", provider_record.last_execution_status)

    def test_update_user_actions_for_provider(self):
        """Test updating user actions for a specific provider"""
        from waldur_core.user_actions import tasks

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
        from waldur_core.user_actions import tasks

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


class TaskFailureTests(TestCase):
    """A broken provider must fail its task visibly, not report success."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="failing_user", email="failing@example.com"
        )
        use_only_fake_provider(self, FailingActionProvider)

    def assertLoggedException(self, logs):
        self.assertTrue(
            any(record.exc_info for record in logs.records),
            "expected a log record carrying the traceback",
        )

    def test_update_user_actions_for_provider_raises(self):
        with self.assertLogs("waldur_core.user_actions.tasks", "ERROR") as logs:
            with self.assertRaises(TypeError):
                tasks.update_user_actions_for_provider(self.user.id, "failing_action")

        self.assertLoggedException(logs)
        self.assertEqual(models.UserAction.objects.count(), 0)

    def test_update_user_actions_for_provider_skips_missing_user(self):
        tasks.update_user_actions_for_provider(999999, "failing_action")

    def test_cleanup_stale_actions_raises_and_keeps_actions(self):
        action = models.UserAction.objects.create(
            user=self.user,
            action_type="failing_action",
            title="Existing action",
            urgency="low",
            content_type=ContentType.objects.get_for_model(User),
            object_id=self.user.id,
        )

        with self.assertLogs("waldur_core.user_actions.tasks", "ERROR") as logs:
            with self.assertRaises(TypeError):
                tasks.cleanup_stale_actions(self.user.id, "failing_action")

        self.assertLoggedException(logs)
        self.assertTrue(models.UserAction.objects.filter(id=action.id).exists())

    def test_cleanup_stale_actions_skips_missing_user(self):
        tasks.cleanup_stale_actions(999999, "failing_action")

    @patch("waldur_core.user_actions.tasks.update_user_actions_for_provider.delay")
    def test_update_actions_for_provider_records_and_raises(self, delay):
        with self.assertLogs("waldur_core.user_actions.tasks", "ERROR") as logs:
            with self.assertRaises(TypeError):
                tasks.update_actions_for_provider("failing_action")

        self.assertLoggedException(logs)
        delay.assert_not_called()
        record = models.UserActionProvider.objects.get(action_type="failing_action")
        self.assertTrue(record.last_execution_status.startswith("ERROR: "))

    @patch("waldur_core.user_actions.tasks.update_user_actions_for_provider.delay")
    def test_update_actions_for_provider_queues_remaining_users(self, delay):
        other_user = User.objects.create_user(
            username="other_user", email="other@example.com"
        )

        def queue(user_id, action_type):
            if user_id == self.user.id:
                raise ConnectionError("broker down")

        delay.side_effect = queue

        with self.assertLogs("waldur_core.user_actions.tasks", "ERROR") as logs:
            with self.assertRaises(UserActionsTaskError):
                tasks.update_actions_for_provider("test_action")

        self.assertLoggedException(logs)
        queued = {call.args[0] for call in delay.call_args_list}
        self.assertIn(other_user.id, queued)
        record = models.UserActionProvider.objects.get(action_type="test_action")
        self.assertIn("1 errors", record.last_execution_status)

    @override_config(USER_ACTIONS_ENABLED=True)
    @patch("waldur_core.user_actions.tasks.update_actions_for_provider.delay")
    def test_update_user_actions_queues_remaining_providers(self, delay):
        def queue(action_type):
            if action_type == "failing_action":
                raise ConnectionError("broker down")

        delay.side_effect = queue

        with self.assertLogs("waldur_core.user_actions.tasks", "ERROR") as logs:
            with self.assertRaises(UserActionsTaskError):
                tasks.update_user_actions()

        self.assertLoggedException(logs)
        queued = {call.args[0] for call in delay.call_args_list}
        self.assertEqual(queued, {"test_action", "failing_action"})

    @override_config(USER_ACTIONS_ENABLED=True)
    @patch("waldur_core.user_actions.tasks.update_user_actions_for_provider.delay")
    def test_update_user_actions_for_one_user_queues_remaining_providers(self, delay):
        def queue(user_id, action_type):
            if action_type == "failing_action":
                raise ConnectionError("broker down")

        delay.side_effect = queue

        with self.assertLogs("waldur_core.user_actions.tasks", "ERROR") as logs:
            with self.assertRaises(UserActionsTaskError):
                tasks.update_user_actions(user_uuid=self.user.uuid.hex)

        self.assertLoggedException(logs)
        queued = {call.args[1] for call in delay.call_args_list}
        self.assertEqual(queued, {"test_action", "failing_action"})

    def test_cleanup_actions_for_deleted_object_raises(self):
        user_ct = ContentType.objects.get_for_model(User)
        with patch.object(
            models.UserAction.objects, "filter", side_effect=DatabaseError("boom")
        ):
            with self.assertLogs("waldur_core.user_actions.tasks", "ERROR") as logs:
                with self.assertRaises(DatabaseError):
                    cleanup_actions_for_deleted_object(user_ct.id, 123)

        self.assertLoggedException(logs)

    def test_cleanup_dangling_user_actions_continues_and_raises(self):
        user_ct = ContentType.objects.get_for_model(User)
        other_user = User.objects.create_user(
            username="unreadable_user", email="unreadable@example.com"
        )
        unreadable = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Unreadable",
            urgency="low",
            content_type=user_ct,
            object_id=other_user.id,
        )
        dangling = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Dangling",
            urgency="low",
            content_type=user_ct,
            object_id=999999,
        )
        original = ContentType.get_object_for_this_type

        def flaky(content_type, using=None, **kwargs):
            if kwargs.get("pk") == other_user.id:
                raise DatabaseError("boom")
            return original(content_type, using=using, **kwargs)

        with patch.object(ContentType, "get_object_for_this_type", flaky):
            with self.assertLogs("waldur_core.user_actions.tasks", "ERROR") as logs:
                with self.assertRaises(UserActionsTaskError):
                    cleanup_dangling_user_actions()

        self.assertLoggedException(logs)
        # The action that could not be checked is kept, not deleted on suspicion;
        # the rest of the sweep still ran.
        self.assertTrue(models.UserAction.objects.filter(id=unreadable.id).exists())
        self.assertFalse(models.UserAction.objects.filter(id=dangling.id).exists())

    def test_cleanup_dangling_user_actions_stops_when_the_database_is_unavailable(
        self,
    ):
        user_ct = ContentType.objects.get_for_model(User)
        for object_id in (999998, 999999):
            models.UserAction.objects.create(
                user=self.user,
                action_type="test_action",
                title=f"Action {object_id}",
                urgency="low",
                content_type=user_ct,
                object_id=object_id,
            )
        calls = []

        def unavailable(content_type, using=None, **kwargs):
            calls.append(kwargs)
            raise OperationalError("connection lost")

        with patch.object(ContentType, "get_object_for_this_type", unavailable):
            with self.assertRaises(OperationalError):
                cleanup_dangling_user_actions()

        # One failed lookup, not one per remaining action; nothing is deleted.
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            models.UserAction.objects.filter(title__startswith="Action ").count(), 2
        )

    def test_update_actions_for_provider_truncates_a_long_error_status(self):
        long_message = "x" * 200

        with patch.object(
            FailingActionProvider,
            "get_affected_users",
            side_effect=TypeError(long_message),
        ):
            with self.assertLogs("waldur_core.user_actions.tasks", "ERROR"):
                with self.assertRaises(TypeError):
                    tasks.update_actions_for_provider("failing_action")

        record = models.UserActionProvider.objects.get(action_type="failing_action")
        max_length = models.UserActionProvider._meta.get_field(
            "last_execution_status"
        ).max_length
        self.assertEqual(len(record.last_execution_status), max_length)
        self.assertTrue(record.last_execution_status.startswith("ERROR: x"))

    def test_cleanup_dangling_user_actions_removes_actions_of_removed_model(self):
        gone_ct = ContentType.objects.create(app_label="removed_app", model="gone")
        action = models.UserAction.objects.create(
            user=self.user,
            action_type="test_action",
            title="Points at a removed model",
            urgency="low",
            content_type=gone_ct,
            object_id=1,
        )

        self.assertEqual(cleanup_dangling_user_actions(), 1)
        self.assertFalse(models.UserAction.objects.filter(id=action.id).exists())


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

        # The sender is a real model: the handler looks up its content type.
        class MockInstance:
            pk = 123

        # Call the handler
        schedule_cleanup_for_deleted_object(User, MockInstance())

        # Should queue the cleanup task
        mock_task.assert_called_once_with(
            ContentType.objects.get_for_model(User).id,  # content_type_id
            123,  # object_id
        )

    @patch("waldur_core.user_actions.tasks.cleanup_actions_for_deleted_object.delay")
    def test_schedule_cleanup_logs_errors_without_breaking_delete(self, mock_task):
        """A broker failure is logged with its traceback, not raised."""
        mock_task.side_effect = Exception("Celery error")

        class MockInstance:
            pk = 123

        with self.assertLogs("waldur_core.user_actions.handlers", "ERROR") as logs:
            schedule_cleanup_for_deleted_object(User, MockInstance())

        self.assertTrue(mock_task.called)
        self.assertEqual(len(logs.records), 1)
        self.assertIsNotNone(logs.records[0].exc_info)

    @patch("waldur_core.user_actions.tasks.cleanup_actions_for_deleted_object.delay")
    def test_schedule_cleanup_waits_for_commit(self, mock_task):
        """A rolled-back delete must not queue the cleanup of its actions."""

        class MockInstance:
            pk = 123

        instance = MockInstance()
        with patch(
            "waldur_core.user_actions.handlers.transaction.on_commit"
        ) as on_commit:
            schedule_cleanup_for_deleted_object(User, instance, using="default")
            # Deletion clears the pk; the handler must have read it already.
            instance.pk = None

        mock_task.assert_not_called()
        on_commit.assert_called_once()
        self.assertEqual(on_commit.call_args.kwargs, {"using": "default"})

        callback = on_commit.call_args.args[0]
        callback()

        mock_task.assert_called_once_with(
            ContentType.objects.get_for_model(User).id, 123
        )

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

        self.assertIn("cleanup_user_actions_for_core.user", dispatch_uids)
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

        # user2's own action goes with them (UserAction.user cascades); the one
        # about them, owned by user1, is left dangling.
        self.assertEqual(models.UserAction.objects.count(), 2)

        # Now run targeted cleanup for the deleted user
        deleted_count = cleanup_actions_for_deleted_object(user_ct.id, user2_id)

        # Should remove the action that pointed to the deleted user (action3)
        self.assertEqual(deleted_count, 1)
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
