from unittest import mock

from rest_framework import test

from waldur_core.logging import handlers, models
from waldur_core.logging.enums import EventType
from waldur_core.logging.event_logger import emit
from waldur_core.logging.tests import factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


class ProcessHookHandlerTest(test.APITestCase):
    """Test the process_hook signal handler that triggers event processing."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.admin = self.fixture.admin

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_triggers_event_processing_when_active_hook_matches(
        self, mock_process_event
    ):
        """Test that event processing is triggered when an active hook matches."""
        event = factories.EventFactory(event_type="test_event")
        factories.FeedFactory(event=event, scope=self.project)

        # Create an active hook that matches the event
        models.EmailHook.objects.create(
            user=self.admin,
            email=self.admin.email,
            event_types=["test_event"],
            is_active=True,
        )

        # Trigger the signal handler; captureOnCommitCallbacks flushes
        # the transaction.on_commit callback used inside process_hook
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)

        # Should trigger event processing
        mock_process_event.assert_called_once_with(event.pk)

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_triggers_event_processing_when_system_notification_matches(
        self, mock_process_event
    ):
        """Test that event processing is triggered when a system notification matches."""
        event = factories.EventFactory(event_type="test_event")
        factories.FeedFactory(event=event, scope=self.project)

        # Create a system notification that matches the event type
        factories.SystemNotificationFactory(
            event_types=["test_event"],
            roles=["admin"],
        )

        # Trigger the signal handler
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)

        # Should trigger event processing
        mock_process_event.assert_called_once_with(event.pk)

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_triggers_event_processing_when_both_hook_and_notification_match(
        self, mock_process_event
    ):
        """Test that event processing is triggered when both hook and notification match."""
        event = factories.EventFactory(event_type="test_event")
        factories.FeedFactory(event=event, scope=self.project)

        # Create both a hook and a system notification
        models.EmailHook.objects.create(
            user=self.admin,
            email=self.admin.email,
            event_types=["test_event"],
            is_active=True,
        )
        factories.SystemNotificationFactory(
            event_types=["test_event"],
            roles=["admin"],
        )

        # Trigger the signal handler
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)

        # Should trigger event processing once
        mock_process_event.assert_called_once_with(event.pk)

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_does_not_trigger_when_no_matching_hooks_or_notifications(
        self, mock_process_event
    ):
        """Test that event processing is not triggered when nothing matches."""
        event = factories.EventFactory(event_type="test_event")
        factories.FeedFactory(event=event, scope=self.project)

        # Create a hook with different event type
        models.EmailHook.objects.create(
            user=self.admin,
            email=self.admin.email,
            event_types=["other_event"],
            is_active=True,
        )

        # Create a system notification with different event type
        factories.SystemNotificationFactory(
            event_types=["another_event"],
            roles=["admin"],
        )

        # Trigger the signal handler
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)

        # Should NOT trigger event processing
        mock_process_event.assert_not_called()

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_does_not_trigger_when_only_inactive_hooks_exist(self, mock_process_event):
        """Test that event processing is not triggered when only inactive hooks exist."""
        event = factories.EventFactory(event_type="test_event")
        factories.FeedFactory(event=event, scope=self.project)

        # Create an inactive hook
        models.EmailHook.objects.create(
            user=self.admin,
            email=self.admin.email,
            event_types=["test_event"],
            is_active=False,
        )

        # Trigger the signal handler
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)

        # Should NOT trigger event processing
        mock_process_event.assert_not_called()

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_triggers_when_inactive_hook_but_system_notification_matches(
        self, mock_process_event
    ):
        """Test that event processing is triggered by system notification even if hook is inactive."""
        event = factories.EventFactory(event_type="test_event")
        factories.FeedFactory(event=event, scope=self.project)

        # Create an inactive hook
        models.EmailHook.objects.create(
            user=self.admin,
            email=self.admin.email,
            event_types=["test_event"],
            is_active=False,
        )

        # Create a system notification
        factories.SystemNotificationFactory(
            event_types=["test_event"],
            roles=["admin"],
        )

        # Trigger the signal handler
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)

        # Should trigger event processing because of system notification
        mock_process_event.assert_called_once_with(event.pk)

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_system_notification_with_multiple_event_types(self, mock_process_event):
        """Test that system notification matches when event type is in the list."""
        event = factories.EventFactory(event_type="test_event")
        factories.FeedFactory(event=event, scope=self.project)

        # Create a system notification with multiple event types
        factories.SystemNotificationFactory(
            event_types=["other_event", "test_event", "third_event"],
            roles=["admin"],
        )

        # Trigger the signal handler
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)

        # Should trigger event processing
        mock_process_event.assert_called_once_with(event.pk)

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_multiple_system_notifications_matching(self, mock_process_event):
        """Test that event processing is triggered when multiple system notifications match."""
        event = factories.EventFactory(event_type="test_event")
        factories.FeedFactory(event=event, scope=self.project)

        # Create multiple system notifications
        factories.SystemNotificationFactory(
            event_types=["test_event"],
            roles=["admin"],
        )
        factories.SystemNotificationFactory(
            event_types=["test_event", "other_event"],
            roles=["manager"],
        )

        # Trigger the signal handler
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)

        # Should trigger event processing
        mock_process_event.assert_called_once_with(event.pk)

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_does_not_trigger_for_user_without_permissions(self, mock_process_event):
        """Test that hook without permissions doesn't trigger processing alone."""
        event = factories.EventFactory(event_type="test_event")
        factories.FeedFactory(event=event, scope=self.project)

        # Create a hook for user without permissions
        user_without_permission = structure_factories.UserFactory()
        models.EmailHook.objects.create(
            user=user_without_permission,
            email=user_without_permission.email,
            event_types=["test_event"],
            is_active=True,
        )

        # Trigger the signal handler
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)

        # Should NOT trigger event processing (no matching hooks with permissions)
        mock_process_event.assert_not_called()

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_event_processing_uses_transaction_on_commit(self, mock_process_event):
        """Test that event processing is scheduled to run after transaction commits."""
        event = factories.EventFactory(event_type="test_event")
        factories.FeedFactory(event=event, scope=self.project)

        factories.SystemNotificationFactory(
            event_types=["test_event"],
            roles=["admin"],
        )

        # Before triggering the handler, no processing should be scheduled
        mock_process_event.assert_not_called()

        # Trigger the signal handler
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)

        # The mock is called after on_commit callbacks are flushed
        mock_process_event.assert_called_once_with(event.pk)


