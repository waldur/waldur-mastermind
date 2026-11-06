"""Who may answer, and who sees, a reviewer pool invitation."""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.enums import ReviewerPoolInvitationStatuses

from . import factories


def _token_url(entry, action):
    return (
        f"http://testserver/api/reviewer-invitations/{entry.invitation_token}/{action}/"
    )


class InvitationToUserWithoutProfileTest(test.APITestCase):
    """An invitation sent to an account that has no reviewer profile yet."""

    def setUp(self):
        self.invitee = structure_factories.UserFactory(email="invitee@example.com")
        self.entry = factories.CallReviewerPoolFactory(
            reviewer=None,
            invited_user=self.invitee,
            invited_email="invitee@example.com",
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.other = factories.ReviewerProfileFactory(is_published=True).user

    def test_another_reviewer_cannot_accept_it_by_token(self):
        self.client.force_authenticate(self.other)

        response = self.client.post(_token_url(self.entry, "accept"), {})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.entry.refresh_from_db()
        self.assertEqual(
            self.entry.invitation_status, ReviewerPoolInvitationStatuses.PENDING
        )
        self.assertIsNone(self.entry.reviewer)

    def test_accepting_by_token_requires_sign_in(self):
        response = self.client.post(_token_url(self.entry, "accept"), {})

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_invitee_accepts_it_by_token(self):
        profile = factories.ReviewerProfileFactory(user=self.invitee, is_published=True)
        self.client.force_authenticate(self.invitee)

        response = self.client.post(_token_url(self.entry, "accept"), {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.entry.refresh_from_db()
        self.assertEqual(
            self.entry.invitation_status, ReviewerPoolInvitationStatuses.ACCEPTED
        )
        self.assertEqual(self.entry.reviewer, profile)

    def test_another_user_cannot_decline_it_by_token(self):
        self.client.force_authenticate(self.other)

        response = self.client.post(_token_url(self.entry, "decline"), {})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.entry.refresh_from_db()
        self.assertEqual(
            self.entry.invitation_status, ReviewerPoolInvitationStatuses.PENDING
        )

    def test_invitee_sees_it_in_their_invitations(self):
        self.client.force_authenticate(self.invitee)

        response = self.client.get(
            factories.CallReviewerPoolFactory.get_list_url(),
            {"my_invitations": True},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([e["uuid"] for e in response.data], [self.entry.uuid.hex])


class InvitationToReviewerDeclineTest(test.APITestCase):
    def setUp(self):
        self.entry = factories.CallReviewerPoolFactory(
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

    def test_anonymous_decline_by_token_is_refused(self):
        response = self.client.post(
            _token_url(self.entry, "decline"), {"reason": "Forwarded."}
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.entry.refresh_from_db()
        self.assertEqual(
            self.entry.invitation_status, ReviewerPoolInvitationStatuses.PENDING
        )

    def test_another_user_cannot_decline_by_token(self):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.post(
            _token_url(self.entry, "decline"), {"reason": "Forwarded."}
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.entry.refresh_from_db()
        self.assertEqual(
            self.entry.invitation_status, ReviewerPoolInvitationStatuses.PENDING
        )


class InvitationToUnknownAddressTest(test.APITestCase):
    """An invitation sent to an address with no account behind it."""

    def setUp(self):
        self.entry = factories.CallReviewerPoolFactory(
            reviewer=None,
            invited_email="Newcomer@Example.com",
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.newcomer = structure_factories.UserFactory(email="newcomer@example.com")

    def test_user_with_the_address_in_another_case_sees_it(self):
        self.client.force_authenticate(self.newcomer)

        response = self.client.get(
            factories.CallReviewerPoolFactory.get_list_url(),
            {"my_invitations": True},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([e["uuid"] for e in response.data], [self.entry.uuid.hex])

    def test_user_with_the_address_in_another_case_accepts_it_from_the_pool(self):
        factories.ReviewerProfileFactory(user=self.newcomer, is_published=True)
        self.client.force_authenticate(self.newcomer)

        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_with_the_address_in_another_case_declines_it_by_token(self):
        self.client.force_authenticate(self.newcomer)

        response = self.client.post(
            _token_url(self.entry, "decline"), {"reason": "Not my field."}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.entry.refresh_from_db()
        self.assertEqual(
            self.entry.invitation_status, ReviewerPoolInvitationStatuses.DECLINED
        )

    def test_user_with_another_address_cannot_decline_it_by_token(self):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.post(_token_url(self.entry, "decline"), {})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_with_another_address_does_not_see_it(self):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.get(
            factories.CallReviewerPoolFactory.get_list_url(),
            {"my_invitations": True},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])


class UserWithoutEmailTest(test.APITestCase):
    def setUp(self):
        # Entries invited by reviewer profile carry no address at all
        self.entry = factories.CallReviewerPoolFactory(
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.assertEqual(self.entry.invited_email, "")
        self.user = structure_factories.UserFactory(email="")

    def test_matches_no_entry_without_an_address(self):
        self.client.force_authenticate(self.user)

        listed = self.client.get(factories.CallReviewerPoolFactory.get_list_url())
        mine = self.client.get(
            factories.CallReviewerPoolFactory.get_list_url(),
            {"my_invitations": True},
        )

        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(listed.data, [])
        self.assertEqual(mine.data, [])

    def test_cannot_decline_an_entry_without_an_address(self):
        self.client.force_authenticate(self.user)

        response = self.client.post(_token_url(self.entry, "decline"), {})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class TwoAccountsOnOneAddressTest(test.APITestCase):
    """Addresses are not unique to an account; the invitation picked one of them."""

    def setUp(self):
        self.picked = structure_factories.UserFactory(email="twin@example.com")
        self.other_account = structure_factories.UserFactory(email="Twin@Example.com")
        self.profile = factories.ReviewerProfileFactory(
            user=self.other_account, is_published=True
        )
        self.entry = factories.CallReviewerPoolFactory(
            reviewer=None,
            invited_user=self.picked,
            invited_email="twin@example.com",
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.client.force_authenticate(self.other_account)

    def test_other_account_of_the_address_sees_it(self):
        response = self.client.get(
            factories.CallReviewerPoolFactory.get_list_url(),
            {"my_invitations": True},
        )

        self.assertEqual([e["uuid"] for e in response.data], [self.entry.uuid.hex])

    def test_other_account_of_the_address_accepts_it_by_token(self):
        response = self.client.post(_token_url(self.entry, "accept"), {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.reviewer, self.profile)
        self.assertEqual(self.entry.invited_user, self.other_account)

    def test_other_account_of_the_address_declines_it_by_token(self):
        response = self.client.post(
            _token_url(self.entry, "decline"), {"reason": "Not my field."}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class UnknownAddressAcceptTest(test.APITestCase):
    def setUp(self):
        self.entry = factories.CallReviewerPoolFactory(
            reviewer=None,
            invited_email="newcomer@example.com",
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

    def test_user_with_another_address_cannot_accept_it_by_token(self):
        stranger = factories.ReviewerProfileFactory(is_published=True).user
        self.client.force_authenticate(stranger)

        response = self.client.post(_token_url(self.entry, "accept"), {})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.reviewer)

    def test_invitee_who_registered_with_the_address_accepts_it_by_token(self):
        newcomer = structure_factories.UserFactory(email="NEWCOMER@example.com")
        profile = factories.ReviewerProfileFactory(user=newcomer, is_published=True)
        self.client.force_authenticate(newcomer)

        response = self.client.post(_token_url(self.entry, "accept"), {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.reviewer, profile)

    def test_addresses_compare_as_the_database_does(self):
        # ``casefold`` would equate these; ``__iexact`` does not, so neither
        # may the ownership check.
        self.entry.invited_email = "strasse@example.com"
        self.entry.save()
        user = structure_factories.UserFactory(email="straße@example.com")

        self.assertFalse(self.entry.is_addressed_to(user))
