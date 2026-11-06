"""Overdue assignments and pool invitations read as expired before the
periodic expiry tasks have stored that.

Every case below leaves the stored status as it was when the deadline passed
(``sent`` batch, ``pending`` item, ``pending`` pool entry) to stand for the
window between the deadline and the next run of the expiry task.
"""

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
    ProposalStates,
    ReviewerPoolInvitationStatuses,
)

from . import factories

MY_BATCHES_URL = "http://testserver" + reverse("my-assignment-batch-list")


def my_batch_url(batch):
    return "http://testserver" + reverse(
        "my-assignment-batch-detail", kwargs={"uuid": batch.uuid.hex}
    )


class AssignmentBase(test.APITestCase):
    expires_in = timedelta(days=3)

    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.call = factories.CallFactory()
        self.call_manager = structure_factories.UserFactory()
        self.call.add_user(self.call_manager, CallRole.MANAGER)
        self.call.manager.add_user(self.call_manager, CallRole.MANAGER)

        self.reviewer_user = structure_factories.UserFactory()
        self.profile = factories.ReviewerProfileFactory(user=self.reviewer_user)
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        self.round = factories.RoundFactory(call=self.call)
        # Under evaluation: only such a proposal is assigned and reviewed.
        self.proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        self.batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.pool_entry,
            status=AssignmentBatchStatuses.SENT,
            sent_at=timezone.now() - timedelta(days=7),
            expires_at=timezone.now() + self.expires_in,
        )
        self.item = factories.AssignmentItemFactory(
            batch=self.batch,
            proposal=self.proposal,
            status=AssignmentItemStatuses.PENDING,
        )
        self.item_url = factories.AssignmentItemFactory.get_url(self.item)

    def get_as(self, user, url, params=None):
        self.client.force_authenticate(user)
        response = self.client.get(url, params or {})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def item_uuids(self, params):
        data = self.get_as(
            self.reviewer_user,
            factories.AssignmentItemFactory.get_list_url(),
            params,
        )
        return {row["uuid"] for row in data}

    def batch_uuids(self, params):
        data = self.get_as(
            self.call_manager,
            factories.AssignmentBatchFactory.get_list_url(),
            params,
        )
        return {row["uuid"] for row in data}

    def my_batch_uuids(self, params=None):
        data = self.get_as(self.reviewer_user, MY_BATCHES_URL, params)
        return {str(row["uuid"]) for row in data}