class EmitWebhookDispatchRaceConditionTest(test.APITestCase):
    """Regression test for webhook dispatch race condition.

    The emit() function creates the Event first (which fires the post_save
    signal and calls process_hook), and only then creates Feed objects that
    link the event to its scopes (project, customer). Because process_hook
    runs during Event creation, get_matching_hooks() finds no Feed records
    yet, so check_event() always returns False. Without a SystemNotification
    fallback, the Celery task is never enqueued and webhooks never fire.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.admin = self.fixture.admin

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_webhook_task_is_enqueued_when_event_emitted_with_matching_hook(
        self, mock_process_event
    ):
        """Webhook task should be enqueued when emit() is called with matching hook.

        This tests the REAL emit() flow rather than calling process_hook()
        directly with pre-existing Feed objects (which masks the race condition).
        """
        # Create a webhook matching the event type, owned by a user with project access
        models.WebHook.objects.create(
            user=self.admin,
            destination_url="https://example.com/webhook",
            event_types=["project_creation_succeeded"],
            is_active=True,
        )

        # Record event count before the test emit
        event_count_before = models.Event.objects.filter(
            event_type="project_creation_succeeded"
        ).count()

        # Emit an event through the real flow; captureOnCommitCallbacks
        # ensures the transaction.on_commit callback in process_hook fires
        with self.captureOnCommitCallbacks(execute=True):
            emit(
                "Project {project_name} has been created.",
                event_type=EventType.PROJECT_CREATION_SUCCEEDED,
                event_context={"project": self.project},
                scopes=[self.project, self.project.customer],
            )

        # Verify the event and feed objects were created
        event_count_after = models.Event.objects.filter(
            event_type="project_creation_succeeded"
        ).count()
        self.assertEqual(event_count_after, event_count_before + 1)
        event = (
            models.Event.objects.filter(event_type="project_creation_succeeded")
            .order_by("-id")
            .first()
        )
        self.assertTrue(models.Feed.objects.filter(event=event).exists())

        # The Celery task should have been enqueued
        mock_process_event.assert_called_once_with(event.pk)

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_existing_tests_mask_the_race_condition(self, mock_process_event):
        """Demonstrates that calling process_hook() after Feed creation works,
        which is why existing tests pass despite the bug in emit().
        """
        # Create the hook
        models.WebHook.objects.create(
            user=self.admin,
            destination_url="https://example.com/webhook",
            event_types=["project_creation_succeeded"],
            is_active=True,
        )

        # Create event and feed objects FIRST (like existing tests do)
        event = factories.EventFactory(event_type="project_creation_succeeded")
        factories.FeedFactory(event=event, scope=self.project)

        # Then call process_hook — Feed objects already exist, so it works
        with self.captureOnCommitCallbacks(execute=True):
            handlers.process_hook(sender=models.Event, instance=event)
        mock_process_event.assert_called_once_with(event.pk)

    @mock.patch("waldur_core.logging.handlers.tasks.process_event.delay")
    def test_system_notification_workaround_bypasses_race_condition(
        self, mock_process_event
    ):
        """SystemNotification check doesn't depend on Feed objects, so it
        can trigger task dispatch even when the webhook-only path fails.
        """
        # Create a webhook (would fail due to race condition alone)
        models.WebHook.objects.create(
            user=self.admin,
            destination_url="https://example.com/webhook",
            event_types=["project_creation_succeeded"],
            is_active=True,
        )

        # Also create a SystemNotification (bypasses the Feed race condition)
        factories.SystemNotificationFactory(
            event_types=["project_creation_succeeded"],
            roles=["admin"],
        )

        with self.captureOnCommitCallbacks(execute=True):
            emit(
                "Project {project_name} has been created.",
                event_type=EventType.PROJECT_CREATION_SUCCEEDED,
                event_context={"project": self.project},
                scopes=[self.project, self.project.customer],
            )

        event = (
            models.Event.objects.filter(event_type="project_creation_succeeded")
            .order_by("-id")
            .first()
        )
        # Task IS enqueued because SystemNotification condition is True
        mock_process_event.assert_called_once_with(event.pk)
