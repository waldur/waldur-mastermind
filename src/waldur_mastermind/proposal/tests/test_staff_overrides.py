from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    COIStatuses,
    COITypes,
    ReviewerPoolInvitationStatuses,
)

from . import factories


class BaseStaffOverrideTest(test.APITestCase):
    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)

        self.call = factories.CallFactory()
        self.call_managing_org = self.call.manager

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call_manager = structure_factories.UserFactory()
        self.call.add_user(self.call_manager, CallRole.MANAGER)
        self.call_managing_org.add_user(self.call_manager, CallRole.MANAGER)

        self.reviewer_profile = factories.ReviewerProfileFactory()
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer_profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )

        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)


class AutoUnblockOnCOIDismissTest(BaseStaffOverrideTest):
    def _create_coi_and_blocked_item(self):
        """Create a COI record and a blocked assignment item linked to it."""
        coi = factories.ConflictOfInterestFactory(
            reviewer=self.reviewer_profile,
            proposal=self.proposal,
            call=self.call,
            status=COIStatuses.PENDING,
        )

        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.pool_entry,
            status=AssignmentBatchStatuses.SENT,
        )
        item = factories.AssignmentItemFactory(
            batch=batch,
            proposal=self.proposal,
            status=AssignmentItemStatuses.COI_BLOCKED,
            has_coi=True,
        )
        item.coi_records.add(coi)
        return coi, item

    def test_dismiss_coi_unblocks_related_assignment(self):
        coi, item = self._create_coi_and_blocked_item()

        self.client.force_authenticate(self.call_manager)
        url = factories.ConflictOfInterestFactory.get_url(coi, action="dismiss")
        resp = self.client.post(
            url,
            {
                "status": "dismissed",
                "review_notes": "False positive",
            },
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.status, AssignmentItemStatuses.PENDING)
        self.assertFalse(item.has_coi)

    def test_waive_coi_unblocks_related_assignment(self):
        coi, item = self._create_coi_and_blocked_item()

        self.client.force_authenticate(self.call_manager)
        url = factories.ConflictOfInterestFactory.get_url(coi, action="waive")
        resp = self.client.post(
            url,
            {
                "status": "waived",
                "review_notes": "Managed",
                "management_plan": "Will use independent oversight.",
            },
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.status, AssignmentItemStatuses.PENDING)
        self.assertFalse(item.has_coi)

    def test_dismiss_does_not_unblock_if_other_active_coi_remains(self):
        coi1 = factories.ConflictOfInterestFactory(
            reviewer=self.reviewer_profile,
            proposal=self.proposal,
            call=self.call,
            status=COIStatuses.PENDING,
            coi_type=COITypes.INST_SAME,
        )
        coi2 = factories.ConflictOfInterestFactory(
            reviewer=self.reviewer_profile,
            proposal=self.proposal,
            call=self.call,
            status=COIStatuses.PENDING,
            coi_type=COITypes.FIN_DIRECT,
        )

        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.pool_entry,
            status=AssignmentBatchStatuses.SENT,
        )
        item = factories.AssignmentItemFactory(
            batch=batch,
            proposal=self.proposal,
            status=AssignmentItemStatuses.COI_BLOCKED,
            has_coi=True,
        )
        item.coi_records.add(coi1, coi2)

        self.client.force_authenticate(self.call_manager)
        url = factories.ConflictOfInterestFactory.get_url(coi1, action="dismiss")
        resp = self.client.post(
            url,
            {
                "status": "dismissed",
                "review_notes": "False positive",
            },
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        # Should still be blocked because coi2 is still pending
        self.assertEqual(item.status, AssignmentItemStatuses.COI_BLOCKED)
        self.assertTrue(item.has_coi)


class ForceUnblockTest(BaseStaffOverrideTest):
    def _create_blocked_item(self):
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.pool_entry,
            status=AssignmentBatchStatuses.SENT,
        )
        item = factories.AssignmentItemFactory(
            batch=batch,
            proposal=self.proposal,
            status=AssignmentItemStatuses.COI_BLOCKED,
            has_coi=True,
        )
        return item

    def test_manager_can_force_unblock_coi_blocked_item(self):
        item = self._create_blocked_item()

        self.client.force_authenticate(self.call_manager)
        url = factories.AssignmentItemFactory.get_url(item, action="force-unblock")
        resp = self.client.post(url, {"override_reason": "Reviewed and cleared."})

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.status, AssignmentItemStatuses.PENDING)
        self.assertFalse(item.has_coi)
        self.assertEqual(item.override_reason, "Reviewed and cleared.")
        self.assertEqual(item.overridden_by, self.call_manager)
        self.assertIsNotNone(item.overridden_at)

    def test_force_unblock_requires_reason(self):
        item = self._create_blocked_item()

        self.client.force_authenticate(self.call_manager)
        url = factories.AssignmentItemFactory.get_url(item, action="force-unblock")
        resp = self.client.post(url, {})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_force_unblock_rejects_non_blocked_item(self):
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.pool_entry,
            status=AssignmentBatchStatuses.SENT,
        )
        item = factories.AssignmentItemFactory(
            batch=batch,
            proposal=self.proposal,
            status=AssignmentItemStatuses.PENDING,
        )

        self.client.force_authenticate(self.call_manager)
        url = factories.AssignmentItemFactory.get_url(item, action="force-unblock")
        resp = self.client.post(url, {"override_reason": "Some reason"})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class ForceAcceptPoolTest(BaseStaffOverrideTest):
    def test_manager_can_force_accept_pending_invitation(self):
        pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=factories.ReviewerProfileFactory(),
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

        self.client.force_authenticate(self.call_manager)
        url = factories.CallReviewerPoolFactory.get_url(
            pool_entry, action="force-accept"
        )
        resp = self.client.post(
            url, {"override_reason": "Reviewer confirmed verbally."}
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        pool_entry.refresh_from_db()
        self.assertEqual(
            pool_entry.invitation_status, ReviewerPoolInvitationStatuses.ACCEPTED
        )
        self.assertEqual(pool_entry.override_reason, "Reviewer confirmed verbally.")
        self.assertEqual(pool_entry.overridden_by, self.call_manager)
        self.assertIsNotNone(pool_entry.overridden_at)
        self.assertIsNotNone(pool_entry.response_date)

    def test_force_accept_already_accepted_returns_error(self):
        pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=factories.ReviewerProfileFactory(),
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )

        self.client.force_authenticate(self.call_manager)
        url = factories.CallReviewerPoolFactory.get_url(
            pool_entry, action="force-accept"
        )
        resp = self.client.post(url, {"override_reason": "Some reason"})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_force_accept_requires_reason(self):
        pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=factories.ReviewerProfileFactory(),
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

        self.client.force_authenticate(self.call_manager)
        url = factories.CallReviewerPoolFactory.get_url(
            pool_entry, action="force-accept"
        )
        resp = self.client.post(url, {})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
