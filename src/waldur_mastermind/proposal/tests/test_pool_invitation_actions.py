"""Tests for CallReviewerPool accept/decline actions."""

from datetime import timedelta

from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.enums import ReviewerPoolInvitationStatuses

from . import factories


class PoolInvitationAcceptTest(test.APITransactionTestCase):
    """Tests for the accept action on CallReviewerPoolViewSet."""

    def setUp(self):
        self.call = factories.CallFactory()
        # Create a user with a published reviewer profile
        self.reviewer_user = structure_factories.UserFactory()
        self.reviewer_profile = factories.ReviewerProfileFactory(
            user=self.reviewer_user,
            is_published=True,
        )
        # Create pending pool invitation
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer_profile,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

    def test_reviewer_can_accept_own_invitation(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pool_entry.refresh_from_db()
        self.assertEqual(
            self.pool_entry.invitation_status, ReviewerPoolInvitationStatuses.ACCEPTED
        )
        self.assertIsNotNone(self.pool_entry.response_date)

    def test_cannot_accept_already_accepted_invitation(self):
        self.pool_entry.invitation_status = ReviewerPoolInvitationStatuses.ACCEPTED
        self.pool_entry.save()

        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_accept_declined_invitation(self):
        self.pool_entry.invitation_status = ReviewerPoolInvitationStatuses.DECLINED
        self.pool_entry.save()

        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_accept_expired_invitation(self):
        self.pool_entry.invitation_expires_at = timezone.now() - timedelta(days=1)
        self.pool_entry.save()

        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_user_cannot_accept_invitation(self):
        """Other users get 404 (not 403) since they can't see the invitation."""
        other_user = structure_factories.UserFactory()

        self.client.force_authenticate(other_user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_user_cannot_accept_invitation(self):
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PoolInvitationAcceptByEmailTest(test.APITransactionTestCase):
    """Tests for accepting invitations sent to email (no reviewer profile linked)."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.user = structure_factories.UserFactory(email="reviewer@example.com")
        # Create pool entry with email invitation (no reviewer profile linked)
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=None,
            invited_email="reviewer@example.com",
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

    def test_user_without_profile_cannot_accept(self):
        """User must have a published reviewer profile to accept."""
        self.client.force_authenticate(self.user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("profile_required", str(response.data))

    def test_user_with_unpublished_profile_cannot_accept(self):
        """User must publish their profile to accept."""
        factories.ReviewerProfileFactory(user=self.user, is_published=False)

        self.client.force_authenticate(self.user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("profile_not_published", str(response.data))

    def test_user_with_published_profile_can_accept(self):
        """User with published profile can accept email invitation."""
        profile = factories.ReviewerProfileFactory(user=self.user, is_published=True)

        self.client.force_authenticate(self.user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pool_entry.refresh_from_db()
        self.assertEqual(
            self.pool_entry.invitation_status, ReviewerPoolInvitationStatuses.ACCEPTED
        )
        # Profile should be linked
        self.assertEqual(self.pool_entry.reviewer, profile)
        self.assertEqual(self.pool_entry.invited_user, self.user)


class PoolInvitationDeclineTest(test.APITransactionTestCase):
    """Tests for the decline action on CallReviewerPoolViewSet."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.reviewer_user = structure_factories.UserFactory()
        self.reviewer_profile = factories.ReviewerProfileFactory(
            user=self.reviewer_user,
            is_published=True,
        )
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer_profile,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

    def test_reviewer_can_decline_own_invitation(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(
                self.pool_entry, action="decline"
            ),
            {"reason": "Too busy"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pool_entry.refresh_from_db()
        self.assertEqual(
            self.pool_entry.invitation_status, ReviewerPoolInvitationStatuses.DECLINED
        )
        self.assertEqual(self.pool_entry.decline_reason, "Too busy")
        self.assertIsNotNone(self.pool_entry.response_date)

    def test_cannot_decline_already_accepted_invitation(self):
        self.pool_entry.invitation_status = ReviewerPoolInvitationStatuses.ACCEPTED
        self.pool_entry.save()

        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(
                self.pool_entry, action="decline"
            ),
            {"reason": "Changed my mind"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_user_cannot_decline_invitation(self):
        """Other users get 404 (not 403) since they can't see the invitation."""
        other_user = structure_factories.UserFactory()

        self.client.force_authenticate(other_user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(
                self.pool_entry, action="decline"
            ),
            {"reason": "Not interested"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_user_cannot_decline_invitation(self):
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(
                self.pool_entry, action="decline"
            ),
            {"reason": "Not interested"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PoolInvitationTokenHidingTest(test.APITransactionTestCase):
    """Tests that invitation_token is hidden from authenticated responses."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.reviewer_user = structure_factories.UserFactory()
        self.reviewer_profile = factories.ReviewerProfileFactory(
            user=self.reviewer_user,
            is_published=True,
        )
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer_profile,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

    def test_token_hidden_in_list_response(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.get(
            factories.CallReviewerPoolFactory.get_list_url(),
            {"my_invitations": "true"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)
        # Token should not be in response for authenticated users
        self.assertNotIn("invitation_token", response.data[0])

    def test_token_hidden_in_detail_response(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.get(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("invitation_token", response.data)
