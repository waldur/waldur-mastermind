from rest_framework import status
from rest_framework.test import APITestCase

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests import fixtures as proposal_fixtures


class ProposalReviewVisibilityTestCase(APITestCase):
    def setUp(self):
        self.fixture = proposal_fixtures.ProposalFixture()

        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = models.Proposal.States.ACCEPTED
        self.proposal.save()
        self.review = factories.ReviewFactory(proposal=self.proposal)

        self.proposal_submitter = self.proposal.created_by
        self.reviewer = self.review.reviewer
        self.call_manager = self.call.created_by

    def test_reviewer_identity_hidden_from_submitters(self):
        """Test that when reviewer_identity_visible_to_submitters=False, submitters see anonymous names"""
        # Set visibility to hide reviewer identity
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.reviews_visible_to_submitters = True
        self.call.save()

        self.review.state = models.Review.States.SUBMITTED
        self.review.save()

        self.client.force_authenticate(self.proposal_submitter)
        response = self.client.get(f"/api/proposal-reviews/{self.review.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("anonymous_reviewer_name", response.data)
        self.assertEqual(response.data["anonymous_reviewer_name"], "Reviewer 1")
        self.assertNotIn("reviewer_full_name", response.data)
        self.assertNotIn("reviewer_uuid", response.data)

    def test_reviewer_identity_visible_to_submitters(self):
        """Test that when reviewer_identity_visible_to_submitters=True, submitters see real names"""
        self.call.reviewer_identity_visible_to_submitters = True
        self.call.reviews_visible_to_submitters = True
        self.call.save()
        self.review.state = models.Review.States.SUBMITTED
        self.review.save()

        self.client.force_authenticate(self.proposal_submitter)
        response = self.client.get(f"/api/proposal-reviews/{self.review.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("reviewer_full_name", response.data)
        self.assertIn("reviewer_uuid", response.data)
        self.assertNotIn("anonymous_reviewer_name", response.data)

    def test_reviews_completely_hidden_from_submitters(self):
        """Test that when reviews_visible_to_submitters=False, submitters cannot access reviews at all"""
        self.call.reviews_visible_to_submitters = False
        self.call.save()
        self.review.state = models.Review.States.SUBMITTED
        self.review.save()

        self.client.force_authenticate(self.proposal_submitter)
        response = self.client.get(f"/api/proposal-reviews/{self.review.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_reviews_visible_to_submitters(self):
        """Test that when reviews_visible_to_submitters=True, submitters can access review instances"""
        self.call.reviews_visible_to_submitters = True
        self.call.save()
        self.review.state = models.Review.States.SUBMITTED
        self.review.save()

        self.client.force_authenticate(self.proposal_submitter)
        response = self.client.get(f"/api/proposal-reviews/{self.review.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("summary_score", response.data)
        self.assertIn("summary_public_comment", response.data)

    def test_call_managers_always_see_all_reviews(self):
        """Test that call managers see all reviews regardless of settings"""
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.reviews_visible_to_submitters = False
        self.call.save()

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(f"/api/proposal-reviews/{self.review.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("reviewer_full_name", response.data)
        self.assertIn("reviewer_uuid", response.data)
        self.assertIn("summary_private_comment", response.data)
        self.assertNotIn("anonymous_reviewer_name", response.data)

    def test_reviewers_see_their_own_reviews(self):
        """Test that reviewers can always see their own reviews"""
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.reviews_visible_to_submitters = False
        self.call.save()

        self.client.force_authenticate(self.reviewer)
        response = self.client.get(f"/api/proposal-reviews/{self.review.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("reviewer_full_name", response.data)
        self.assertIn("summary_private_comment", response.data)
        self.assertNotIn("anonymous_reviewer_name", response.data)

    def test_proposal_list_respects_review_visibility(self):
        """Test that proposal list endpoint respects review visibility settings"""
        self.call.reviews_visible_to_submitters = False
        self.call.save()
        self.review.state = models.Review.States.SUBMITTED
        self.review.save()

        self.client.force_authenticate(self.proposal_submitter)
        response = self.client.get(f"/api/proposal-proposals/{self.proposal.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        if "reviews" in response.data:
            self.assertEqual(len(response.data["reviews"]), 0)

    def test_anonymous_reviewer_numbering_consistency(self):
        """Test that anonymous reviewer numbering is consistent across requests"""
        second_review = factories.ReviewFactory(proposal=self.proposal)
        second_review.reviewer = self.fixture.user
        second_review.save()

        self.call.reviewer_identity_visible_to_submitters = False
        self.call.reviews_visible_to_submitters = True
        self.call.save()

        for review in [self.review, second_review]:
            review.state = models.Review.States.SUBMITTED
            review.save()

        self.client.force_authenticate(self.proposal_submitter)

        response1 = self.client.get(f"/api/proposal-reviews/{self.review.uuid.hex}/")
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        first_reviewer_name = response1.data["anonymous_reviewer_name"]

        response2 = self.client.get(f"/api/proposal-reviews/{second_review.uuid.hex}/")
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        second_reviewer_name = response2.data["anonymous_reviewer_name"]

        self.assertIn("Reviewer", first_reviewer_name)
        self.assertIn("Reviewer", second_reviewer_name)
        self.assertNotEqual(first_reviewer_name, second_reviewer_name)

    def test_staff_users_see_everything(self):
        """Test that staff users see all review information regardless of settings"""
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.reviews_visible_to_submitters = False
        self.call.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(f"/api/proposal-reviews/{self.review.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("reviewer_full_name", response.data)
        self.assertIn("reviewer_uuid", response.data)
        self.assertIn("summary_private_comment", response.data)
        self.assertNotIn("anonymous_reviewer_name", response.data)

    def test_non_submitted_reviews_hidden_from_submitters(self):
        """Test that only submitted reviews are visible to submitters"""
        self.call.reviews_visible_to_submitters = True
        self.call.save()
        self.review.state = models.Review.States.IN_REVIEW  # Not submitted
        self.review.save()

        self.client.force_authenticate(self.proposal_submitter)
        response = self.client.get(f"/api/proposal-reviews/{self.review.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
