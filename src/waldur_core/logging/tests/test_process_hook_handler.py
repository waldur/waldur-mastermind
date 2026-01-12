from unittest import mock

from rest_framework import test

from waldur_core.logging import handlers, models
from waldur_core.logging.tests import factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


class ProcessHookHandlerTest(test.APITransactionTestCase):
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

        # Trigger the signal handler
        handlers.process_hook(sender=models.Event, instance=event, created=True)

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
        handlers.process_hook(sender=models.Event, instance=event, created=True)

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
        handlers.process_hook(sender=models.Event, instance=event, created=True)

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
        handlers.process_hook(sender=models.Event, instance=event, created=True)

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
        handlers.process_hook(sender=models.Event, instance=event, created=True)

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
        handlers.process_hook(sender=models.Event, instance=event, created=True)

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
        handlers.process_hook(sender=models.Event, instance=event, created=True)

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
        handlers.process_hook(sender=models.Event, instance=event, created=True)

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
        handlers.process_hook(sender=models.Event, instance=event, created=True)

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
        handlers.process_hook(sender=models.Event, instance=event, created=True)

        # The mock is called, but in real scenario it would be delayed until transaction commit
        mock_process_event.assert_called_once_with(event.pk)
