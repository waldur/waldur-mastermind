"""Tests for cross-organization notification isolation.

Validates that users only receive hook notifications (email/webhook)
for events in organizations and projects they have access to.

This addresses a reported security concern where creating an EmailHook
could result in receiving notifications for ALL Waldur instance events.
"""

from unittest import mock

from django.core import mail
from rest_framework import test

from waldur_core.logging import models as logging_models
from waldur_core.logging.tasks import process_event
from waldur_core.logging.tests.factories import EventFactory, SystemNotificationFactory
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories


class CrossOrgNotificationIsolationTest(test.APITestCase):
    """Verify that email hooks only deliver events the hook owner has access to."""

    def setUp(self):
        # Organization A with owner
        self.customer_a = structure_factories.CustomerFactory(name="Org A")
        self.owner_a = structure_factories.UserFactory()
        self.customer_a.add_user(self.owner_a, CustomerRole.OWNER)

        # Organization B with owner
        self.customer_b = structure_factories.CustomerFactory(name="Org B")
        self.owner_b = structure_factories.UserFactory()
        self.customer_b.add_user(self.owner_b, CustomerRole.OWNER)

        # Unaffiliated user
        self.unaffiliated_user = structure_factories.UserFactory()

        self.event_type = "customer_update_succeeded"

    def _create_event_with_feed(self, scope):
        event = EventFactory(event_type=self.event_type)
        logging_models.Feed.objects.create(scope=scope, event=event)
        return event

    def test_user_does_not_receive_notification_for_other_org(self):
        """Owner of Org A must NOT receive notifications for events in Org B."""
        logging_models.EmailHook.objects.create(
            user=self.owner_a,
            email=self.owner_a.email,
            event_types=[self.event_type],
        )

        event = self._create_event_with_feed(self.customer_b)
        process_event(event.id)

        self.assertEqual(len(mail.outbox), 0)

    def test_user_receives_notification_for_own_org(self):
        """Owner of Org A must receive notifications for events in Org A."""
        logging_models.EmailHook.objects.create(
            user=self.owner_a,
            email=self.owner_a.email,
            event_types=[self.event_type],
        )

        event = self._create_event_with_feed(self.customer_a)
        process_event(event.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.owner_a.email, mail.outbox[0].to)

    def test_unaffiliated_user_does_not_receive_any_notification(self):
        """User with no org membership must NOT receive any notifications."""
        logging_models.EmailHook.objects.create(
            user=self.unaffiliated_user,
            email=self.unaffiliated_user.email,
            event_types=[self.event_type],
        )

        event = self._create_event_with_feed(self.customer_a)
        process_event(event.id)

        self.assertEqual(len(mail.outbox), 0)

    def test_only_correct_owner_notified_when_both_have_hooks(self):
        """When owners of two orgs both have hooks, only the relevant one is notified."""
        logging_models.EmailHook.objects.create(
            user=self.owner_a,
            email=self.owner_a.email,
            event_types=[self.event_type],
        )
        logging_models.EmailHook.objects.create(
            user=self.owner_b,
            email=self.owner_b.email,
            event_types=[self.event_type],
        )

        event = self._create_event_with_feed(self.customer_a)
        process_event(event.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.owner_a.email, mail.outbox[0].to)

    @mock.patch("requests.post")
    def test_webhook_not_sent_to_user_without_access(self, requests_post):
        """WebHook must NOT fire for events the hook owner has no access to."""
        logging_models.WebHook.objects.create(
            user=self.owner_a,
            destination_url="http://example.com/hook",
            event_types=[self.event_type],
        )

        event = self._create_event_with_feed(self.customer_b)
        process_event(event.id)

        requests_post.assert_not_called()

    @mock.patch("requests.post")
    def test_webhook_sent_to_user_with_access(self, requests_post):
        """WebHook must fire for events the hook owner has access to."""
        logging_models.WebHook.objects.create(
            user=self.owner_a,
            destination_url="http://example.com/hook",
            event_types=[self.event_type],
        )

        event = self._create_event_with_feed(self.customer_a)
        process_event(event.id)

        requests_post.assert_called_once()


class CrossOrgNotificationIsolationWithSystemNotificationTest(test.APITestCase):
    """Verify isolation when SystemNotification expands hook event types."""

    def setUp(self):
        self.customer_a = structure_factories.CustomerFactory(name="Org A")
        self.project_a = structure_factories.ProjectFactory(customer=self.customer_a)
        self.admin_a = structure_factories.UserFactory()
        self.project_a.add_user(self.admin_a, ProjectRole.ADMIN)

        self.customer_b = structure_factories.CustomerFactory(name="Org B")
        self.project_b = structure_factories.ProjectFactory(customer=self.customer_b)
        self.admin_b = structure_factories.UserFactory()
        self.project_b.add_user(self.admin_b, ProjectRole.ADMIN)

    def test_system_notification_does_not_leak_to_other_org(self):
        """SystemNotification must only notify users with roles on the event's project."""
        SystemNotificationFactory(
            event_types=["project_update_succeeded"],
            roles=["admin"],
        )

        event = EventFactory(event_type="project_update_succeeded")
        logging_models.Feed.objects.create(scope=self.project_b, event=event)

        process_event(event.id)

        # Only admin_b should get notified (admin of project_b)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.admin_b.email, mail.outbox[0].to)
        self.assertNotIn(self.admin_a.email, mail.outbox[0].to)

    def test_user_hook_with_system_notification_expansion_does_not_leak(self):
        """Even when SystemNotification expands event types on a user hook,
        events must only be delivered if the hook owner has access."""
        # SystemNotification for EmailHook adds project_update_succeeded
        SystemNotificationFactory(
            event_types=["project_update_succeeded"],
            roles=["admin"],
        )

        # admin_a creates a hook with a different event type,
        # but SystemNotification expands all_event_types to include project_update_succeeded
        hook = logging_models.EmailHook.objects.create(
            user=self.admin_a,
            email=self.admin_a.email,
            event_types=["some_other_event"],
        )
        self.assertIn("project_update_succeeded", hook.all_event_types)

        # Event happens in project_b
        event = EventFactory(event_type="project_update_succeeded")
        logging_models.Feed.objects.create(scope=self.project_b, event=event)

        process_event(event.id)

        # admin_a must NOT receive the email via their hook (no access to project_b).
        # admin_b should receive via SystemNotification.
        recipients = [msg.to[0] for msg in mail.outbox]
        self.assertNotIn(self.admin_a.email, recipients)
        self.assertIn(self.admin_b.email, recipients)


class StaffUserNotificationTest(test.APITestCase):
    """Verify that staff/support users receive all notifications (by design)."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.support_user = structure_factories.UserFactory(is_support=True)
        self.event_type = "customer_update_succeeded"

    def test_staff_user_receives_notification_for_any_org(self):
        """Staff users should receive notifications for any organization."""
        logging_models.EmailHook.objects.create(
            user=self.staff_user,
            email=self.staff_user.email,
            event_types=[self.event_type],
        )

        event = EventFactory(event_type=self.event_type)
        logging_models.Feed.objects.create(scope=self.customer, event=event)
        process_event(event.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.staff_user.email, mail.outbox[0].to)

    def test_support_user_receives_notification_for_any_org(self):
        """Support users should receive notifications for any organization."""
        logging_models.EmailHook.objects.create(
            user=self.support_user,
            email=self.support_user.email,
            event_types=[self.event_type],
        )

        event = EventFactory(event_type=self.event_type)
        logging_models.Feed.objects.create(scope=self.customer, event=event)
        process_event(event.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.support_user.email, mail.outbox[0].to)
