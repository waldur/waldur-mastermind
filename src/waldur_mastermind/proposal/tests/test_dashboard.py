import datetime

from constance.test.unittest import override_config
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.user_actions.providers import DASHBOARD_LIST_LIMIT
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories, fixtures


class ReviewerDashboardStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ReviewFactory.get_list_url(action="dashboard-stats")
        # fixture.review is an IN_REVIEW review by fixture.reviewer_1 on
        # fixture.proposal_submitted; fixture.round has cutoff in +10 days
        # but no review_duration_in_days set, so no deadline is exposed.

    def test_anonymous_user_cannot_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_zero_counts_for_user_with_no_reviews(self):
        unrelated_user = self.fixture.user
        self.client.force_authenticate(unrelated_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["assigned"], 0)
        self.assertEqual(response.data["pending"], 0)
        self.assertEqual(response.data["completed"], 0)
        self.assertEqual(list(response.data["deadlines"]), [])

    def test_counts_assigned_pending_and_completed(self):
        # fixture.review is one IN_REVIEW for reviewer_1
        # add a SUBMITTED review by same reviewer
        factories.ReviewFactory(
            proposal=self.fixture.proposal_submitted,
            reviewer=self.fixture.reviewer_1,
            state=models.Review.States.SUBMITTED,
        )
        # rejected one for another reviewer should not affect counts
        factories.ReviewFactory(
            proposal=self.fixture.proposal_submitted,
            reviewer=self.fixture.reviewer_2,
            state=models.Review.States.SUBMITTED,
        )

        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(self.url)
        self.assertEqual(response.data["assigned"], 2)
        self.assertEqual(response.data["pending"], 1)
        self.assertEqual(response.data["completed"], 1)

    def test_exposes_deadline_when_review_duration_is_set(self):
        round_obj = self.fixture.round
        round_obj.review_duration_in_days = 5
        round_obj.save()

        with freeze_time("2026-05-28T12:00:00Z"):
            review = factories.ReviewFactory(
                proposal=self.fixture.proposal_submitted,
                reviewer=self.fixture.reviewer_1,
                state=models.Review.States.IN_REVIEW,
            )

        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(self.url)
        deadlines = response.data["deadlines"]
        # Both reviews share the same round so both should appear with a
        # deadline = created + 5 days.
        self.assertEqual(len(deadlines), 2)
        expected_uuids = {str(review.uuid), str(self.fixture.review.uuid)}
        for item in deadlines:
            self.assertIn(str(item["uuid"]), expected_uuids)
            self.assertEqual(item["call_name"], self.fixture.call.name)
            self.assertIsNotNone(item["due_date"])

    def test_caps_deadlines_while_counts_stay_exact(self):
        # The endpoint is hit on every dashboard load, so the list is capped;
        # the counts are aggregated separately and must still cover everything.
        round_obj = self.fixture.round
        round_obj.review_duration_in_days = 5
        round_obj.save()
        extra = DASHBOARD_LIST_LIMIT + 3
        for _ in range(extra):
            factories.ReviewFactory(
                proposal=self.fixture.proposal_submitted,
                reviewer=self.fixture.reviewer_1,
                state=models.Review.States.IN_REVIEW,
            )

        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data["deadlines"]), DASHBOARD_LIST_LIMIT)
        # fixture.review is IN_REVIEW too, hence the +1.
        self.assertEqual(response.data["pending"], extra + 1)
        # The embedded list cannot be paginated, so it carries its own total —
        # otherwise a capped list is indistinguishable from a complete one.
        self.assertEqual(response.data["deadlines_total"], extra + 1)

    def test_deadlines_total_excludes_reviews_without_a_deadline(self):
        # `pending` is not a stand-in for the deadline count: a review whose
        # round sets no review duration is pending but never gets a deadline.
        round_obj = self.fixture.round
        round_obj.review_duration_in_days = None
        round_obj.save()

        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(self.url)
        self.assertEqual(response.data["pending"], 1)
        self.assertEqual(response.data["deadlines_total"], 0)

    def test_skips_deadline_when_review_duration_is_null(self):
        # fixture.round has review_duration_in_days=None by default
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(self.url)
        self.assertEqual(list(response.data["deadlines"]), [])

    def test_skips_deadline_when_review_duration_is_zero(self):
        # Review.review_end_date treats 0 like an unset duration: no deadline.
        self.fixture.round.review_duration_in_days = 0
        self.fixture.round.save()
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(self.url)
        self.assertEqual(list(response.data["deadlines"]), [])
        self.assertEqual(response.data["deadlines_total"], 0)


class CallManagerDashboardStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.manager_user = self.fixture.call_manager
        self.url = factories.CallFactory.get_protected_list_url(
            action="dashboard-stats"
        )

    def test_anonymous_user_cannot_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_zero_counts_for_user_without_managed_calls(self):
        unrelated_user = self.fixture.user
        self.client.force_authenticate(unrelated_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["pending_assessments"], 0)
        self.assertEqual(response.data["active_calls"], 0)
        self.assertEqual(response.data["reviews_due_soon"], 0)

    def test_counts_active_calls_managed_by_user(self):
        # fixture.call is ACTIVE; new_call (draft) is also managed but not active
        self.client.force_authenticate(self.manager_user)
        response = self.client.get(self.url)
        self.assertEqual(response.data["active_calls"], 1)

    def test_counts_pending_assessments_on_managed_calls(self):
        # fixture.proposal_submitted is SUBMITTED on fixture.round (in
        # fixture.call) — counts as pending assessment.
        self.client.force_authenticate(self.manager_user)
        response = self.client.get(self.url)
        self.assertEqual(response.data["pending_assessments"], 1)

    def test_counts_reviews_due_soon_on_managed_calls(self):
        round_obj = self.fixture.round
        round_obj.review_duration_in_days = 10
        round_obj.save()

        # Created 5 days ago: due in 5 days, inside the 7-day window.
        with freeze_time(timezone.now() - datetime.timedelta(days=5)):
            factories.ReviewFactory(
                proposal=self.fixture.proposal_submitted,
                reviewer=self.fixture.reviewer_2,
                state=models.Review.States.IN_REVIEW,
            )

        # fixture.review was created just now, so it is due in 10 days.
        self.client.force_authenticate(self.manager_user)
        response = self.client.get(self.url)
        self.assertEqual(response.data["reviews_due_soon"], 1)
        self.assertEqual(response.data["reviews_due_within_days"], 7)

    @override_config(PROPOSAL_DASHBOARD_REVIEWS_DUE_WITHIN_DAYS=14)
    def test_due_soon_window_is_configurable(self):
        round_obj = self.fixture.round
        round_obj.review_duration_in_days = 10
        round_obj.save()

        # fixture.review is due in 10 days: outside the default window, inside 14.
        self.client.force_authenticate(self.manager_user)
        response = self.client.get(self.url)
        self.assertEqual(response.data["reviews_due_soon"], 1)
        self.assertEqual(response.data["reviews_due_within_days"], 14)


class UpcomingDeadlinesTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.manager_user = self.fixture.call_manager
        self.url = (
            "http://testserver" + reverse("call-round-list") + "dashboard-deadlines/"
        )

    def test_anonymous_user_cannot_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_empty_for_user_without_managed_calls(self):
        unrelated_user = self.fixture.user
        self.client.force_authenticate(unrelated_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(list(response.data), [])

    def test_returns_rounds_in_calls_managed_by_user(self):
        # fixture.round has cutoff_time = now + 10 days
        self.client.force_authenticate(self.manager_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [row["uuid"] for row in response.data]
        self.assertIn(str(self.fixture.round.uuid), uuids)

    def test_excludes_rounds_that_already_closed(self):
        # Force fixture.round into the past
        round_obj = self.fixture.round
        round_obj.cutoff_time = timezone.now() - datetime.timedelta(days=1)
        round_obj.save()

        self.client.force_authenticate(self.manager_user)
        response = self.client.get(self.url)
        uuids = [row["uuid"] for row in response.data]
        self.assertNotIn(str(round_obj.uuid), uuids)

    def test_caps_number_of_returned_rounds(self):
        for _ in range(DASHBOARD_LIST_LIMIT + 3):
            factories.RoundFactory(
                call=self.fixture.call,
                cutoff_time=timezone.now() + datetime.timedelta(days=20),
            )

        self.client.force_authenticate(self.manager_user)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), DASHBOARD_LIST_LIMIT)

    def test_reports_the_true_total_and_serves_further_pages(self):
        extra = DASHBOARD_LIST_LIMIT + 3
        for _ in range(extra):
            factories.RoundFactory(
                call=self.fixture.call,
                cutoff_time=timezone.now() + datetime.timedelta(days=20),
            )

        self.client.force_authenticate(self.manager_user)
        response = self.client.get(self.url)
        # fixture.round is open too, hence the +1.
        total = extra + 1
        self.assertEqual(int(response["X-Result-Count"]), total)

        second_page = self.client.get(self.url, {"page": 2})
        self.assertEqual(len(second_page.data), total - DASHBOARD_LIST_LIMIT)
        # Still a bare array, so the response shape the SDK sees is unchanged.
        self.assertIsInstance(second_page.data, list)

    def test_head_returns_the_count_without_a_body(self):
        # The `_count` companion: paginated, so the HEAD operation is real and
        # the SDK method it generates is worth having.
        extra = DASHBOARD_LIST_LIMIT + 3
        for _ in range(extra):
            factories.RoundFactory(
                call=self.fixture.call,
                cutoff_time=timezone.now() + datetime.timedelta(days=20),
            )

        self.client.force_authenticate(self.manager_user)
        response = self.client.head(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # fixture.round is open too, hence the +1.
        self.assertEqual(int(response["X-Result-Count"]), extra + 1)

    def test_response_shape(self):
        self.client.force_authenticate(self.manager_user)
        response = self.client.get(self.url)
        if response.data:
            row = response.data[0]
            self.assertEqual(
                set(row.keys()),
                {
                    "uuid",
                    "call_uuid",
                    "call_name",
                    "round_name",
                    "due_date",
                },
            )


class SubmitterDashboardStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ProposalFactory.get_list_url(action="dashboard-stats")

    def test_anonymous_user_cannot_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_zero_counts_for_user_without_proposals(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in (
            "total",
            "draft",
            "submitted",
            "in_review",
            "accepted",
            "rejected",
            "canceled",
        ):
            self.assertEqual(response.data[field], 0)

    def test_counts_own_proposals_by_state(self):
        user = self.fixture.user
        states = [
            models.Proposal.States.DRAFT,
            models.Proposal.States.SUBMITTED,
            models.Proposal.States.IN_REVIEW,
            models.Proposal.States.ACCEPTED,
            models.Proposal.States.ACCEPTED,
            models.Proposal.States.REJECTED,
        ]
        for state in states:
            factories.ProposalFactory(
                round=self.fixture.round, created_by=user, state=state
            )

        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.data["total"], 6)
        self.assertEqual(response.data["draft"], 1)
        self.assertEqual(response.data["submitted"], 1)
        self.assertEqual(response.data["in_review"], 1)
        self.assertEqual(response.data["accepted"], 2)
        self.assertEqual(response.data["rejected"], 1)
        self.assertEqual(response.data["canceled"], 0)

    def test_other_users_proposals_are_not_counted(self):
        # The fixture's proposals belong to the owner, not fixture.user.
        self.fixture.proposal_submitted
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.data["total"], 0)


class ReviewDueWithinDaysFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.manager_user = self.fixture.call_manager
        self.url = factories.ReviewFactory.get_list_url()
        self.fixture.round.review_duration_in_days = 10
        self.fixture.round.save()

        # fixture.review is IN_REVIEW and created just now: due in 10 days.
        self.due_later = self.fixture.review
        with freeze_time(timezone.now() - datetime.timedelta(days=5)):
            # Due in 5 days.
            self.due_soon = factories.ReviewFactory(
                proposal=self.fixture.proposal_submitted,
                reviewer=self.fixture.reviewer_2,
                state=models.Review.States.IN_REVIEW,
            )
            # Also due in 5 days, but already handed in.
            self.submitted = factories.ReviewFactory(
                proposal=self.fixture.proposal_submitted,
                reviewer=self.fixture.reviewer_1,
                state=models.Review.States.SUBMITTED,
            )
        self.client.force_authenticate(self.manager_user)

    def list_uuids(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def test_returns_reviews_in_progress_due_within_the_window(self):
        self.assertEqual(self.list_uuids(due_within_days=7), {self.due_soon.uuid.hex})

    def test_wider_window_includes_later_deadlines(self):
        self.assertEqual(
            self.list_uuids(due_within_days=14),
            {self.due_soon.uuid.hex, self.due_later.uuid.hex},
        )

    def test_includes_reviews_already_past_their_deadline(self):
        # Until the hourly expiry task rejects it, a past-due review is the
        # most urgent one, so it stays in the list.
        with freeze_time(timezone.now() - datetime.timedelta(days=20)):
            past_due = factories.ReviewFactory(
                proposal=self.fixture.proposal_submitted,
                reviewer=self.fixture.reviewer_1,
                state=models.Review.States.IN_REVIEW,
            )
        self.assertIn(past_due.uuid.hex, self.list_uuids(due_within_days=7))

    def test_round_without_review_duration_has_no_deadline(self):
        for duration in (None, 0):
            with self.subTest(duration=duration):
                self.fixture.round.review_duration_in_days = duration
                self.fixture.round.save()
                self.assertEqual(self.list_uuids(due_within_days=7), set())

    def test_negative_window_is_rejected(self):
        response = self.client.get(self.url, {"due_within_days": -1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_managed_calls_only_matches_the_call_manager_dashboard(self):
        # Staff also see reviews on calls they do not manage. The card counts
        # only managed calls, so the list it opens has to be narrowed the same way.
        self.manager_user.is_staff = True
        self.manager_user.save()
        with freeze_time(timezone.now() - datetime.timedelta(days=5)):
            other_call_review = factories.ReviewFactory(
                proposal__round__review_duration_in_days=10,
                state=models.Review.States.IN_REVIEW,
            )
        stats = self.client.get(
            factories.CallFactory.get_protected_list_url(action="dashboard-stats")
        ).data
        days = stats["reviews_due_within_days"]

        self.assertIn(other_call_review.uuid.hex, self.list_uuids(due_within_days=days))
        self.assertEqual(
            self.list_uuids(due_within_days=days, managed_calls_only=True),
            {self.due_soon.uuid.hex},
        )
        self.assertEqual(stats["reviews_due_soon"], 1)
