"""A reviewer sending back a review they accepted says why, and the call's
managers read it: in the review and in the email telling them."""

import datetime
from unittest import mock

from ddt import data, ddt
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import enums, models, tasks
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures

REASON = "Conflict of interest with the PI"
States = models.Review.States


class DeclineReasonTestMixin:
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review
        self.call_manager = self.fixture.call_manager
        self.reject_url = factories.ReviewFactory.get_url(self.review, "reject")
        self.review_url = factories.ReviewFactory.get_url(self.review)

    def reject(self, user, payload=None, **kwargs):
        self.client.force_authenticate(user)
        with mock.patch.object(
            tasks.notify_call_managers_about_rejected_review, "delay"
        ) as notify:
            response = self.client.post(self.reject_url, payload, **kwargs)
        self.review.refresh_from_db()
        self.notify = notify
        return response


@ddt
class RejectWithReasonTest(DeclineReasonTestMixin, test.APITestCase):
    @data("reviewer_1", "staff")
    def test_the_reason_is_kept_on_the_review(self, user):
        response = self.reject(
            getattr(self.fixture, user), {"reason": REASON}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.review.state, States.REJECTED)
        self.assertEqual(
            self.review.rejection_cause, enums.ReviewRejectionCauses.DECLINED
        )
        self.assertEqual(self.review.decline_reason, REASON)
        self.notify.assert_called_once_with(self.review.uuid)

    @data(None, {}, {"reason": ""}, {"reason": "   "})
    def test_without_a_reason_the_review_is_rejected_as_before(self, payload):
        response = self.reject(self.fixture.reviewer_1, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.review.state, States.REJECTED)
        self.assertEqual(
            self.review.rejection_cause, enums.ReviewRejectionCauses.DECLINED
        )
        self.assertEqual(self.review.decline_reason, "")
        self.notify.assert_called_once_with(self.review.uuid)

    def test_a_reason_longer_than_the_limit_is_refused(self):
        response = self.reject(
            self.fixture.reviewer_1, {"reason": "x" * 2001}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reason", response.data)
        self.assertEqual(self.review.state, States.IN_REVIEW)
        self.assertEqual(self.review.decline_reason, "")
        self.notify.assert_not_called()

    def test_a_reason_at_the_limit_is_kept(self):
        response = self.reject(
            self.fixture.reviewer_1, {"reason": "x" * 2000}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(self.review.decline_reason), 2000)

    def test_a_review_no_longer_in_review_is_not_rejected_again(self):
        self.reject(self.fixture.reviewer_1, {"reason": REASON}, format="json")
        response = self.reject(
            self.fixture.reviewer_1, {"reason": "Another reason"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.review.decline_reason, REASON)
        self.notify.assert_not_called()

    def test_the_event_does_not_carry_the_reason(self):
        self.reject(self.fixture.reviewer_1, {"reason": REASON}, format="json")
        events = Event.objects.filter(event_type="review_rejected")
        self.assertTrue(events.exists())
        for event in events:
            self.assertNotIn(REASON, event.message)
            self.assertNotIn(REASON, str(event.context))


@ddt
class RejectPermissionTest(DeclineReasonTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.review.proposal.round.call.add_user(self.call_manager, CallRole.MANAGER)

    def test_the_call_manager_cannot_reject_for_the_reviewer(self):
        response = self.reject(self.call_manager, {"reason": REASON}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.review.state, States.IN_REVIEW)
        self.assertEqual(self.review.decline_reason, "")

    @data("reviewer_2", "proposal_submitted_creator")
    def test_others_cannot_reject(self, user):
        response = self.reject(
            getattr(self.fixture, user), {"reason": REASON}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.review.state, States.IN_REVIEW)
        self.assertEqual(self.review.decline_reason, "")


class OtherClosuresHaveNoReasonTest(DeclineReasonTestMixin, test.APITestCase):
    def test_an_expired_review_has_no_reason(self):
        review_round = self.review.proposal.round
        review_round.review_duration_in_days = 5
        review_round.save()

        with freeze_time(timezone.now() + datetime.timedelta(days=10)):
            tasks.expired_reviews_should_be_cancelled()

        self.review.refresh_from_db()
        self.assertEqual(self.review.state, States.REJECTED)
        self.assertEqual(
            self.review.rejection_cause, enums.ReviewRejectionCauses.EXPIRED
        )
        self.assertEqual(self.review.decline_reason, "")

    def test_a_review_withdrawn_on_joining_the_team_has_no_reason(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.review.proposal.add_user(
                self.fixture.reviewer_1,
                ProposalRole.MEMBER,
                created_by=self.fixture.staff,
            )

        self.review.refresh_from_db()
        self.assertEqual(self.review.state, States.REJECTED)
        self.assertEqual(
            self.review.rejection_cause, enums.ReviewRejectionCauses.WITHDRAWN
        )
        self.assertEqual(self.review.decline_reason, "")


@ddt
class DeclineReasonInTheApiTest(DeclineReasonTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.review.proposal.round.call.add_user(self.call_manager, CallRole.MANAGER)

    def get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.review_url)

    @data("reviewer_1", "call_manager", "staff")
    def test_the_reviewer_the_call_managers_and_staff_read_the_reason(self, user):
        self.reject(self.fixture.reviewer_1, {"reason": REASON}, format="json")
        response = self.get(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["decline_reason"], REASON)
        self.assertEqual(
            response.data["rejection_cause"], enums.ReviewRejectionCauses.DECLINED
        )

    def test_the_applicant_side_never_reads_the_reason(self):
        # Submitters read only submitted reviews; whatever a review holds,
        # the reason is not for them.
        call = self.review.proposal.round.call
        call.reviews_visible_to_submitters = True
        call.reviewer_identity_visible_to_submitters = True
        call.save()
        proposal = self.review.proposal
        proposal.state = ProposalStates.ACCEPTED
        proposal.save()
        models.Review.objects.filter(pk=self.review.pk).update(
            state=States.SUBMITTED, decline_reason=REASON
        )

        response = self.get(self.fixture.proposal_submitted_creator)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("decline_reason", response.data)
        self.assertNotIn("summary_private_comment", response.data)

        response = self.get(self.call_manager)
        self.assertEqual(response.data["decline_reason"], REASON)

    def test_a_patch_sets_neither_the_reason_nor_the_cause(self):
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.patch(
            self.review_url,
            {
                "summary_score": 3,
                "decline_reason": REASON,
                "rejection_cause": enums.ReviewRejectionCauses.EXPIRED,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.review.refresh_from_db()
        self.assertEqual(self.review.summary_score, 3)
        self.assertEqual(self.review.decline_reason, "")
        self.assertEqual(self.review.rejection_cause, "")


@override_settings(task_always_eager=True)
class DeclineReasonMailTest(DeclineReasonTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.review.proposal.round.call.add_user(self.call_manager, CallRole.MANAGER)
        structure_factories.NotificationFactory(key="proposal.review_rejected")

    def send_back(self, payload):
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.post(self.reject_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.call_manager.email])
        message = mail.outbox[0]
        html = message.alternatives[0][0] if message.alternatives else ""
        return message.body, html

    def test_the_mail_gives_the_reason(self):
        body, html = self.send_back({"reason": REASON})
        self.assertIn(f"Reason: {REASON}", body)
        self.assertIn(f"Reason: {REASON}", html)

    def test_a_multi_line_reason_keeps_its_lines_in_the_html(self):
        _, html = self.send_back({"reason": "First line.\nSecond line."})
        self.assertIn("First line.<br>Second line.", html)

    def test_no_reason_line_without_a_reason(self):
        body, html = self.send_back({})
        self.assertNotIn("Reason", body)
        self.assertNotIn("Reason", html)
        self.assertIn("Rejected date:", body)
