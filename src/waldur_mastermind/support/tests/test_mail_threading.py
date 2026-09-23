from constance.test.unittest import override_config
from django.core import mail
from django.test import TestCase, override_settings

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import tasks
from waldur_mastermind.support.tests import factories
from waldur_mastermind.support.utils import (
    get_issue_thread_headers,
    get_issue_thread_id,
)

BASIC = override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)

SENDER = override_settings(DEFAULT_FROM_EMAIL="Waldur <noreply@example.com>")


class IssueThreadIdTest(TestCase):
    @SENDER
    def test_the_id_is_built_from_the_issue_and_the_sender_domain(self):
        issue = factories.IssueFactory()

        self.assertEqual(
            get_issue_thread_id(issue.uuid),
            f"<support-issue-{issue.uuid.hex}@example.com>",
        )

    @SENDER
    def test_the_id_does_not_change_between_calls(self):
        issue = factories.IssueFactory()

        self.assertEqual(
            get_issue_thread_id(issue.uuid), get_issue_thread_id(issue.uuid)
        )

    @override_settings(DEFAULT_FROM_EMAIL="noreply")
    def test_a_sender_without_a_domain_still_yields_a_usable_id(self):
        issue = factories.IssueFactory()

        self.assertEqual(
            get_issue_thread_id(issue.uuid),
            f"<support-issue-{issue.uuid.hex}@localhost>",
        )

    def test_two_issues_do_not_share_a_thread(self):
        self.assertNotEqual(
            get_issue_thread_id(factories.IssueFactory().uuid),
            get_issue_thread_id(factories.IssueFactory().uuid),
        )


class IssueThreadHeadersTest(TestCase):
    def setUp(self):
        self.issue = factories.IssueFactory()

    def test_every_message_points_at_the_thread(self):
        thread_id = get_issue_thread_id(self.issue.uuid)

        self.assertEqual(
            get_issue_thread_headers(self.issue.uuid),
            {"In-Reply-To": thread_id, "References": thread_id},
        )

    def test_no_message_claims_the_thread_id_as_its_own(self):
        # One notification fans out to one message per recipient, so a shared
        # Message-ID would be suppressed as a duplicate by a mailbox that sees
        # it twice.
        self.assertNotIn("Message-ID", get_issue_thread_headers(self.issue.uuid))


@BASIC
class SupportMailThreadingTest(TestCase):
    """Every mail about one ticket carries the ticket's thread id."""

    def setUp(self):
        self.caller = structure_factories.UserFactory(email="caller@example.com")
        self.issue = factories.IssueFactory(
            caller=self.caller, backend_id="WLD-800", key="WLD-800", status="Open"
        )
        structure_factories.UserFactory(is_support=True, email="support@example.com")
        self.thread_id = get_issue_thread_id(self.issue.uuid)

    def arm(self, key):
        return structure_factories.NotificationFactory(key=key, enabled=True)

    def sent(self):
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0].message()

    def test_the_created_notification_joins_the_thread(self):
        self.arm("support.notification_issue_created")

        tasks.notify_staff_new_issue(self.issue.id)

        message = self.sent()
        self.assertEqual(message["In-Reply-To"], self.thread_id)
        self.assertNotEqual(message["Message-ID"], self.thread_id)

    def test_every_recipient_of_one_notification_gets_a_distinct_message_id(self):
        # Two helpdesk addresses that resolve to the same mailbox would lose a
        # copy if both messages carried the same id.
        self.arm("support.notification_issue_created")
        structure_factories.UserFactory(is_support=True, email="second@example.com")

        tasks.notify_staff_new_issue(self.issue.id)

        ids = [message.message()["Message-ID"] for message in mail.outbox]
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(len(set(ids)), 2, ids)

    def test_an_update_replies_into_the_thread(self):
        self.arm("support.notification_issue_updated")

        tasks.send_issue_updated_notification(
            core_utils.serialize_instance(self.issue), {}
        )

        message = self.sent()
        self.assertEqual(message["In-Reply-To"], self.thread_id)
        self.assertEqual(message["References"], self.thread_id)

    def test_an_update_subject_is_prefixed_with_the_issue_key(self):
        self.arm("support.notification_issue_updated")

        tasks.send_issue_updated_notification(
            core_utils.serialize_instance(self.issue), {}
        )

        self.assertTrue(
            mail.outbox[0].subject.startswith(f"[{self.issue.key}] "),
            mail.outbox[0].subject,
        )

    def test_the_key_is_not_repeated_when_the_subject_already_carries_it(self):
        # An operator-authored subject that opens with the key of its own
        # accord. `TemplateStatusNotification` wins over the shipped template,
        # so this is the one subject the prefix cannot be applied to blindly.
        self.arm("support.notification_issue_updated")
        factories.TemplateStatusNotificationFactory(
            status=self.issue.status,
            subject="[{{ issue.key }}] Updated",
            text="{{ issue.summary }}",
            html="{{ issue.summary }}",
        )

        tasks.send_issue_updated_notification(
            core_utils.serialize_instance(self.issue), {}
        )

        self.assertEqual(mail.outbox[0].subject, f"[{self.issue.key}] Updated")

    def test_a_comment_to_the_helpdesk_joins_the_thread(self):
        self.arm("support.notification_comment_added_staff")
        comment = factories.CommentFactory(issue=self.issue, is_public=True)

        tasks.notify_helpdesk_new_comment(comment)

        self.assertEqual(self.sent()["In-Reply-To"], self.thread_id)

    def test_an_escalation_joins_the_thread(self):
        self.arm("support.notification_issue_escalated")

        tasks.notify_ticket_escalated(self.issue.id, "no response")

        self.assertEqual(self.sent()["In-Reply-To"], self.thread_id)


