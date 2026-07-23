from django.core import mail
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import tasks
from waldur_mastermind.support.tests import factories


class ProviderNotificationTest(TestCase):
    """The provider helpdesk notifications are registered and actually send.

    They were previously dead (no registry entry), so broadcast_mail returned
    early. These tests guard that the registry + templates are wired up.
    """

    def setUp(self):
        self.helpdesk = factories.ProviderHelpdeskFactory(
            notification_email="provider@example.com",
            notify_on_new_ticket=True,
        )
        self.issue = factories.IssueFactory(backend_id="WLD-700")

    def test_new_ticket_notification_sends_when_enabled(self):
        structure_factories.NotificationFactory(
            key="support.provider_new_ticket", enabled=True
        )
        child = factories.IssueFactory(
            parent_issue=self.issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-701",
        )

        tasks.notify_provider_new_ticket(child.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.helpdesk.notification_email, mail.outbox[0].to)

    def test_new_ticket_notification_silent_when_not_registered(self):
        # No NotificationFactory row -> broadcast_mail returns early, no mail.
        child = factories.IssueFactory(
            parent_issue=self.issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-702",
        )

        tasks.notify_provider_new_ticket(child.id)

        self.assertEqual(len(mail.outbox), 0)

    def test_ticket_withdrawn_notification_sends_when_enabled(self):
        structure_factories.NotificationFactory(
            key="support.provider_ticket_withdrawn", enabled=True
        )

        tasks.notify_provider_ticket_withdrawn(self.issue.id, self.helpdesk.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.helpdesk.notification_email, mail.outbox[0].to)
