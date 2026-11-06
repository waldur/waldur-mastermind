"""A reviewer's record is worked out from their reviews and assignments."""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.tools.proposals_reviewer.review_workload import (
    ReviewWorkloadTool,
)
from waldur_mastermind.proposal import enums, models, reviewer_stats
from waldur_mastermind.proposal.tests import factories

States = models.Review.States
Causes = enums.ReviewRejectionCauses
ItemStatuses = enums.AssignmentItemStatuses

START = datetime.datetime(2026, 3, 2, 9, 0, tzinfo=datetime.UTC)


def review(user, state, score=0, cause="", days=None):
    """A review by ``user``; a submitted one created at START and last changed
    ``days`` later."""
    made = factories.ReviewFactory(
        reviewer=user, state=state, summary_score=score, rejection_cause=cause
    )
    if days is not None:
        models.Review.objects.filter(pk=made.pk).update(
            created=START, modified=START + datetime.timedelta(days=days)
        )
    return made


def assignment(profile, item_status):
    return factories.AssignmentItemFactory(
        batch__reviewer_pool_entry__reviewer=profile, status=item_status
    )


class ReviewerStatsTest(test.APITestCase):
    def setUp(self):
        self.profile = factories.ReviewerProfileFactory()
        self.user = self.profile.user

    def record(self, profile):
        review(profile.user, States.SUBMITTED, score=4, days=2)
        review(profile.user, States.SUBMITTED, score=5, days=4)
        review(profile.user, States.IN_REVIEW)
        review(profile.user, States.REJECTED, cause=Causes.DECLINED)
        # Rejected before the cause was recorded: declined, as the review
        # progress report counts it.
        review(profile.user, States.REJECTED)
        review(profile.user, States.REJECTED, cause=Causes.EXPIRED)
        review(profile.user, States.REJECTED, cause=Causes.WITHDRAWN)
        assignment(profile, ItemStatuses.DECLINED)
        assignment(profile, ItemStatuses.EXPIRED)
        assignment(profile, ItemStatuses.PENDING)
        assignment(profile, ItemStatuses.COI_BLOCKED)

    def test_the_figures_come_from_the_reviews_and_assignments(self):
        self.record(self.profile)
        # Another reviewer's work is not counted.
        review(structure_factories.UserFactory(), States.SUBMITTED, score=1, days=9)

        self.assertEqual(
            reviewer_stats.of_profile(self.profile),
            {
                "total_reviews_completed": 2,
                "total_reviews_declined": 3,
                "total_reviews_timeout": 2,
                "average_review_time_days": 3.0,
                "average_score_given": 4.5,
                "last_review_date": timezone.localdate(
                    START + datetime.timedelta(days=4)
                ),
            },
        )

    def test_a_reviewer_with_no_reviews_has_no_averages(self):
        self.assertEqual(
            reviewer_stats.of_profile(self.profile),
            {
                "total_reviews_completed": 0,
                "total_reviews_declined": 0,
                "total_reviews_timeout": 0,
                "average_review_time_days": None,
                "average_score_given": None,
                "last_review_date": None,
            },
        )

    def test_an_unrated_submission_is_left_out_of_the_average_score(self):
        review(self.user, States.SUBMITTED, score=3, days=1)
        review(self.user, States.SUBMITTED, score=0, days=1)
        stats = reviewer_stats.of_profile(self.profile)
        self.assertEqual(stats["total_reviews_completed"], 2)
        self.assertEqual(stats["average_score_given"], 3.0)

    def test_the_profile_serves_its_figures(self):
        self.record(self.profile)
        self.client.force_authenticate(self.user)
        response = self.client.get(factories.ReviewerProfileFactory.get_list_url("me"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["stats"]["total_reviews_completed"], 2)
        self.assertEqual(response.data["stats"]["total_reviews_timeout"], 2)
        self.assertEqual(response.data["stats"]["average_score_given"], 4.5)

    def test_a_page_of_profiles_reads_the_reviews_once(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        url = factories.ReviewerProfileFactory.get_list_url()
        self.record(self.profile)

        def list_queries():
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            reading_reviews = [q for q in queries if '"proposal_review"' in q["sql"]]
            return len(reading_reviews), response.data

        one, _ = list_queries()
        for _ in range(2):
            self.record(factories.ReviewerProfileFactory())
        three, rows = list_queries()

        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [row["stats"]["total_reviews_completed"] for row in rows], [2, 2, 2]
        )
        self.assertEqual(one, three)

    def test_the_workload_assistant_reports_the_figures(self):
        self.record(self.profile)
        stats = ReviewWorkloadTool().execute(self.user, {})["data"]["stats"]
        self.assertEqual(
            (stats["total_completed"], stats["total_declined"]),
            (2, 3),
        )
        self.assertEqual(stats["total_timed_out"], 2)
        self.assertEqual(stats["avg_score"], 4.5)
