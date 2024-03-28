from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class ReviewGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ReviewFactory.get_list_url()

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_review_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data("user", "proposal_submitted_creator", "reviewer_2")
    def test_review_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(len(response.json()))

    @data("proposal_submitted_creator")
    def test_submitted_review_should_be_visible(self, user):
        self.fixture.review.state = models.Review.States.SUBMITTED
        self.fixture.review.save()
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))


@ddt
class ReviewCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ReviewFactory.get_list_url()

    @data("staff")
    def test_user_can_add(self, user):
        response = self.create(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Review.objects.filter(uuid=response.data["uuid"]).exists()
        )

    @data(
        "owner",
        "customer_support",
        "user",
    )
    def test_user_cannot_add(self, user):
        response = self.create(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create(self, user, **kwargs):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "proposal": factories.ProposalFactory.get_url(
                self.fixture.proposal_submitted
            ),
            "reviewer": structure_factories.UserFactory.get_url(
                self.fixture.reviewer_1
            ),
        }
        payload.update(kwargs)

        return self.client.post(self.url, payload)


@ddt
class ReviewUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review
        self.url = factories.ReviewFactory.get_url(self.review)

    @data("staff", "reviewer_1")
    def test_user_can_update(self, user):
        response = self.update(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("proposal_submitted_creator", "reviewer_2")
    def test_user_can_not_update(self, user):
        response = self.update(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def update(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "summary_score": 5,
        }
        response = self.client.patch(self.url, payload)
        self.review.refresh_from_db()
        return response


@ddt
class ReviewDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review
        self.url = factories.ReviewFactory.get_url(self.review)

    @data(
        "staff",
    )
    def test_user_can_delete(self, user):
        response = self.run_delete(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data(
        "owner",
        "customer_support",
        "proposal_submitted_creator",
    )
    def test_customer_user_can_not_delete(self, user):
        response = self.run_delete(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def run_delete(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.delete(self.url)


@ddt
class ActionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review
        self.url_accept = factories.ReviewFactory.get_url(self.review, "accept")

    @data(
        "staff",
        "reviewer_1",
    )
    def test_user_can_accept(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url_accept)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertTrue(self.review.state, models.Review.States.IN_REVIEW)

    @data(
        "owner",
        "customer_support",
    )
    def test_user_can_not_accept(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url_accept)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ReviewerGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.round_2 = fixtures.ProposalFixture().round
        self.url = factories.RoundFactory.get_own_url(
            self.fixture.round, action="reviewers"
        )
        self.url_2 = factories.RoundFactory.get_own_url(
            self.round_2, action="reviewers"
        )

    @data(
        "staff",
    )
    def test_reviewers_counters_are_zero_for_unrelated_proposals(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))
        self.assertEqual(response.data[0]["in_review_proposals"], 0)
        self.assertEqual(response.data[0]["rejected_proposals"], 0)
        self.assertEqual(response.data[0]["accepted_proposals"], 0)

    @data(
        "staff",
    )
    def test_reviewers_counters_are_zero_for_unrelated_rounds(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url_2)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))
        self.assertEqual(response.data[0]["in_review_proposals"], 0)
        self.assertEqual(response.data[0]["rejected_proposals"], 0)
        self.assertEqual(response.data[0]["accepted_proposals"], 0)

    @data(
        "staff",
    )
    def test_reviewers_counter_should_be_visible(self, user):
        self.fixture.proposal.state = models.Proposal.States.IN_REVIEW
        self.fixture.review.proposal = self.fixture.proposal
        self.fixture.review.save()
        self.fixture.proposal.save()
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))
        self.assertEqual(response.data[0]["in_review_proposals"], 1)
