"""What a reviewer may see of an assignment before accepting it.

A reviewer only sees batches that were actually sent to them, and of each
proposal only what the call's invitation disclosure level allows. A proposal
kept away from the reviewer by a conflict of interest shows its title only.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.tools.proposals_reviewer.review_assistant import (
    ReviewAssistantTool,
)
from waldur_mastermind.chat.tools.proposals_reviewer.review_workload import (
    ReviewWorkloadTool,
)
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    ProposalDisclosureLevels,
    ReviewerPoolInvitationStatuses,
)

from . import factories

SUMMARY = "Confidential summary of the proposal"
COI_SUMMARY = "Confidential summary of the conflicted proposal"

UNSENT_STATUSES = (AssignmentBatchStatuses.DRAFT, AssignmentBatchStatuses.CANCELLED)
SENT_STATUSES = (
    AssignmentBatchStatuses.SENT,
    AssignmentBatchStatuses.RESPONDED,
    AssignmentBatchStatuses.EXPIRED,
)


class AssignmentDisclosureBaseTest(test.APITestCase):
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
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer_profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        self.round = factories.RoundFactory(call=self.call)

    def make_batch(self, batch_status):
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.pool_entry,
            status=batch_status,
            expires_at=timezone.now() + timedelta(days=7),
        )
        item = factories.AssignmentItemFactory(
            batch=batch,
            proposal=factories.ProposalFactory(
                round=self.round, project_summary=SUMMARY
            ),
            status=AssignmentItemStatuses.PENDING,
        )
        return batch, item

    def my_batch_url(self, batch):
        return reverse("my-assignment-batch-detail", kwargs={"uuid": batch.uuid.hex})

    def set_disclosure(self, level):
        factories.CallCOIConfigurationFactory(
            call=self.call, invitation_proposal_disclosure=level
        )


class UnsentBatchVisibilityTest(AssignmentDisclosureBaseTest):
    def test_reviewer_cannot_retrieve_unsent_batch(self):
        self.client.force_authenticate(self.reviewer_user)
        for batch_status in UNSENT_STATUSES:
            batch, _ = self.make_batch(batch_status)
            response = self.client.get(self.my_batch_url(batch))
            self.assertEqual(
                response.status_code, status.HTTP_404_NOT_FOUND, batch_status
            )

    def test_reviewer_can_retrieve_sent_batch(self):
        self.client.force_authenticate(self.reviewer_user)
        for batch_status in SENT_STATUSES:
            batch, item = self.make_batch(batch_status)
            response = self.client.get(self.my_batch_url(batch))
            self.assertEqual(response.status_code, status.HTTP_200_OK, batch_status)
            self.assertEqual(
                [i["uuid"] for i in response.data["items"]], [item.uuid], batch_status
            )

    def test_reviewer_item_list_excludes_unsent_batches(self):
        sent_batch, sent_item = self.make_batch(AssignmentBatchStatuses.SENT)
        for batch_status in UNSENT_STATUSES:
            self.make_batch(batch_status)

        self.client.force_authenticate(self.reviewer_user)
        response = self.client.get(reverse("assignment-item-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([i["uuid"] for i in response.data], [sent_item.uuid.hex])

    def test_manager_item_list_includes_unsent_batches(self):
        self.make_batch(AssignmentBatchStatuses.SENT)
        for batch_status in UNSENT_STATUSES:
            self.make_batch(batch_status)

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(reverse("assignment-item-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)

    def test_reviewer_cannot_retrieve_item_of_unsent_batch(self):
        self.client.force_authenticate(self.reviewer_user)
        for batch_status in UNSENT_STATUSES:
            _, item = self.make_batch(batch_status)
            url = factories.AssignmentItemFactory.get_url(item)
            response = self.client.get(url)
            self.assertEqual(
                response.status_code, status.HTTP_404_NOT_FOUND, batch_status
            )

    def test_reviewer_cannot_respond_to_item_of_unsent_batch(self):
        self.client.force_authenticate(self.reviewer_user)
        for batch_status in UNSENT_STATUSES:
            for action in ("accept", "decline"):
                _, item = self.make_batch(batch_status)
                url = factories.AssignmentItemFactory.get_url(item)
                response = self.client.post(url + f"{action}/")
                self.assertEqual(
                    response.status_code,
                    status.HTTP_404_NOT_FOUND,
                    (batch_status, action),
                )
                item.refresh_from_db()
                self.assertEqual(item.status, AssignmentItemStatuses.PENDING)
                self.assertFalse(
                    models.Review.objects.filter(proposal=item.proposal).exists()
                )

    def test_reviewer_can_accept_item_of_sent_batch(self):
        _, item = self.make_batch(AssignmentBatchStatuses.SENT)
        self.client.force_authenticate(self.reviewer_user)
        url = factories.AssignmentItemFactory.get_url(item)
        response = self.client.post(url + "accept/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        item.refresh_from_db()
        self.assertEqual(item.status, AssignmentItemStatuses.ACCEPTED)

    def test_reviewer_can_decline_item_of_sent_batch(self):
        _, item = self.make_batch(AssignmentBatchStatuses.SENT)
        self.client.force_authenticate(self.reviewer_user)
        url = factories.AssignmentItemFactory.get_url(item)
        response = self.client.post(url + "decline/", {"reason": "too busy"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        item.refresh_from_db()
        self.assertEqual(item.status, AssignmentItemStatuses.DECLINED)

    def test_reviewer_batch_list_excludes_unsent_batches(self):
        sent_batch, _ = self.make_batch(AssignmentBatchStatuses.SENT)
        unsent = [self.make_batch(s)[0] for s in UNSENT_STATUSES]

        self.client.force_authenticate(self.reviewer_user)
        response = self.client.get(reverse("assignment-batch-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([b["uuid"] for b in response.data], [sent_batch.uuid.hex])

        for batch in unsent:
            response = self.client.get(factories.AssignmentBatchFactory.get_url(batch))
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_manager_batch_list_includes_unsent_batches(self):
        self.make_batch(AssignmentBatchStatuses.SENT)
        for batch_status in UNSENT_STATUSES:
            self.make_batch(batch_status)

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(reverse("assignment-batch-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)

    def test_workload_tool_lists_only_sent_invitations(self):
        _, sent_item = self.make_batch(AssignmentBatchStatuses.SENT)
        for batch_status in UNSENT_STATUSES:
            self.make_batch(batch_status)

        result = ReviewWorkloadTool().execute(self.reviewer_user, {})
        self.assertEqual(
            [p["proposal_slug"] for p in result["data"]["pending_assignments"]],
            [sent_item.proposal.slug],
        )

    def test_review_assistant_ignores_unsent_assignments(self):
        # A call role makes the proposals readable, so only the assignment
        # check decides whether the assistant answers.
        self.call.add_user(self.reviewer_user, CallRole.REVIEWER)
        tool = ReviewAssistantTool()
        for batch_status in UNSENT_STATUSES:
            _, item = self.make_batch(batch_status)
            result = tool.execute(self.reviewer_user, {"uuid": item.proposal.uuid.hex})
            self.assertEqual(result["type"], "error", batch_status)

        _, item = self.make_batch(AssignmentBatchStatuses.SENT)
        result = tool.execute(self.reviewer_user, {"uuid": item.proposal.uuid.hex})
        self.assertEqual(result["type"], "success", result)


class ProposalDisclosureTest(AssignmentDisclosureBaseTest):
    def setUp(self):
        super().setUp()
        self.batch, self.item = self.make_batch(AssignmentBatchStatuses.SENT)
        self.coi_item = factories.AssignmentItemFactory(
            batch=self.batch,
            proposal=factories.ProposalFactory(
                round=self.round, project_summary=COI_SUMMARY
            ),
            status=AssignmentItemStatuses.COI_BLOCKED,
            has_coi=True,
        )
        self.client.force_authenticate(self.reviewer_user)

    def get_items(self):
        response = self.client.get(self.my_batch_url(self.batch))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {i["uuid"]: i for i in response.data["items"]}

    def assert_summary(self, expected):
        items = self.get_items()
        item = items[self.item.uuid]
        self.assertEqual(item["proposal_name"], self.item.proposal.name)
        self.assertEqual(item["proposal_uuid"], self.item.proposal.uuid)
        self.assertEqual(item["proposal_slug"], self.item.proposal.slug)
        self.assertEqual(item["proposal_summary"], expected)

    def test_no_coi_configuration_discloses_titles_only(self):
        self.assert_summary("")

    def test_titles_only_hides_summary(self):
        self.set_disclosure(ProposalDisclosureLevels.TITLES_ONLY)
        self.assert_summary("")

    def test_titles_and_summaries_shows_summary(self):
        self.set_disclosure(ProposalDisclosureLevels.TITLES_AND_SUMMARIES)
        self.assert_summary(SUMMARY)

    def test_full_details_shows_summary(self):
        self.set_disclosure(ProposalDisclosureLevels.FULL_DETAILS)
        self.assert_summary(SUMMARY)

    def test_coi_blocked_item_shows_title_only_at_every_level(self):
        levels = [None] + [level for level, _ in ProposalDisclosureLevels.CHOICES]
        for level in levels:
            models.CallCOIConfiguration.objects.filter(call=self.call).delete()
            if level:
                self.set_disclosure(level)
            items = self.get_items()
            self.assertIn(self.coi_item.uuid, items, level)
            item = items[self.coi_item.uuid]
            self.assertEqual(item["proposal_name"], self.coi_item.proposal.name, level)
            self.assertEqual(item["status"], AssignmentItemStatuses.COI_BLOCKED, level)
            self.assertEqual(item["proposal_summary"], "", level)
            self.assertNotIn(COI_SUMMARY, str(items), level)

    def test_coi_blocked_item_is_listed_for_reviewer_and_manager(self):
        for user in (self.reviewer_user, self.call_manager):
            self.client.force_authenticate(user)
            response = self.client.get(reverse("assignment-item-list"))
            self.assertIn(
                self.coi_item.uuid.hex, [i["uuid"] for i in response.data], user
            )
