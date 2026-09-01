from constance.test.unittest import override_config
from django.core import mail
from django.test import TestCase, override_settings

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models, tasks
from waldur_mastermind.support.backend.basic import BasicBackend
from waldur_mastermind.support.tests import factories

# `base.BaseTest` mocks `get_active_backend` wholesale and leaves `backend_name`
# as None, which defeats the backend gate under test here, so these cases drive
# the real backend instead. Auto-assign is pinned off so it cannot perturb the
# saves the handler keys on.
BASIC = override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)

NOTIFICATION_KEY = "support.notification_issue_created"


def arm_notification(enabled=True):
    return structure_factories.NotificationFactory(
        key=NOTIFICATION_KEY, enabled=enabled
    )


class StaffNewIssueNotificationTaskTest(TestCase):
    """The task itself: who it mails, and when it stays quiet."""

    def setUp(self):
        self.issue = factories.IssueFactory(backend_id="WLD-900", key="WLD-900")
        self.staff = structure_factories.UserFactory(
            is_staff=True, email="staff@example.com"
        )
        self.support = structure_factories.UserFactory(
            is_support=True, email="support@example.com"
        )

    def recipients(self):
        return {address for message in mail.outbox for address in message.to}

    def test_staff_and_support_are_notified(self):
        arm_notification()

        tasks.notify_staff_new_issue(self.issue.id)

        self.assertEqual(
            self.recipients(), {"staff@example.com", "support@example.com"}
        )

    def test_silent_when_the_notification_is_not_registered(self):
        # No Notification row -> broadcast_mail returns early. This is the
        # default state of a deployment, so it must not raise.
        tasks.notify_staff_new_issue(self.issue.id)

        self.assertEqual(len(mail.outbox), 0)

    def test_silent_when_the_notification_is_disabled(self):
        arm_notification(enabled=False)

        tasks.notify_staff_new_issue(self.issue.id)

        self.assertEqual(len(mail.outbox), 0)

    def test_regular_users_are_not_notified(self):
        arm_notification()
        structure_factories.UserFactory(email="nobody@example.com")

        tasks.notify_staff_new_issue(self.issue.id)

        self.assertNotIn("nobody@example.com", self.recipients())

    def test_users_who_opted_out_are_not_notified(self):
        arm_notification()
        structure_factories.UserFactory(
            is_support=True,
            email="quiet@example.com",
            notifications_enabled=False,
        )

        tasks.notify_staff_new_issue(self.issue.id)

        self.assertNotIn("quiet@example.com", self.recipients())

    def test_inactive_and_address_less_users_are_not_notified(self):
        arm_notification()
        structure_factories.UserFactory(
            is_support=True, email="gone@example.com", is_active=False
        )
        structure_factories.UserFactory(is_support=True, email="")

        tasks.notify_staff_new_issue(self.issue.id)

        self.assertNotIn("gone@example.com", self.recipients())

    def test_a_missing_issue_is_not_an_error(self):
        arm_notification()

        tasks.notify_staff_new_issue(self.issue.id + 10000)

        self.assertEqual(len(mail.outbox), 0)

    def test_the_message_names_the_request(self):
        arm_notification()

        tasks.notify_staff_new_issue(self.issue.id)

        self.assertIn(self.issue.key, mail.outbox[0].subject)
        self.assertIn(self.issue.summary, mail.outbox[0].subject)


@BASIC
@override_settings(task_always_eager=True)
class StaffNewIssueNotificationHandlerTest(TestCase):
    """The trigger: fires once per created ticket, and only where it should."""

    def setUp(self):
        arm_notification()
        structure_factories.UserFactory(is_staff=True, email="staff@example.com")
        self.backend = BasicBackend()

    def create_issue(self, **kwargs):
        """Create an issue the way the API does — a blank save, then the backend."""
        issue = factories.IssueFactory(backend_id="", key="", status="", **kwargs)
        models.Issue.objects.filter(pk=issue.pk).update(
            backend_id="", key="", status=""
        )
        issue.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            self.backend.create_issue(issue)
        return issue

    def test_creating_a_ticket_notifies_staff_once(self):
        self.create_issue()

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("staff@example.com", mail.outbox[0].to)

    def test_updating_a_ticket_does_not_notify_again(self):
        issue = self.create_issue()
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            issue.summary = "Edited after the fact"
            issue.save()

        self.assertEqual(len(mail.outbox), 0)

    @override_config(WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="atlassian")
    def test_an_external_backend_notifies_its_own_agents(self):
        # Jira, Zammad and SMAX alert their agents themselves; announcing the
        # ticket from Waldur as well would double up.
        self.create_issue()

        self.assertEqual(len(mail.outbox), 0)

    def test_a_provider_routed_ticket_does_not_notify_operator_staff(self):
        # The child issue belongs to the provider, who is told by
        # notify_provider_new_ticket instead.
        helpdesk = factories.ProviderHelpdeskFactory()

        self.create_issue(provider_helpdesk=helpdesk)

        self.assertEqual(len(mail.outbox), 0)


