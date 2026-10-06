"""Staff opening a support request addressed to another user (#491).

The on-behalf branch of `POST /api/support-issues/` carries an opening message
in `first_comment`, which becomes the ticket's first public comment.
"""

from datetime import timedelta
from unittest import mock

from constance.test.unittest import override_config
from ddt import data, ddt
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure import exceptions as structure_exceptions
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models, tasks, utils
from waldur_mastermind.support.tests import base, factories, fixtures

OPENING_MESSAGE = "Hi, your SSH key expires on Friday. Please rotate it."


def valid_payload(caller, **extra):
    request_type = factories.RequestTypeFactory(is_active=True)
    payload = {
        "summary": "Your SSH key expires on Friday",
        "type": request_type.name,
        "caller": structure_factories.UserFactory.get_url(caller),
        "first_comment": OPENING_MESSAGE,
    }
    payload.update(extra)
    return payload


@ddt
class StaffInitiatedIssueTest(base.BaseTest):
    """API contract and backend dispatch, against a mocked active backend.

    `base.BaseTest` configures Atlassian and mocks `get_active_backend`, so this
    is also where the remote desks are covered: the view hands the comment to
    whichever backend is active, and each backend's own `create_comment` tests
    cover the push itself.
    """

    def setUp(self):
        super().setUp()
        self.url = factories.IssueFactory.get_list_url()
        self.caller = structure_factories.UserFactory()
        self.staff = self.fixture.staff
        self.client.force_authenticate(self.staff)

    def create(self, **extra):
        return self.client.post(self.url, valid_payload(self.caller, **extra))

    # --- the opening message ---------------------------------------------

    def test_opening_message_is_first_public_comment_by_sender(self):
        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        issue = models.Issue.objects.get(uuid=response.data["uuid"])
        comments = list(issue.comments.all())
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].description, OPENING_MESSAGE)
        self.assertTrue(comments[0].is_public)
        self.assertEqual(comments[0].author.user, self.staff)

    def test_opening_message_is_pushed_to_the_active_backend(self):
        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        comment = models.Comment.objects.get(issue__uuid=response.data["uuid"])
        self.mock_get_active_backend().create_comment.assert_called_once_with(comment)

    def test_recipient_is_notified_through_comment_added(self):
        with mock.patch(
            "waldur_mastermind.support.handlers.tasks.send_comment_added_notification"
        ) as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        comment = models.Comment.objects.get(issue__uuid=response.data["uuid"])
        task.delay.assert_called_once()
        (serialized,) = task.delay.call_args.args
        self.assertEqual(serialized, f"support.comment:{comment.pk}")

    def test_issue_is_rolled_back_when_the_comment_cannot_be_pushed(self):
        self.mock_get_active_backend().create_comment.side_effect = (
            structure_exceptions.ServiceBackendError("desk is down")
        )

        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.Issue.objects.exists())
        self.assertFalse(models.Comment.objects.exists())

    def test_blank_opening_message_creates_no_comment(self):
        response = self.create(first_comment="")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertFalse(models.Comment.objects.exists())
        self.mock_get_active_backend().create_comment.assert_not_called()

    def test_whitespace_opening_message_creates_no_comment(self):
        response = self.create(first_comment="   ")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertFalse(models.Comment.objects.exists())

    def test_first_comment_is_refused_on_update(self):
        response = self.create(first_comment="")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        update = self.client.patch(response.data["url"], {"first_comment": "Hello"})

        self.assertEqual(update.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("first_comment", update.data)
        self.assertFalse(models.Comment.objects.exists())

    def test_first_comment_is_absent_from_responses(self):
        response = self.create(first_comment="")

        self.assertNotIn("first_comment", response.data)
        detail = self.client.get(response.data["url"])
        self.assertNotIn("first_comment", detail.data)

    # --- the recipient ---------------------------------------------------

    def test_recipient_can_open_and_reply_to_the_ticket(self):
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        issue_url = response.data["url"]

        self.client.force_authenticate(self.caller)
        self.assertEqual(self.client.get(issue_url).status_code, status.HTTP_200_OK)
        reply = self.client.post(issue_url + "comment/", {"description": "Done."})

        self.assertEqual(reply.status_code, status.HTTP_201_CREATED, reply.data)

    # --- what must stay as it is -----------------------------------------

    @data("atlassian", "zammad", "smax")
    def test_reporter_is_not_set_on_a_remote_backend(self, backend_type):
        # On a remote desk the reporter decides who the ticket is filed as, so
        # it stays empty and the ticket stays the caller's.
        with override_config(WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=backend_type):
            response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        issue = models.Issue.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(issue.reporter)
        self.assertIsNone(issue.assignee)

    @data("atlassian", "zammad", "smax")
    def test_remote_desk_description_keeps_the_reporter_mark(self, backend_type):
        # The remote desk's agents read the description and have no reporter
        # field for the sender, so the mark stays there.
        with override_config(WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=backend_type):
            response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        issue = models.Issue.objects.get(uuid=response.data["uuid"])
        self.assertIn(f"Reported by {self.staff.full_name}.", issue.description)

    def test_reporting_manually_rejects_an_opening_message(self):
        self.client.force_authenticate(self.fixture.user)
        payload = valid_payload(self.caller, is_reported_manually=True)
        del payload["caller"]

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("first_comment", response.data)

    def test_reporting_manually_is_otherwise_unaffected(self):
        self.client.force_authenticate(self.fixture.user)
        payload = valid_payload(self.caller, is_reported_manually=True)
        del payload["caller"]
        del payload["first_comment"]

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        issue = models.Issue.objects.get(uuid=response.data["uuid"])
        self.assertEqual(issue.caller, self.fixture.user)
        self.assertFalse(issue.comments.exists())


@override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)
class StaffInitiatedIssueBasicTest(test.APITestCase):
    """End to end on the built-in desk, with the real BasicBackend."""

    def setUp(self):
        self.fixture = fixtures.SupportFixture()
        self.url = factories.IssueFactory.get_list_url()
        self.caller = structure_factories.UserFactory(email="bob@example.com")
        self.staff = self.fixture.staff

    def create(self, **extra):
        self.client.force_authenticate(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, valid_payload(self.caller, **extra))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return models.Issue.objects.get(uuid=response.data["uuid"])

    def reply_as_caller(self, issue):
        self.client.force_authenticate(self.caller)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                factories.IssueFactory.get_url(issue, action="comment"),
                {"description": "Rotated it, thanks."},
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        issue.refresh_from_db()

    def test_staff_member_is_recorded_as_reporter_and_assignee(self):
        issue = self.create()

        self.assertIsNotNone(issue.reporter)
        self.assertEqual(issue.reporter.user, self.staff)
        self.assertEqual(issue.assignee, issue.reporter)

    def test_a_named_assignee_is_kept(self):
        colleague = factories.SupportUserFactory()

        issue = self.create(assignee=factories.SupportUserFactory.get_url(colleague))

        self.assertEqual(issue.reporter.user, self.staff)
        self.assertEqual(issue.assignee, colleague)

    @override_config(WALDUR_SUPPORT_AUTO_ASSIGN=True)
    def test_auto_assign_does_not_take_the_ticket_from_the_sender(self):
        structure_factories.UserFactory(is_support=True)

        issue = self.create()

        self.assertEqual(issue.assignee.user, self.staff)

    @override_config(WALDUR_SUPPORT_SLA_ENABLED=True)
    def test_request_logged_on_behalf_without_a_message_is_unchanged(self):
        # A support agent logging a user's call: the user asked something, so
        # the desk is told and the response clock runs as for any request.
        with mock.patch(
            "waldur_mastermind.support.handlers.tasks.notify_staff_new_issue"
        ) as task:
            issue = self.create(first_comment="")

        self.assertIsNone(issue.reporter)
        self.assertIsNone(issue.assignee)
        self.assertIsNotNone(issue.first_response_deadline)
        task.delay.assert_called_once_with(issue.id)

    @override_settings(task_always_eager=True)
    def test_recipient_reply_is_mailed_to_the_sender_only(self):
        structure_factories.NotificationFactory(
            key="support.notification_comment_added_staff", enabled=True
        )
        self.staff.email = "alice@example.com"
        self.staff.save()
        structure_factories.UserFactory(is_support=True, email="desk@example.com")
        issue = self.create()
        mail.outbox.clear()

        self.reply_as_caller(issue)

        recipients = {address for message in mail.outbox for address in message.to}
        self.assertEqual(recipients, {"alice@example.com"})

    @override_config(WALDUR_SUPPORT_SLA_ENABLED=True)
    def test_unanswered_request_is_never_breached(self):
        # An informational message may get no reply; that is not the desk
        # failing an SLA.
        issue = self.create()

        self.assertIsNone(issue.first_response_deadline)
        self.assertIsNone(issue.resolution_deadline)
        with freeze_time(timezone.now() + timedelta(days=30)):
            tasks.check_sla_breaches()
        issue.refresh_from_db()
        self.assertFalse(issue.sla_breached)

    @override_config(WALDUR_SUPPORT_SLA_ENABLED=True)
    def test_recipient_reply_starts_the_sla_deadlines(self):
        issue = self.create()

        self.reply_as_caller(issue)

        self.assertIsNotNone(issue.first_response_deadline)
        self.assertIsNotNone(issue.resolution_deadline)
        self.assertIsNone(issue.first_response_at)

    def test_recipient_reply_sets_no_deadline_when_sla_is_off(self):
        issue = self.create()

        self.reply_as_caller(issue)

        self.assertIsNone(issue.first_response_deadline)
        self.assertIsNone(issue.resolution_deadline)

    def test_response_time_runs_from_the_recipient_reply(self):
        # Opened on day 0, answered by the recipient on day 4, by staff an hour
        # later: the desk took an hour, not four days.
        issue = self.create()
        self.reply_as_caller(issue)
        reply = issue.comments.get(author__user=self.caller)
        start = timezone.now() - timedelta(days=10)
        models.Issue.objects.filter(pk=issue.pk).update(
            created=start,
            first_response_at=start + timedelta(days=4, hours=1),
        )
        models.Comment.objects.filter(pk=reply.pk).update(
            created=start + timedelta(days=4)
        )

        stats = utils.get_helpdesk_stats()

        self.assertAlmostEqual(stats["avg_first_response_hours"], 1.0, places=3)

    def test_reporting_manually_records_no_reporter(self):
        self.client.force_authenticate(self.caller)
        payload = valid_payload(self.caller, is_reported_manually=True)
        del payload["caller"]
        del payload["first_comment"]

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        issue = models.Issue.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(issue.reporter)

    def test_staff_naming_themselves_as_caller_is_an_ordinary_request(self):
        # Homeport's CustomerErrorDialog files staff's own reports this way.
        self.client.force_authenticate(self.staff)
        payload = valid_payload(self.staff)
        del payload["first_comment"]

        with mock.patch(
            "waldur_mastermind.support.handlers.tasks.notify_staff_new_issue"
        ) as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        issue = models.Issue.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(issue.reporter)
        task.delay.assert_called_once_with(issue.id)

    def test_opening_message_to_oneself_is_refused(self):
        self.client.force_authenticate(self.staff)

        response = self.client.post(self.url, valid_payload(self.staff))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("first_comment", response.data)
        self.assertFalse(models.Issue.objects.exists())

    def test_sender_is_not_told_about_a_request_they_made(self):
        with mock.patch(
            "waldur_mastermind.support.handlers.tasks.notify_staff_new_issue"
        ) as task:
            self.create()

        task.delay.assert_not_called()

    def test_opening_message_does_not_start_the_response_clock(self):
        issue = self.create()

        self.assertEqual(issue.comments.count(), 1)
        self.assertIsNone(issue.first_response_at)

    @override_settings(task_always_eager=True)
    def test_recipient_receives_the_opening_message_by_email(self):
        structure_factories.NotificationFactory(
            key="support.notification_comment_added", enabled=True
        )

        issue = self.create()

        (message,) = [m for m in mail.outbox if "bob@example.com" in m.to]
        # Bob did not create this request, so the mail must not say he did,
        # and the plain-text part carries the message, not just a link.
        self.assertNotIn("you have created", message.subject)
        self.assertIn(issue.key, message.subject)
        self.assertIn(OPENING_MESSAGE, message.body)
        self.assertIn(self.staff.full_name, message.body)

    def test_description_holds_only_what_staff_wrote(self):
        # No "Reported by" mark and no agent-facing "Additional Info" block:
        # the caller reads this, and the opening message names the sender.
        issue = self.create(description="")

        self.assertEqual(issue.description, "")

    def test_summary_has_no_trailing_newline(self):
        issue = self.create()

        self.assertEqual(issue.summary, "Your SSH key expires on Friday")

    def test_request_logged_on_behalf_keeps_its_description_marks(self):
        issue = self.create(first_comment="", description="Cannot log in.")

        self.assertIn(f"Reported by {self.staff.full_name}.", issue.description)
        self.assertIn("Cannot log in.", issue.description)
        self.assertIn("Additional Info", issue.description)