class OverdueAssignmentTest(AssignmentBase):
    expires_in = -timedelta(hours=1)

    def test_item_reads_as_expired(self):
        data = self.get_as(self.reviewer_user, self.item_url)
        self.assertEqual(data["status"], AssignmentItemStatuses.EXPIRED)
        self.assertEqual(data["status_display"], "Expired without response")
        self.assertTrue(data["is_expired"])
        self.assertIsNotNone(data["expires_at"])

    def test_stored_status_is_left_for_the_task(self):
        self.get_as(self.reviewer_user, self.item_url)
        self.item.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(self.item.status, AssignmentItemStatuses.PENDING)
        self.assertEqual(self.batch.status, AssignmentBatchStatuses.SENT)

    def test_item_filters_use_effective_status(self):
        self.assertEqual(self.item_uuids({"status": "pending"}), set())
        self.assertEqual(self.item_uuids({"status": "expired"}), {self.item.uuid.hex})

    def test_accept_is_refused(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(self.item_url + "accept/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Ask the call manager to send it again", str(response.data))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, AssignmentItemStatuses.PENDING)
        self.assertFalse(models.Review.objects.filter(proposal=self.proposal).exists())

    def test_decline_is_refused(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(self.item_url + "decline/", {"reason": "busy"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Ask the call manager to send it again", str(response.data))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, AssignmentItemStatuses.PENDING)

    def test_batch_reads_as_expired_for_the_manager(self):
        data = self.get_as(
            self.call_manager, factories.AssignmentBatchFactory.get_url(self.batch)
        )
        self.assertEqual(data["status"], AssignmentBatchStatuses.EXPIRED)
        self.assertEqual(data["items_pending_count"], 0)
        self.assertEqual(data["items"][0]["status"], AssignmentItemStatuses.EXPIRED)

    def test_batch_filters_use_effective_status(self):
        self.assertEqual(self.batch_uuids({"status": "sent"}), set())
        self.assertEqual(self.batch_uuids({"status": "expired"}), {self.batch.uuid.hex})

    def test_my_batches_leave_it_out_of_the_default_list(self):
        self.assertEqual(self.my_batch_uuids(), set())

    def test_my_batches_list_it_as_expired_on_request(self):
        data = self.get_as(
            self.reviewer_user, MY_BATCHES_URL, {"status": ["sent", "expired"]}
        )
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], AssignmentBatchStatuses.EXPIRED)
        self.assertEqual(data[0]["items_pending_count"], 0)
        self.assertTrue(data[0]["is_expired"])

    def test_my_batch_detail_reports_expired_items(self):
        data = self.get_as(self.reviewer_user, my_batch_url(self.batch))
        self.assertEqual(data["status"], AssignmentBatchStatuses.EXPIRED)
        item = data["items"][0]
        self.assertEqual(item["status"], AssignmentItemStatuses.EXPIRED)
        self.assertTrue(item["is_expired"])
        self.assertEqual(item["expires_at"], self.batch.expires_at)

    def test_overdue_item_is_not_open_workload(self):
        entry = models.CallReviewerPool.objects.with_open_assignments().get(
            pk=self.pool_entry.pk
        )
        self.assertEqual(entry.open_assignments, 0)


class AssignmentInDateTest(AssignmentBase):
    def test_item_reads_as_pending(self):
        data = self.get_as(self.reviewer_user, self.item_url)
        self.assertEqual(data["status"], AssignmentItemStatuses.PENDING)
        self.assertFalse(data["is_expired"])
        self.assertIsNotNone(data["expires_at"])

    def test_item_filters_use_effective_status(self):
        self.assertEqual(self.item_uuids({"status": "pending"}), {self.item.uuid.hex})
        self.assertEqual(self.item_uuids({"status": "expired"}), set())

    def test_batch_filters_use_effective_status(self):
        self.assertEqual(self.batch_uuids({"status": "sent"}), {self.batch.uuid.hex})
        self.assertEqual(self.batch_uuids({"status": "expired"}), set())

    def test_my_batches_list_it_by_default(self):
        self.assertEqual(self.my_batch_uuids(), {self.batch.uuid.hex})
        self.assertEqual(self.my_batch_uuids({"status": "expired"}), set())

    def test_my_batches_refuse_unknown_status(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.get(MY_BATCHES_URL, {"status": "draft"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_accept_succeeds(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(self.item_url + "accept/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, AssignmentItemStatuses.ACCEPTED)

    def test_pending_item_is_open_workload(self):
        entry = models.CallReviewerPool.objects.with_open_assignments().get(
            pk=self.pool_entry.pk
        )
        self.assertEqual(entry.open_assignments, 1)


class PoolInvitationBase(test.APITestCase):
    expires_in = timedelta(days=3)

    def setUp(self):
        self.reviewer_user = structure_factories.UserFactory()
        self.profile = factories.ReviewerProfileFactory(
            user=self.reviewer_user, is_published=True
        )
        self.entry = factories.CallReviewerPoolFactory(
            reviewer=self.profile,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
            invitation_expires_at=timezone.now() + self.expires_in,
        )

    def my_invitations(self, **params):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.get(
            factories.CallReviewerPoolFactory.get_list_url(),
            {"my_invitations": True, **params},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data


class OverduePoolInvitationTest(PoolInvitationBase):
    expires_in = -timedelta(hours=1)

    def test_invitation_reads_as_expired(self):
        [row] = self.my_invitations()
        self.assertEqual(
            row["invitation_status"], ReviewerPoolInvitationStatuses.EXPIRED
        )
        self.assertEqual(row["invitation_status_display"], "Invitation expired")
        self.assertTrue(row["is_expired"])
        self.assertIsNone(row["invitation_link"])
        self.entry.refresh_from_db()
        self.assertEqual(
            self.entry.invitation_status, ReviewerPoolInvitationStatuses.PENDING
        )

    def test_filter_uses_effective_status(self):
        self.assertEqual(self.my_invitations(invitation_status="pending"), [])
        [row] = self.my_invitations(invitation_status="expired")
        self.assertEqual(row["uuid"], self.entry.uuid.hex)

    def test_accept_is_refused(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.entry, action="accept"),
            [],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Ask the call manager to send it again", str(response.data))


class PoolInvitationInDateTest(PoolInvitationBase):
    def test_invitation_reads_as_pending(self):
        [row] = self.my_invitations()
        self.assertEqual(
            row["invitation_status"], ReviewerPoolInvitationStatuses.PENDING
        )
        self.assertFalse(row["is_expired"])
        self.assertIsNotNone(row["invitation_link"])

    def test_filter_uses_effective_status(self):
        [row] = self.my_invitations(invitation_status="pending")
        self.assertEqual(row["uuid"], self.entry.uuid.hex)
        self.assertEqual(self.my_invitations(invitation_status="expired"), [])

    def test_invitation_without_deadline_stays_pending(self):
        self.entry.invitation_expires_at = None
        self.entry.save()
        [row] = self.my_invitations(invitation_status="pending")
        self.assertFalse(row["is_expired"])
