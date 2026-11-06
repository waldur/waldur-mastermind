"""A call's reviewer pool and its reviewer role are one list of reviewers."""

from rest_framework import test

from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, reviewer_membership
from waldur_mastermind.proposal.enums import ReviewerPoolInvitationStatuses

from . import factories

Statuses = ReviewerPoolInvitationStatuses


class ReviewerMembershipTest(test.APITestCase):
    def setUp(self):
        self.call = factories.CallFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.profile = factories.ReviewerProfileFactory()
        self.user = self.profile.user

    def entries(self):
        return models.CallReviewerPool.objects.filter(call=self.call)

    def test_accepting_the_pool_invitation_gives_the_role(self):
        entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.profile,
            invitation_status=Statuses.PENDING,
        )
        self.assertFalse(self.call.has_user(self.user, CallRole.REVIEWER))
        entry.invitation_status = Statuses.ACCEPTED
        entry.save()
        self.assertTrue(self.call.has_user(self.user, CallRole.REVIEWER))

    def test_the_role_puts_its_holder_in_the_pool(self):
        self.call.add_user(self.user, CallRole.REVIEWER, created_by=self.staff)
        entry = self.entries().get()
        self.assertEqual(entry.reviewer, self.profile)
        self.assertEqual(entry.invitation_status, Statuses.ACCEPTED)
        self.assertEqual(entry.override_reason, reviewer_membership.GRANTED_REASON)

    def test_the_role_accepts_a_pending_invitation(self):
        entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.profile,
            invitation_status=Statuses.PENDING,
        )
        self.call.add_user(self.user, CallRole.REVIEWER, created_by=self.staff)
        entry.refresh_from_db()
        self.assertEqual(entry.invitation_status, Statuses.ACCEPTED)
        self.assertEqual(self.entries().count(), 1)

    def test_someone_without_a_profile_joins_by_address(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.REVIEWER, created_by=self.staff)
        entry = self.entries().get()
        self.assertIsNone(entry.reviewer)
        self.assertEqual(entry.invited_user, user)
        self.assertEqual(entry.invited_email, user.email)

    def test_the_role_takes_up_an_invitation_sent_to_the_address(self):
        # One entry per address: a second one beside it would break
        # unique_call_invited_email.
        user = structure_factories.UserFactory()
        entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=None,
            invited_email=user.email,
            invitation_status=Statuses.PENDING,
        )
        self.call.add_user(user, CallRole.REVIEWER, created_by=self.staff)
        entry.refresh_from_db()
        self.assertEqual(self.entries().count(), 1)
        self.assertEqual(entry.invitation_status, Statuses.ACCEPTED)
        self.assertEqual(entry.invited_user, user)
        self.assertIsNone(entry.reviewer)

    def test_an_invitation_to_the_address_of_a_profile_is_bound_to_it(self):
        entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=None,
            invited_email=self.user.email.upper(),
            invitation_status=Statuses.PENDING,
        )
        self.call.add_user(self.user, CallRole.REVIEWER, created_by=self.staff)
        entry.refresh_from_db()
        self.assertEqual(self.entries().count(), 1)
        self.assertEqual(entry.invitation_status, Statuses.ACCEPTED)
        self.assertEqual(entry.reviewer, self.profile)
        self.assertEqual(entry.invited_user, self.user)

    def test_revoking_the_role_declines_the_entry_and_keeps_it(self):
        self.call.add_user(self.user, CallRole.REVIEWER, created_by=self.staff)
        self.call.remove_user(self.user, CallRole.REVIEWER, removed_by=self.staff)
        entry = self.entries().get()
        self.assertEqual(entry.invitation_status, Statuses.DECLINED)
        self.assertEqual(entry.decline_reason, reviewer_membership.REVOKED_REASON)
        self.assertNotIn(entry, models.CallReviewerPool.objects.eligible())
