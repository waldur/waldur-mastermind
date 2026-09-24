from constance.test.unittest import override_config
from django.core import mail
from django.test import TestCase, override_settings

from waldur_core.core import models as core_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support.tests import factories

BASIC = override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)

CALLER_KEY = "support.notification_comment_added"
STAFF_KEY = "support.notification_comment_added_staff"


def arm(key, enabled=True):
    return structure_factories.NotificationFactory(key=key, enabled=enabled)


@BASIC
@override_settings(task_always_eager=True)
class CallerCommentNotificationTest(TestCase):
    """A comment from the person who raised the ticket used to reach nobody.

    The only comment notification was addressed to the caller, and the handler
    dropped the caller's own comments before it was ever sent.
    """

    def setUp(self):
        arm(CALLER_KEY)
        arm(STAFF_KEY)
        self.caller = structure_factories.UserFactory(email="caller@example.com")
        self.staff = structure_factories.UserFactory(
            is_staff=True, email="staff@example.com"
        )
        self.support = structure_factories.UserFactory(
            is_support=True, email="support@example.com"
        )
        self.issue = factories.IssueFactory(caller=self.caller, status="Open")

    def comment_as(self, user, issue=None, **kwargs):
        author = factories.SupportUserFactory(user=user, name=user.full_name)
        # The factory makes comments private by default, and a private comment
        # deliberately notifies nobody.
        kwargs.setdefault("is_public", True)
        # The notification is dispatched from an on_commit hook, which a plain
        # TestCase never reaches on its own.
        with self.captureOnCommitCallbacks(execute=True):
            return factories.CommentFactory(
                issue=issue or self.issue, author=author, **kwargs
            )

    def recipients(self):
        return sorted(address for message in mail.outbox for address in message.to)

    def test_caller_comment_reaches_the_whole_helpdesk_when_unassigned(self):
        self.comment_as(self.caller)

        self.assertEqual(
            self.recipients(), ["staff@example.com", "support@example.com"]
        )
        self.assertIn(self.issue.key, mail.outbox[0].subject)

    def test_caller_comment_reaches_only_the_assignee_when_assigned(self):
        assignee = structure_factories.UserFactory(email="assignee@example.com")
        self.issue.assignee = factories.SupportUserFactory(user=assignee)
        self.issue.save()

        self.comment_as(self.caller)

        self.assertEqual(self.recipients(), ["assignee@example.com"])

    def test_caller_never_receives_their_own_comment(self):
        self.comment_as(self.caller)

        self.assertNotIn("caller@example.com", self.recipients())

    def test_a_staff_caller_is_not_notified_of_their_own_comment(self):
        issue = factories.IssueFactory(caller=self.staff, status="Open")
        self.comment_as(self.staff, issue=issue)

        self.assertEqual(self.recipients(), ["support@example.com"])

    def test_helpdesk_comment_still_goes_to_the_caller_alone(self):
        self.comment_as(self.support)

        self.assertEqual(self.recipients(), ["caller@example.com"])

    def test_private_comment_notifies_nobody(self):
        self.comment_as(self.caller, is_public=False)

        self.assertEqual(mail.outbox, [])

    def test_notification_can_be_switched_off(self):
        core_models.Notification.objects.filter(key=STAFF_KEY).update(enabled=False)

        self.comment_as(self.caller)

        self.assertEqual(mail.outbox, [])

    def test_an_unreachable_assignee_falls_back_to_the_helpdesk(self):
        # An agent who leaves, or switches notifications off, must not swallow
        # every reply on the tickets they still hold.
        gone = structure_factories.UserFactory(
            email="gone@example.com", is_active=False
        )
        self.issue.assignee = factories.SupportUserFactory(user=gone)
        self.issue.save()

        self.comment_as(self.caller)

        self.assertEqual(
            self.recipients(), ["staff@example.com", "support@example.com"]
        )

    def test_support_switched_off_sends_nothing(self):
        with override_config(WALDUR_SUPPORT_ENABLED=False):
            self.comment_as(self.caller)

        self.assertEqual(mail.outbox, [])

    def test_personnel_who_opted_out_are_skipped(self):
        self.support.notifications_enabled = False
        self.support.save()

        self.comment_as(self.caller)

        self.assertEqual(self.recipients(), ["staff@example.com"])


EDITED_KEY = "support.notification_comment_updated_staff"
CALLER_EDITED_KEY = "support.notification_comment_updated"


@BASIC
@override_settings(task_always_eager=True)
class CallerCommentEditNotificationTest(TestCase):
    """The caller may now edit their own comment, which the helpdesk may already
    have acted on, so the edit goes to whoever works the ticket."""

    def setUp(self):
        arm(EDITED_KEY)
        arm(CALLER_EDITED_KEY)
        self.caller = structure_factories.UserFactory(email="caller@example.com")
        self.staff = structure_factories.UserFactory(
            is_staff=True, email="staff@example.com"
        )
        self.issue = factories.IssueFactory(caller=self.caller, status="Open")
        self.comment = factories.CommentFactory(
            issue=self.issue,
            author=factories.SupportUserFactory(user=self.caller),
            description="Original text",
            is_public=True,
        )
        mail.outbox = []

    def edit(self, comment, description="Edited text"):
        comment.description = description
        with self.captureOnCommitCallbacks(execute=True):
            comment.save()

    def recipients(self):
        return sorted(address for message in mail.outbox for address in message.to)

    def test_caller_edit_reaches_the_assignee_with_both_versions(self):
        assignee = structure_factories.UserFactory(email="assignee@example.com")
        self.issue.assignee = factories.SupportUserFactory(user=assignee)
        self.issue.save()

        self.edit(self.comment)

        self.assertEqual(self.recipients(), ["assignee@example.com"])
        self.assertIn(self.issue.key, mail.outbox[0].subject)
        self.assertIn("Original text", mail.outbox[0].body)
        self.assertIn("Edited text", mail.outbox[0].body)

    def test_caller_edit_reaches_the_helpdesk_when_unassigned(self):
        self.edit(self.comment)

        self.assertEqual(self.recipients(), ["staff@example.com"])

    def test_caller_is_not_told_about_their_own_edit(self):
        self.edit(self.comment)

        self.assertNotIn("caller@example.com", self.recipients())

    def test_unchanged_description_notifies_nobody(self):
        self.edit(self.comment, description="Original text")

        self.assertEqual(mail.outbox, [])

    def test_private_comment_edit_notifies_nobody(self):
        self.comment.is_public = False
        self.comment.save()

        self.edit(self.comment)

        self.assertEqual(mail.outbox, [])

    def test_notification_is_off_until_enabled(self):
        core_models.Notification.objects.filter(key=EDITED_KEY).update(enabled=False)

        self.edit(self.comment)

        self.assertEqual(mail.outbox, [])

    def test_helpdesk_edit_still_goes_to_the_caller(self):
        comment = factories.CommentFactory(
            issue=self.issue,
            author=factories.SupportUserFactory(user=self.staff),
            is_public=True,
        )
        mail.outbox = []

        self.edit(comment)

        self.assertEqual(self.recipients(), ["caller@example.com"])