@BASIC
class StaffEscalationNotificationTest(TestCase):
    """notify_ticket_escalated used to only write a log line."""

    def setUp(self):
        self.issue = factories.IssueFactory(backend_id="WLD-901", key="WLD-901")
        structure_factories.UserFactory(is_staff=True, email="staff@example.com")
        structure_factories.UserFactory(is_support=True, email="support@example.com")

    def recipients(self):
        return {address for message in mail.outbox for address in message.to}

    def test_staff_are_told_and_the_reason_travels_with_it(self):
        structure_factories.NotificationFactory(
            key="support.notification_issue_escalated", enabled=True
        )

        tasks.notify_ticket_escalated(self.issue.id, "Breached the agreed deadline")

        self.assertEqual(
            self.recipients(), {"staff@example.com", "support@example.com"}
        )
        self.assertIn("Breached the agreed deadline", mail.outbox[0].body)
        self.assertIn(self.issue.key, mail.outbox[0].subject)

    def test_silent_when_not_registered(self):
        tasks.notify_ticket_escalated(self.issue.id, "reason")

        self.assertEqual(len(mail.outbox), 0)

    @override_config(WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="atlassian")
    def test_an_external_backend_reaches_its_agents_elsewhere(self):
        structure_factories.NotificationFactory(
            key="support.notification_issue_escalated", enabled=True
        )

        tasks.notify_ticket_escalated(self.issue.id, "reason")

        self.assertEqual(len(mail.outbox), 0)


class DeadProviderNotificationsTest(TestCase):
    """Two provider notifications were registered nowhere and called nowhere.

    broadcast_mail returns early for an unregistered key, so even once they were
    invoked they could not send. These assert both halves are now wired.
    """

    def setUp(self):
        self.helpdesk = factories.ProviderHelpdeskFactory(
            notification_email="provider@example.com",
            notify_on_comment=True,
            notify_on_sla_warning=True,
        )
        self.parent = factories.IssueFactory(backend_id="WLD-910", key="WLD-910")
        self.child = factories.IssueFactory(
            backend_id="WLD-911",
            key="WLD-911",
            parent_issue=self.parent,
            provider_helpdesk=self.helpdesk,
        )

    def test_customer_comment_reaches_the_provider(self):
        structure_factories.NotificationFactory(
            key="support.provider_customer_comment", enabled=True
        )
        comment = factories.CommentFactory(issue=self.child)

        tasks.notify_provider_customer_comment(comment.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("provider@example.com", mail.outbox[0].to)

    def test_sla_warning_reaches_the_provider(self):
        structure_factories.NotificationFactory(
            key="support.provider_sla_warning", enabled=True
        )

        tasks.notify_provider_sla_warning(self.child.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("provider@example.com", mail.outbox[0].to)

    def test_both_respect_the_per_helpdesk_switches(self):
        structure_factories.NotificationFactory(
            key="support.provider_customer_comment", enabled=True
        )
        structure_factories.NotificationFactory(
            key="support.provider_sla_warning", enabled=True
        )
        self.helpdesk.notify_on_comment = False
        self.helpdesk.notify_on_sla_warning = False
        self.helpdesk.save()
        comment = factories.CommentFactory(issue=self.child)

        tasks.notify_provider_customer_comment(comment.id)
        tasks.notify_provider_sla_warning(self.child.id)

        self.assertEqual(len(mail.outbox), 0)
