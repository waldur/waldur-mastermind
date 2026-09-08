from constance.test.unittest import override_config
from rest_framework import status, test

from waldur_mastermind.support import models
from waldur_mastermind.support.backend.basic import BasicBackend
from waldur_mastermind.support.enums import IssueStatusTypes
from waldur_mastermind.support.tests import factories, fixtures

BASIC = override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)


@BASIC
class ClosedTicketIsReadOnlyTest(test.APITestCase):
    """A ticket that reached a terminal status takes no further comments.

    Staff are deliberately not exempt: reopening the ticket is the way back in,
    and that keeps `add_comment_is_available` a property of the ticket rather
    than of whoever asks for it.
    """

    def setUp(self):
        self.fixture = fixtures.SupportFixture()
        self.backend = BasicBackend()
        models.IssueStatus.objects.create(
            name="Resolved", type=IssueStatusTypes.RESOLVED
        )
        models.IssueStatus.objects.create(
            name="Canceled", type=IssueStatusTypes.CANCELED
        )
        self.issue = factories.IssueFactory(caller=self.fixture.staff, status="Open")

    def comment_url(self):
        return factories.IssueFactory.get_url(self.issue, action="comment")

    def post_comment(self):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(self.comment_url(), {"description": "a note"})

    def close(self, resolved):
        # Go through the model API rather than assigning the status, so that
        # `resolution_date` is synced exactly as the set_status action does it.
        if resolved:
            self.issue.set_resolved()
        else:
            self.issue.set_canceled()

    def test_open_ticket_accepts_a_comment(self):
        self.assertTrue(self.backend.comment_create_is_available(self.issue))
        self.assertEqual(self.post_comment().status_code, status.HTTP_201_CREATED)

    def test_resolved_ticket_refuses_a_comment(self):
        self.close(resolved=True)

        self.assertFalse(self.backend.comment_create_is_available(self.issue))
        self.assertEqual(self.post_comment().status_code, status.HTTP_400_BAD_REQUEST)

    def test_canceled_ticket_refuses_a_comment(self):
        self.close(resolved=False)

        self.assertFalse(self.backend.comment_create_is_available(self.issue))
        self.assertEqual(self.post_comment().status_code, status.HTTP_400_BAD_REQUEST)

    def reopen(self):
        self.issue.status = "Open"
        self.issue.sync_resolution_date()
        self.issue.save()

    def test_reopening_lets_comments_through_again(self):
        self.close(resolved=True)
        self.reopen()

        self.assertTrue(self.backend.comment_create_is_available(self.issue))
        self.assertEqual(self.post_comment().status_code, status.HTTP_201_CREATED)

    def test_the_serializer_flag_follows_the_ticket(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.IssueFactory.get_url(self.issue)

        self.assertTrue(self.client.get(url).data["add_comment_is_available"])

        self.close(resolved=True)

        self.assertFalse(self.client.get(url).data["add_comment_is_available"])

    def test_editing_and_deleting_a_comment_stop_too(self):
        comment = factories.CommentFactory(issue=self.issue)

        self.assertTrue(self.backend.comment_update_is_available(comment))
        self.assertTrue(self.backend.comment_destroy_is_available(comment))

        self.close(resolved=False)
        comment.refresh_from_db()

        self.assertFalse(self.backend.comment_update_is_available(comment))
        self.assertFalse(self.backend.comment_destroy_is_available(comment))

    def test_new_attachments_stop_but_removing_one_does_not(self):
        attachment = factories.AttachmentFactory(issue=self.issue)

        self.assertTrue(self.backend.attachment_create_is_available(self.issue))

        self.close(resolved=True)
        attachment.refresh_from_db()

        self.assertFalse(self.backend.attachment_create_is_available(self.issue))
        # Deleting stays available: an erasure request arrives long after the
        # ticket is closed.
        self.assertTrue(self.backend.attachment_destroy_is_available(attachment))

    def test_the_caller_is_refused_too_not_just_staff(self):
        # The reported scenario, and a different branch through
        # `_comment_permission` than the staff path the other cases take. The
        # caller needs no project role to reach the validator.
        caller = self.issue.caller
        self.close(resolved=True)

        self.client.force_authenticate(caller)
        response = self.client.post(self.comment_url(), {"description": "still broken"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_ticket_closed_without_a_resolution_date_stays_open(self):
        # The registry cannot classify the status, so nothing marks the ticket
        # terminal. The gate deliberately fails open rather than freezing a
        # deployment out of tickets it cannot classify.
        self.issue.status = "Something the registry does not know"
        self.issue.save()

        self.assertTrue(self.backend.comment_create_is_available(self.issue))
