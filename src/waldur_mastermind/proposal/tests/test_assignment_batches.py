from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    ReviewerPoolInvitationStatuses,
)

from . import factories


class ExtendDeadlineTest(test.APITestCase):
    def setUp(self):
        # Set up required permissions on CallRole.MANAGER
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)

        self.call = factories.CallFactory()
        self.call_managing_org = self.call.manager
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call_manager = structure_factories.UserFactory()
        # Add user to both Call (for queryset visibility) and CallManagingOrganisation (for permission)
        self.call.add_user(self.call_manager, CallRole.MANAGER)
        self.call_managing_org.add_user(self.call_manager, CallRole.MANAGER)

        # Create reviewer pool entry
        self.reviewer_profile = factories.ReviewerProfileFactory()
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer_profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )

        # Create a proposal
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)

    def _create_batch(self, status=AssignmentBatchStatuses.SENT, expires_at=None):
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.pool_entry,
            status=status,
            expires_at=expires_at or timezone.now() + timedelta(days=7),
        )
        factories.AssignmentItemFactory(
            batch=batch,
            proposal=self.proposal,
            status=AssignmentItemStatuses.PENDING,
        )
        return batch

    def test_staff_can_extend_deadline_for_sent_batch(self):
        batch = self._create_batch(status=AssignmentBatchStatuses.SENT)
        new_deadline = timezone.now() + timedelta(days=14)

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="extend-deadline"),
            {"expires_at": new_deadline.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        batch.refresh_from_db()
        self.assertAlmostEqual(
            batch.expires_at.timestamp(),
            new_deadline.timestamp(),
            delta=1,
        )

    def test_call_manager_can_extend_deadline(self):
        batch = self._create_batch(status=AssignmentBatchStatuses.SENT)
        new_deadline = timezone.now() + timedelta(days=14)

        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="extend-deadline"),
            {"expires_at": new_deadline.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_extend_deadline_reactivates_expired_batch(self):
        # Create expired batch
        batch = self._create_batch(
            status=AssignmentBatchStatuses.EXPIRED,
            expires_at=timezone.now() - timedelta(days=1),
        )
        # Mark items as expired too
        batch.items.update(status=AssignmentItemStatuses.EXPIRED)

        new_deadline = timezone.now() + timedelta(days=7)

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="extend-deadline"),
            {"expires_at": new_deadline.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        batch.refresh_from_db()
        self.assertEqual(batch.status, AssignmentBatchStatuses.SENT)
        # Items should be reactivated to pending
        self.assertTrue(
            batch.items.filter(status=AssignmentItemStatuses.PENDING).exists()
        )

    def test_cannot_extend_deadline_for_draft_batch(self):
        batch = self._create_batch(status=AssignmentBatchStatuses.DRAFT)
        new_deadline = timezone.now() + timedelta(days=14)

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="extend-deadline"),
            {"expires_at": new_deadline.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_extend_deadline_for_responded_batch(self):
        batch = self._create_batch(status=AssignmentBatchStatuses.RESPONDED)
        new_deadline = timezone.now() + timedelta(days=14)

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="extend-deadline"),
            {"expires_at": new_deadline.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_extend_deadline_for_cancelled_batch(self):
        batch = self._create_batch(status=AssignmentBatchStatuses.CANCELLED)
        new_deadline = timezone.now() + timedelta(days=14)

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="extend-deadline"),
            {"expires_at": new_deadline.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_anonymous_user_cannot_extend_deadline(self):
        batch = self._create_batch(status=AssignmentBatchStatuses.SENT)
        new_deadline = timezone.now() + timedelta(days=14)

        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="extend-deadline"),
            {"expires_at": new_deadline.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_extend_deadline(self):
        batch = self._create_batch(status=AssignmentBatchStatuses.SENT)
        new_deadline = timezone.now() + timedelta(days=14)
        regular_user = structure_factories.UserFactory()

        self.client.force_authenticate(regular_user)
        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="extend-deadline"),
            {"expires_at": new_deadline.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_extends_deadline_resets_notification_flags(self):
        batch = self._create_batch(
            status=AssignmentBatchStatuses.EXPIRED,
            expires_at=timezone.now() - timedelta(days=1),
        )
        batch.reminder_sent = True
        batch.manager_notified = True
        batch.save()

        new_deadline = timezone.now() + timedelta(days=7)

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="extend-deadline"),
            {"expires_at": new_deadline.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        batch.refresh_from_db()
        self.assertFalse(batch.reminder_sent)
        self.assertFalse(batch.manager_notified)


class CreateManualAssignmentTest(test.APITestCase):
    def setUp(self):
        self.call = factories.CallFactory()
        self.call_managing_org = self.call.manager
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call_manager = structure_factories.UserFactory()
        # Add user to CallManagingOrganisation with MANAGER role
        self.call_managing_org.add_user(self.call_manager, CallRole.MANAGER)

        # Create reviewer pool entry
        self.reviewer_profile = factories.ReviewerProfileFactory()
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer_profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )

        # Create proposals
        self.round = factories.RoundFactory(call=self.call)
        self.proposal1 = factories.ProposalFactory(round=self.round)
        self.proposal2 = factories.ProposalFactory(round=self.round)

    def test_staff_can_create_manual_assignment(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="create-manual-assignment"
            ),
            {
                "reviewer_pool_entry_uuid": str(self.pool_entry.uuid),
                "proposal_uuids": [str(self.proposal1.uuid)],
                "manager_notes": "Manual assignment for testing",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("batch_uuid", response.data)
        self.assertEqual(response.data["items_created"], 1)

        # Verify batch was created
        batch = models.AssignmentBatch.objects.get(uuid=response.data["batch_uuid"])
        self.assertEqual(batch.call, self.call)
        self.assertEqual(batch.reviewer_pool_entry, self.pool_entry)
        self.assertEqual(batch.status, AssignmentBatchStatuses.DRAFT)
        self.assertEqual(batch.items.count(), 1)

    def test_call_manager_can_create_manual_assignment(self):
        # Note: Call managers need specific permission to create manual assignments
        # For now, test with staff user
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="create-manual-assignment"
            ),
            {
                "reviewer_pool_entry_uuid": str(self.pool_entry.uuid),
                "proposal_uuids": [str(self.proposal1.uuid), str(self.proposal2.uuid)],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["items_created"], 2)

    def test_cannot_assign_to_non_accepted_reviewer(self):
        # Change pool entry status
        self.pool_entry.invitation_status = ReviewerPoolInvitationStatuses.PENDING
        self.pool_entry.save()

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="create-manual-assignment"
            ),
            {
                "reviewer_pool_entry_uuid": str(self.pool_entry.uuid),
                "proposal_uuids": [str(self.proposal1.uuid)],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reviewer_pool_entry_uuid", response.data)

    def test_cannot_assign_proposals_from_other_call(self):
        other_call = factories.CallFactory()
        other_round = factories.RoundFactory(call=other_call)
        other_proposal = factories.ProposalFactory(round=other_round)

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="create-manual-assignment"
            ),
            {
                "reviewer_pool_entry_uuid": str(self.pool_entry.uuid),
                "proposal_uuids": [str(other_proposal.uuid)],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("proposal_uuids", response.data)

    def test_anonymous_user_cannot_create_assignment(self):
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="create-manual-assignment"
            ),
            {
                "reviewer_pool_entry_uuid": str(self.pool_entry.uuid),
                "proposal_uuids": [str(self.proposal1.uuid)],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_create_assignment(self):
        regular_user = structure_factories.UserFactory()

        self.client.force_authenticate(regular_user)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="create-manual-assignment"
            ),
            {
                "reviewer_pool_entry_uuid": str(self.pool_entry.uuid),
                "proposal_uuids": [str(self.proposal1.uuid)],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_adds_to_existing_draft_batch(self):
        # Create existing draft batch
        existing_batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.pool_entry,
            status=AssignmentBatchStatuses.DRAFT,
        )
        factories.AssignmentItemFactory(
            batch=existing_batch,
            proposal=self.proposal1,
        )

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="create-manual-assignment"
            ),
            {
                "reviewer_pool_entry_uuid": str(self.pool_entry.uuid),
                "proposal_uuids": [str(self.proposal2.uuid)],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["batch_uuid"], str(existing_batch.uuid))
        # Should now have 2 items
        existing_batch.refresh_from_db()
        self.assertEqual(existing_batch.items.count(), 2)

    def test_does_not_create_duplicate_items(self):
        # Create existing draft batch with proposal1
        existing_batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.pool_entry,
            status=AssignmentBatchStatuses.DRAFT,
        )
        factories.AssignmentItemFactory(
            batch=existing_batch,
            proposal=self.proposal1,
        )

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="create-manual-assignment"
            ),
            {
                "reviewer_pool_entry_uuid": str(self.pool_entry.uuid),
                # Try to add proposal1 again + proposal2
                "proposal_uuids": [str(self.proposal1.uuid), str(self.proposal2.uuid)],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Only proposal2 should be added (proposal1 already exists)
        self.assertEqual(response.data["items_created"], 1)
        existing_batch.refresh_from_db()
        self.assertEqual(existing_batch.items.count(), 2)


class AssignmentResponseConsentTest(test.APITestCase):
    """Responding to an assignment belongs to the reviewer it was sent to.

    An ungated accept let a call manager create a review in a reviewer's name.
    """

    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)

        self.call = factories.CallFactory()
        self.call_manager = structure_factories.UserFactory()
        self.call.add_user(self.call_manager, CallRole.MANAGER)
        self.call.manager.add_user(self.call_manager, CallRole.MANAGER)

        self.reviewer_user = structure_factories.UserFactory()
        self.reviewer_profile = factories.ReviewerProfileFactory(
            user=self.reviewer_user
        )
        pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer_profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)
        self.batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=pool_entry,
            status=AssignmentBatchStatuses.SENT,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.item = factories.AssignmentItemFactory(
            batch=self.batch,
            proposal=self.proposal,
            status=AssignmentItemStatuses.PENDING,
        )
        self.url = factories.AssignmentItemFactory.get_url(self.item)

    def test_call_manager_cannot_accept_for_the_reviewer(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(self.url + "accept/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, AssignmentItemStatuses.PENDING)
        self.assertFalse(
            models.Review.objects.filter(proposal=self.proposal).exists(),
            "no review may be created in the reviewer's name",
        )

    def test_call_manager_cannot_decline_for_the_reviewer(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(self.url + "decline/", {"reason": "too busy"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, AssignmentItemStatuses.PENDING)

    def test_reviewer_can_accept_own_assignment(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(self.url + "accept/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, AssignmentItemStatuses.ACCEPTED)
        self.assertEqual(self.item.review.reviewer, self.reviewer_user)

    def test_reviewer_cannot_delete_instead_of_declining(self):
        self.client.force_authenticate(self.reviewer_user)
        self.assertEqual(
            self.client.delete(self.url).status_code, status.HTTP_403_FORBIDDEN
        )
        batch_url = factories.AssignmentBatchFactory.get_url(self.batch)
        self.assertEqual(
            self.client.delete(batch_url).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_call_manager_can_delete_an_assignment(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_collection_post_is_not_a_route(self):
        self.client.force_authenticate(self.call_manager)
        for name in ("assignment-batch-list", "assignment-item-list"):
            response = self.client.post("http://testserver" + reverse(name), {})
            self.assertEqual(
                response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED, name
            )

    def test_unrelated_reviewer_cannot_accept(self):
        outsider = structure_factories.UserFactory()
        factories.ReviewerProfileFactory(user=outsider)
        self.client.force_authenticate(outsider)
        response = self.client.post(self.url + "accept/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
