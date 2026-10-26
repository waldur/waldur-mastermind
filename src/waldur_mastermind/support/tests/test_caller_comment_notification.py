from django.core import mail
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from waldur_core.configuration.testing import override_config
from waldur_core.core import models as core_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
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


@BASIC
@override_settings(task_always_eager=True)
class CustomerSideCommentNotificationTest(TestCase):
    """Organization owners and project admins and managers may comment on a
    ticket somebody else raised. The helpdesk used to hear only about the
    caller's comments, so theirs reached the caller alone."""

    def setUp(self):
        arm(CALLER_KEY)
        arm(STAFF_KEY)
        arm(CALLER_EDITED_KEY)
        arm(EDITED_KEY)
        self.fixture = structure_fixtures.ProjectFixture()
        self.caller = self.fixture.member
        self.owner = self.fixture.owner
        self.staff = structure_factories.UserFactory(
            is_staff=True, email="staff@example.com"
        )
        self.support = structure_factories.UserFactory(
            is_support=True, email="support@example.com"
        )
        self.issue = self.make_issue(caller=self.caller)

    def make_issue(self, **kwargs):
        return factories.IssueFactory(
            customer=self.fixture.customer,
            project=self.fixture.project,
            status="Open",
            **kwargs,
        )

    def comment_as(self, user, issue=None, **kwargs):
        author = factories.SupportUserFactory(user=user, name=user.full_name)
        kwargs.setdefault("is_public", True)
        with self.captureOnCommitCallbacks(execute=True):
            return factories.CommentFactory(
                issue=issue or self.issue, author=author, **kwargs
            )

    def recipients(self):
        return sorted(address for message in mail.outbox for address in message.to)

    def subjects_by_recipient(self):
        return {
            address: message.subject
            for message in mail.outbox
            for address in message.to
        }

    def test_comment_reaches_the_caller_and_the_helpdesk(self):
        for role in ("owner", "admin", "manager"):
            with self.subTest(role=role):
                mail.outbox = []

                self.comment_as(getattr(self.fixture, role))

                self.assertEqual(
                    self.recipients(),
                    sorted(
                        [self.caller.email, "staff@example.com", "support@example.com"]
                    ),
                )
                subjects = self.subjects_by_recipient()
                self.assertIn(
                    "Your support request has a new comment",
                    subjects[self.caller.email],
                )
                self.assertIn("New comment from", subjects["staff@example.com"])
                self.assertIn(self.issue.key, subjects["staff@example.com"])

    def test_comment_reaches_the_assignee_rather_than_the_whole_helpdesk(self):
        assignee = structure_factories.UserFactory(email="assignee@example.com")
        self.issue.assignee = factories.SupportUserFactory(user=assignee)
        self.issue.save()

        self.comment_as(self.owner)

        self.assertEqual(
            self.recipients(), sorted([self.caller.email, "assignee@example.com"])
        )

    def test_edit_reaches_the_caller_and_the_helpdesk(self):
        comment = self.comment_as(self.owner, description="Original text")
        mail.outbox = []

        comment.description = "Edited text"
        with self.captureOnCommitCallbacks(execute=True):
            comment.save()

        self.assertEqual(
            self.recipients(),
            sorted([self.caller.email, "staff@example.com", "support@example.com"]),
        )
        to_staff = next(m for m in mail.outbox if m.to == ["staff@example.com"])
        self.assertIn("Original text", to_staff.body)
        self.assertIn("Edited text", to_staff.body)

    def test_a_staff_caller_is_told_only_once(self):
        issue = self.make_issue(caller=self.staff)

        self.comment_as(self.owner, issue=issue)

        self.assertEqual(
            self.recipients(), ["staff@example.com", "support@example.com"]
        )
        self.assertIn(
            "Your support request has a new comment",
            self.subjects_by_recipient()["staff@example.com"],
        )

    def test_an_assignee_who_is_the_caller_is_told_only_once(self):
        # The ticket's owner hears about it as its caller, so nobody else on
        # the desk needs to.
        issue = self.make_issue(
            caller=self.staff,
            assignee=factories.SupportUserFactory(user=self.staff),
        )

        self.comment_as(self.owner, issue=issue)

        self.assertEqual(self.recipients(), ["staff@example.com"])

    def test_ticket_routed_to_a_provider_is_left_to_the_provider(self):
        self.issue.provider_helpdesk = factories.ProviderHelpdeskFactory()
        self.issue.save()

        self.comment_as(self.owner)

        self.assertEqual(self.recipients(), [self.caller.email])

    def test_other_backends_leave_it_to_their_own_agents(self):
        with override_config(WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="atlassian"):
            self.comment_as(self.owner)

        self.assertEqual(self.recipients(), [self.caller.email])

    def test_private_and_forwarded_comments_reach_nobody(self):
        self.comment_as(self.owner, is_public=False)
        self.comment_as(self.owner, is_forwarded=True)

        self.assertEqual(mail.outbox, [])

    def test_helpdesk_comment_still_goes_to_the_caller_alone(self):
        self.comment_as(self.support)

        self.assertEqual(self.recipients(), [self.caller.email])

    def test_comment_posted_through_the_api_reaches_the_helpdesk(self):
        client = APIClient()
        client.force_authenticate(self.owner)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                factories.IssueFactory.get_url(self.issue, action="comment"),
                {"description": "Any news on this?"},
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.recipients(),
            sorted([self.caller.email, "staff@example.com", "support@example.com"]),
        )

    def edit_through_the_api(self, comment, user, description="Edited text"):
        client = APIClient()
        client.force_authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.patch(
                factories.CommentFactory.get_url(comment),
                {"description": description},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_edit_through_the_api_reaches_the_caller_and_the_helpdesk(self):
        comment = self.comment_as(self.owner, description="Original text")
        mail.outbox = []

        self.edit_through_the_api(comment, self.owner)

        self.assertEqual(
            self.recipients(),
            sorted([self.caller.email, "staff@example.com", "support@example.com"]),
        )

    def test_staff_correcting_an_owner_comment_tells_the_caller_alone(self):
        # Only staff may edit a comment they did not write, and the helpdesk
        # does not need telling about its own correction.
        comment = self.comment_as(self.owner, description="Original text")
        mail.outbox = []

        self.edit_through_the_api(comment, self.staff)

        self.assertEqual(self.recipients(), [self.caller.email])

    def test_a_staff_caller_hears_when_their_caller_notification_is_off(self):
        core_models.Notification.objects.filter(key=CALLER_KEY).update(enabled=False)
        issue = self.make_issue(caller=self.staff)

        self.comment_as(self.owner, issue=issue)

        self.assertEqual(
            self.recipients(), ["staff@example.com", "support@example.com"]
        )
        self.assertIn(
            "New comment from", self.subjects_by_recipient()["staff@example.com"]
        )

    def test_an_assignee_who_is_the_caller_hears_when_their_caller_notification_is_off(
        self,
    ):
        core_models.Notification.objects.filter(key=CALLER_KEY).update(enabled=False)
        issue = self.make_issue(
            caller=self.staff,
            assignee=factories.SupportUserFactory(user=self.staff),
        )

        self.comment_as(self.owner, issue=issue)

        self.assertEqual(self.recipients(), ["staff@example.com"])
        self.assertIn(
            "New comment from", self.subjects_by_recipient()["staff@example.com"]
        )

    def test_assignee_reply_goes_to_the_caller_alone(self):
        # Whoever the ticket is assigned to works it, staff or not, so their
        # replies are not news to the rest of the helpdesk.
        agent = structure_factories.UserFactory(email="agent@example.com")
        self.issue.assignee = factories.SupportUserFactory(user=agent)
        self.issue.save()

        self.comment_as(agent)

        self.assertEqual(self.recipients(), [self.caller.email])

    def test_comment_on_a_ticket_without_a_caller_reaches_the_helpdesk(self):
        issue = self.make_issue(caller=None)

        self.comment_as(self.owner, issue=issue)

        self.assertEqual(
            self.recipients(), ["staff@example.com", "support@example.com"]
        )
