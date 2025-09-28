from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.proposal.tests import factories, fixtures


@ddt
class ProposalFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ProposalFactory.get_list_url()

    @data("staff", "call_manager")
    def test_proposals_can_be_ordered_by_slug(self, user):
        """Test that proposals can be ordered by slug field."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        response = self.client.get(self.url, {"o": "slug"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url, {"o": "-slug"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@ddt
class ProposalReviewSerializerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ReviewFactory.get_list_url()

    @data("staff", "owner", "customer_support")
    def test_review_response_includes_slug_fields(self, user):
        """Test that review API response includes slug fields for proposal, round, and call."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        if response.data:
            review_data = response.data[0]

            # Check that slug fields are present in the response
            self.assertIn("proposal_slug", review_data)
            self.assertIn("round_slug", review_data)
            self.assertIn("call_slug", review_data)

            # Check that slug values match the actual slugs from the objects
            self.assertEqual(
                review_data["proposal_slug"], self.fixture.review.proposal.slug
            )
            self.assertEqual(
                review_data["round_slug"], self.fixture.review.proposal.round.slug
            )
            self.assertEqual(
                review_data["call_slug"], self.fixture.review.proposal.round.call.slug
            )