class ProviderMailThreadingTest(TestCase):
    """A ticket routed to a provider is a thread of its own, keyed by the child."""

    def setUp(self):
        self.helpdesk = factories.ProviderHelpdeskFactory(
            notification_email="provider@example.com",
            notify_on_new_ticket=True,
            notify_on_comment=True,
        )
        self.issue = factories.IssueFactory(backend_id="WLD-810", key="WLD-810")
        self.child = factories.IssueFactory(
            parent_issue=self.issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-811",
            key="WLD-811",
        )

    def test_the_provider_thread_follows_the_child_ticket(self):
        structure_factories.NotificationFactory(
            key="support.provider_new_ticket", enabled=True
        )

        tasks.notify_provider_new_ticket(self.child.id)

        message = mail.outbox[0].message()
        self.assertEqual(message["In-Reply-To"], get_issue_thread_id(self.child.uuid))
        self.assertNotEqual(
            message["In-Reply-To"], get_issue_thread_id(self.issue.uuid)
        )

    def test_an_escalation_is_keyed_on_the_ticket_the_provider_holds(self):
        # The escalation is raised on the operator ticket, but the provider only
        # ever saw the child, so both the thread and the subject follow it.
        self.helpdesk.notify_on_escalation = True
        self.helpdesk.save()
        structure_factories.NotificationFactory(
            key="support.provider_escalation", enabled=True
        )

        tasks.notify_provider_escalation(self.issue.id, "no response")

        message = mail.outbox[0]
        self.assertTrue(
            message.subject.startswith(f"[{self.child.key}]"), message.subject
        )
        self.assertEqual(
            message.message()["In-Reply-To"], get_issue_thread_id(self.child.uuid)
        )

    def test_a_withdrawal_stays_in_the_thread_of_the_ticket_being_withdrawn(self):
        # The child row is deleted by the reroute, so the task is told which
        # ticket the mail is about; without it the provider would get a mail
        # keyed on an operator ticket they have never seen.
        structure_factories.NotificationFactory(
            key="support.provider_ticket_withdrawn", enabled=True
        )
        child_uuid, child_key = self.child.uuid, self.child.key
        self.child.delete()

        tasks.notify_provider_ticket_withdrawn(
            self.issue.id, self.helpdesk.id, child_uuid.hex, child_key
        )

        message = mail.outbox[0]
        self.assertTrue(message.subject.startswith(f"[{child_key}]"), message.subject)
        self.assertEqual(
            message.message()["In-Reply-To"], get_issue_thread_id(child_uuid)
        )

    def test_a_withdrawal_without_a_child_falls_back_to_the_operator_ticket(self):
        # A task enqueued before the child arguments existed.
        structure_factories.NotificationFactory(
            key="support.provider_ticket_withdrawn", enabled=True
        )

        tasks.notify_provider_ticket_withdrawn(self.issue.id, self.helpdesk.id)

        message = mail.outbox[0]
        self.assertTrue(
            message.subject.startswith(f"[{self.issue.key}]"), message.subject
        )
        self.assertEqual(
            message.message()["In-Reply-To"], get_issue_thread_id(self.issue.uuid)
        )

    def test_a_customer_comment_joins_the_provider_thread(self):
        structure_factories.NotificationFactory(
            key="support.provider_customer_comment", enabled=True
        )
        comment = factories.CommentFactory(issue=self.child, is_public=True)

        tasks.notify_provider_customer_comment(comment.id)

        self.assertEqual(
            mail.outbox[0].message()["In-Reply-To"],
            get_issue_thread_id(self.child.uuid),
        )


class UnrelatedMailTest(TestCase):
    """Mail that passes no headers is left exactly as it was."""

    def test_no_threading_headers_are_invented(self):
        core_utils.send_mail("Subject", "Body", ["somebody@example.com"])

        message = mail.outbox[0].message()
        self.assertIsNone(message["In-Reply-To"])
        self.assertIsNone(message["References"])
        self.assertNotIn("support-issue-", message["Message-ID"])
