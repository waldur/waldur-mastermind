from rest_framework import status, test

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories, fixtures


class StatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.manager = self.fixture.manager
        self.url_performance = factories.CallManagingOrganisationFactory.get_list_url(
            action="global_stats_performance"
        )
        self.url_review_progress = (
            factories.CallManagingOrganisationFactory.get_list_url(
                action="global_stats_review_progress"
            )
        )
        self.url_resource_demand = (
            factories.CallManagingOrganisationFactory.get_list_url(
                action="global_stats_resource_demand"
            )
        )

    def test_global_stats_performance(self):
        call = self.fixture.call
        self.client.force_authenticate(self.fixture.staff)

        # Ensure review is submitted and has a score
        review = self.fixture.review
        review.state = models.Review.States.SUBMITTED
        review.summary_score = 4
        review.save()

        response = self.client.get(self.url_performance)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Check if the call stats we expect are in the response
        call_stats = next(s for s in response.data if s["call_uuid"] == call.uuid.hex)
        self.assertEqual(
            call_stats["total_proposals"], 2
        )  # fixture.proposal and fixture.proposal_submitted
        self.assertEqual(call_stats["reviews_completed"], 1)
        self.assertEqual(call_stats["average_score"], 4.0)

    def test_global_stats_review_progress(self):
        self.client.force_authenticate(self.fixture.staff)

        # Ensure review is submitted
        review = self.fixture.review
        review.state = models.Review.States.SUBMITTED
        review.save()

        response = self.client.get(self.url_review_progress)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["reviewer_name"], self.fixture.reviewer_1.full_name
        )
        self.assertEqual(response.data[0]["completed"], 1)

    def test_global_stats_resource_demand(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url_resource_demand)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # fixture.offering is requested in call
        self.assertTrue(len(response.data) >= 1)
        offering_stats = next(
            s
            for s in response.data
            if s["offering_uuid"] == self.fixture.offering.uuid.hex
        )
        self.assertEqual(offering_stats["offering_name"], self.fixture.offering.name)

    def test_permissions(self):
        # User without permission should not see stats
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url_performance)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Call manager should NOT see global stats (only staff)
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url_performance)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Staff should see global stats
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url_performance)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
