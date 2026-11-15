"""An assignment reads a proposal only while its reviewer is an accepted
member of the call's pool, and a reader through it sees the applicant as an
evaluator does.

Revoking a reviewer's call role declines their pool entry; the assignments
they had not answered close with it.
"""

import importlib
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.core import mail
from django.utils import timezone
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.tools.proposals_reviewer.review_assistant import (
    ReviewAssistantTool,
)
from waldur_mastermind.chat.tools.proposals_reviewer.review_workload import (
    ReviewWorkloadTool,
)
from waldur_mastermind.proposal import (
    managers,
    models,
    my_tasks,
    reviewer_membership,
    tasks,
)
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    ProposalStates,
    ReviewerPoolInvitationStatuses,
)

from . import factories

ITEM = AssignmentItemStatuses
BATCH = AssignmentBatchStatuses
POOL = ReviewerPoolInvitationStatuses

APPLICANT_NAME_FIELDS = ("created_by_name", "applicant_full_name")


class RevokedReviewerAssignmentsBaseTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        # The call shows its reviewers no applicant identity attribute.
        self.visibility_config = models.CallApplicantVisibilityConfig.objects.create(
            call=self.call,
            expose_full_name=False,
            expose_username=False,
            expose_email=False,
        )
        self.applicant = structure_factories.UserFactory(full_name="Ada Applicant")
        self.proposal = factories.ProposalFactory(
            round=self.round,
            created_by=self.applicant,
            state=ProposalStates.SUBMITTED,
        )
        self.reviewer = structure_factories.UserFactory()
        self.profile = factories.ReviewerProfileFactory(user=self.reviewer)

    def make_entry(self, invitation_status=POOL.ACCEPTED):
        return factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.profile,
            invitation_status=invitation_status,
        )

    def make_item(self, entry, batch_status=BATCH.SENT, item_status=ITEM.PENDING):
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=entry,
            status=batch_status,
            expires_at=timezone.now() + timedelta(days=7),
        )
        return factories.AssignmentItemFactory(
            batch=batch, proposal=self.proposal, status=item_status
        )

    def get_proposal(self, user):
        self.client.force_authenticate(user)
        return self.client.get(factories.ProposalFactory.get_url(self.proposal))


class RevokingTheRoleClosesPendingAssignmentsTest(RevokedReviewerAssignmentsBaseTest):
    def setUp(self):
        super().setUp()
        # Accepting the pool invitation gives the call's reviewer role.
        self.entry = self.make_entry()
        self.item = self.make_item(self.entry)
        self.assertTrue(self.call.has_user(self.reviewer, CallRole.REVIEWER))

    def revoke(self):
        self.call.remove_user(self.reviewer, CallRole.REVIEWER, removed_by=self.staff)

    def test_the_assigned_reviewer_reads_the_proposal_before_the_revocation(self):
        response = self.get_proposal(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in APPLICANT_NAME_FIELDS:
            self.assertNotIn(field, response.data)

    def test_revoking_the_role_closes_the_pending_item(self):
        self.revoke()
        self.entry.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.entry.invitation_status, POOL.DECLINED)
        self.assertEqual(self.item.status, ITEM.EXPIRED)
        # Nothing left to answer: the batch is done, so neither the expiry
        # task nor a deadline extension reopens it.
        self.item.batch.refresh_from_db()
        self.assertEqual(self.item.batch.status, BATCH.RESPONDED)

    def test_the_revoked_reviewer_no_longer_reads_the_proposal(self):
        self.revoke()
        response = self.get_proposal(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_review_assistant_no_longer_answers_the_revoked_reviewer(self):
        self.revoke()
        result = ReviewAssistantTool().execute(
            self.reviewer, {"uuid": self.proposal.uuid.hex}
        )
        self.assertEqual(result["type"], "error", result)
        self.assertNotIn("Ada Applicant", str(result))

    def test_the_closed_items_say_why(self):
        self.revoke()
        self.item.refresh_from_db()
        self.assertEqual(self.item.decline_reason, reviewer_membership.REVOKED_REASON)
        event = Event.objects.get(
            event_type=EventType.REVIEW_ASSIGNMENTS_CLOSED_ON_LEAVING_POOL
        )
        self.assertEqual(event.context["call_uuid"], self.call.uuid.hex)

    def test_nothing_is_logged_when_nothing_was_pending(self):
        self.item.status = ITEM.DECLINED
        self.item.save()
        self.revoke()
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.REVIEW_ASSIGNMENTS_CLOSED_ON_LEAVING_POOL
            ).exists()
        )

    def test_answered_items_are_kept(self):
        other = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        accepted = factories.AssignmentItemFactory(
            batch=self.item.batch, proposal=other, status=ITEM.ACCEPTED
        )
        self.revoke()
        accepted.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(accepted.status, ITEM.ACCEPTED)
        self.assertEqual(self.item.status, ITEM.EXPIRED)

    def test_an_unsent_batch_is_cancelled_with_its_items(self):
        draft_item = self.make_item(self.entry, batch_status=BATCH.DRAFT)
        self.revoke()
        draft_item.refresh_from_db()
        draft_item.batch.refresh_from_db()
        self.assertEqual(draft_item.status, ITEM.EXPIRED)
        self.assertEqual(draft_item.batch.status, BATCH.CANCELLED)

    def test_other_reviewers_assignments_are_untouched(self):
        other_profile = factories.ReviewerProfileFactory()
        other_entry = factories.CallReviewerPoolFactory(
            call=self.call, reviewer=other_profile
        )
        other_item = self.make_item(other_entry)
        self.revoke()
        other_item.refresh_from_db()
        other_item.batch.refresh_from_db()
        self.assertEqual(other_item.status, ITEM.PENDING)
        self.assertEqual(other_item.batch.status, BATCH.SENT)
        response = self.get_proposal(other_profile.user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class AssignmentNeedsAnAcceptedPoolEntryTest(RevokedReviewerAssignmentsBaseTest):
    def test_a_pending_item_of_a_declined_entry_grants_nothing(self):
        # As a pool entry declined before this change left it.
        entry = self.make_entry(invitation_status=POOL.DECLINED)
        self.make_item(entry)
        self.assertNotIn(
            self.proposal.pk, managers.get_assigned_proposals(self.reviewer)
        )
        response = self.get_proposal(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_pending_item_of_an_unanswered_invitation_grants_nothing(self):
        entry = self.make_entry(invitation_status=POOL.PENDING)
        self.make_item(entry)
        response = self.get_proposal(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_pending_item_of_a_declined_entry_cannot_be_accepted(self):
        entry = self.make_entry(invitation_status=POOL.DECLINED)
        item = self.make_item(entry)
        self.client.force_authenticate(self.reviewer)
        response = self.client.post(
            factories.AssignmentItemFactory.get_url(item) + "accept/"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        item.refresh_from_db()
        self.assertEqual(item.status, ITEM.PENDING)
        self.assertFalse(
            models.Review.objects.filter(
                proposal=self.proposal, reviewer=self.reviewer
            ).exists()
        )

    def test_a_pending_item_of_an_accepted_entry_reads_the_proposal(self):
        entry = self.make_entry()
        self.make_item(entry)
        self.assertIn(self.proposal.pk, managers.get_assigned_proposals(self.reviewer))
        response = self.get_proposal(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class AssignedReaderSeesTheApplicantAsAnEvaluatorTest(
    RevokedReviewerAssignmentsBaseTest
):
    def setUp(self):
        super().setUp()
        self.entry = self.make_entry()
        self.make_item(self.entry)

    def drop_role_quietly(self):
        # An accepted entry whose holder lacks the role, as rows written
        # around the membership signals leave it (a demo preset, say).
        UserRole.objects.filter(user=self.reviewer, is_active=True).update(
            is_active=False
        )
        self.assertFalse(self.call.has_user(self.reviewer, CallRole.REVIEWER))

    def test_an_assigned_reader_without_the_role_sees_no_concealed_attribute(self):
        self.drop_role_quietly()
        response = self.get_proposal(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in APPLICANT_NAME_FIELDS:
            self.assertNotIn(field, response.data)

    def test_an_assigned_reader_sees_what_the_call_exposes(self):
        self.drop_role_quietly()
        self.visibility_config.expose_full_name = True
        self.visibility_config.save()
        response = self.get_proposal(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created_by_name"], "Ada Applicant")

    def test_an_assigned_reader_holding_the_role_sees_no_concealed_attribute(self):
        response = self.get_proposal(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in APPLICANT_NAME_FIELDS:
            self.assertNotIn(field, response.data)


class DeclinedEntryAssignmentsStayClosedTest(RevokedReviewerAssignmentsBaseTest):
    """Nothing reopens, sends, reminds of or counts the assignments of a pool
    entry that is no longer accepted: one whose call role was revoked, or
    one declined before revocation closed its items."""

    def setUp(self):
        super().setUp()
        self.reviewer.email = "reviewer@example.com"
        self.reviewer.save()
        structure_factories.NotificationFactory(
            key="proposal.assignment_expiry_reminder"
        )

    def declined_entry_with_item(self, batch_status=BATCH.SENT, **kwargs):
        entry = self.make_entry(invitation_status=POOL.DECLINED)
        return entry, self.make_item(entry, batch_status=batch_status, **kwargs)

    def extend(self, batch):
        self.client.force_authenticate(self.staff)
        return self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="extend-deadline"),
            {"expires_at": (timezone.now() + timedelta(days=7)).isoformat()},
        )

    def test_a_deadline_extension_does_not_reopen_a_revoked_reviewers_batch(self):
        entry = self.make_entry()
        item = self.make_item(entry)
        batch = item.batch
        batch.expires_at = timezone.now() - timedelta(days=1)
        batch.save()
        tasks.mark_expired_assignment_batches()
        self.call.remove_user(self.reviewer, CallRole.REVIEWER, removed_by=self.staff)

        response = self.extend(batch)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        batch.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(batch.status, BATCH.EXPIRED)
        self.assertEqual(item.status, ITEM.EXPIRED)
        self.assertEqual(self.get_proposal(self.reviewer).status_code, 404)

    def test_a_deadline_extension_is_refused_for_a_declined_entry(self):
        _, item = self.declined_entry_with_item()
        response = self.extend(item.batch)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_deadline_extension_still_works_for_an_accepted_entry(self):
        item = self.make_item(self.make_entry())
        response = self.extend(item.batch)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @mock.patch("waldur_mastermind.proposal.tasks.send_assignment_batch_invitation")
    def test_a_draft_batch_of_a_declined_entry_is_not_sent(self, send_task):
        _, item = self.declined_entry_with_item(batch_status=BATCH.DRAFT)
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(item.batch, action="send")
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        item.batch.refresh_from_db()
        self.assertEqual(item.batch.status, BATCH.DRAFT)
        send_task.delay.assert_not_called()

    @mock.patch("waldur_mastermind.proposal.tasks.send_assignment_batch_invitation")
    def test_send_all_skips_a_draft_batch_of_a_declined_entry(self, send_task):
        _, item = self.declined_entry_with_item(batch_status=BATCH.DRAFT)
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="send-all-assignments"
            ),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["batches_sent"], 0)
        item.batch.refresh_from_db()
        self.assertEqual(item.batch.status, BATCH.DRAFT)
        send_task.delay.assert_not_called()

    def test_a_coi_block_of_a_declined_entry_is_not_lifted(self):
        _, item = self.declined_entry_with_item(item_status=ITEM.COI_BLOCKED)
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.AssignmentItemFactory.get_url(item, action="force-unblock"),
            {"override_reason": "Cleared."},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        item.refresh_from_db()
        self.assertEqual(item.status, ITEM.COI_BLOCKED)

    def test_no_task_asks_a_declined_reviewer_to_answer(self):
        self.declined_entry_with_item()
        self.client.force_authenticate(self.reviewer)
        response = self.client.get(
            "http://testserver/api/proposal-my-tasks/", {"page_size": 100}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(
            my_tasks.Needs.ANSWER_ASSIGNMENT, [row["needed"] for row in response.data]
        )

    def test_no_expiry_reminder_reaches_a_declined_reviewer(self):
        _, item = self.declined_entry_with_item()
        item.batch.expires_at = timezone.now() + timedelta(hours=12)
        item.batch.save()
        tasks.send_assignment_expiry_reminders()
        self.assertEqual(len(mail.outbox), 0)

    def test_an_accepted_reviewer_is_still_reminded(self):
        item = self.make_item(self.make_entry())
        item.batch.expires_at = timezone.now() + timedelta(hours=12)
        item.batch.save()
        tasks.send_assignment_expiry_reminders()
        self.assertEqual(len(mail.outbox), 1)

    def test_a_declined_entrys_items_are_no_workload(self):
        entry, _ = self.declined_entry_with_item()
        self.assertEqual(entry.get_open_assignments(), 0)
        self.assertFalse(
            models.AssignmentItem.objects.filter(
                managers.open_assignment_items_q()
            ).exists()
        )

    def test_the_workload_tool_lists_no_assignment_of_a_declined_entry(self):
        self.declined_entry_with_item()
        result = ReviewWorkloadTool().execute(self.reviewer, {})
        self.assertEqual(result["data"]["pending_assignments"], [])


class ReviewAssistantFollowsTheAssignmentAccessRuleTest(
    RevokedReviewerAssignmentsBaseTest
):
    """The review assistant answers only whoever reads the proposal through
    a live assignment, as the proposal API decides it."""

    def assert_refused(self):
        result = ReviewAssistantTool().execute(
            self.reviewer, {"uuid": self.proposal.uuid.hex}
        )
        self.assertEqual(result["type"], "error", result)
        self.assertNotIn("Ada Applicant", str(result))

    def test_an_expired_item_opens_nothing(self):
        self.make_item(self.make_entry(), item_status=ITEM.EXPIRED)
        self.assert_refused()

    def test_a_reassigned_item_opens_nothing(self):
        self.make_item(self.make_entry(), item_status=ITEM.REASSIGNED)
        self.assert_refused()

    def test_a_pending_item_of_a_declined_entry_opens_nothing(self):
        self.make_item(self.make_entry(invitation_status=POOL.DECLINED))
        self.assert_refused()

    def test_a_pending_item_of_an_accepted_entry_is_answered(self):
        self.make_item(self.make_entry())
        result = ReviewAssistantTool().execute(
            self.reviewer, {"uuid": self.proposal.uuid.hex}
        )
        self.assertEqual(result["type"], "success", result)


class AcceptingLocksThePoolEntryTest(RevokedReviewerAssignmentsBaseTest):
    def test_the_entry_is_locked_before_the_item(self):
        # Revoking the role locks the entry and then its items: accepting
        # takes them in the same order, so it sees a revocation that won.
        item = self.make_item(self.make_entry())
        locked = []
        for model in (models.CallReviewerPool, models.AssignmentItem):
            original = model.objects.select_for_update

            def spy(*args, _model=model, _original=original, **kwargs):
                locked.append(_model)
                return _original(*args, **kwargs)

            patcher = mock.patch.object(model.objects, "select_for_update", spy)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client.force_authenticate(self.reviewer)
        response = self.client.post(
            factories.AssignmentItemFactory.get_url(item) + "accept/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            locked[:2], [models.CallReviewerPool, models.AssignmentItem], locked
        )


close_migration = importlib.import_module(
    "waldur_mastermind.proposal.migrations.0102_close_assignments_of_departed_reviewers"
)


class LeavingThePoolClosesPendingAssignmentsTest(RevokedReviewerAssignmentsBaseTest):
    """Whatever takes an entry out of accepted closes its pending items, so
    re-accepting it later does not bring them back."""

    def regrant(self):
        self.call.add_user(self.reviewer, CallRole.REVIEWER, created_by=self.staff)

    def test_any_path_out_of_accepted_closes_the_pending_items(self):
        entry = self.make_entry()
        item = self.make_item(entry)
        entry.invitation_status = POOL.DECLINED
        entry.decline_reason = "Asked to leave."
        entry.save()
        item.refresh_from_db()
        self.assertEqual(item.status, ITEM.EXPIRED)
        self.assertEqual(item.decline_reason, "Asked to leave.")

    def test_a_regranted_reviewer_does_not_get_old_items_back(self):
        entry = self.make_entry()
        item = self.make_item(entry)
        self.call.remove_user(self.reviewer, CallRole.REVIEWER, removed_by=self.staff)
        self.regrant()
        entry.refresh_from_db()
        item.refresh_from_db()
        self.assertTrue(entry.is_accepted)
        self.assertEqual(item.status, ITEM.EXPIRED)
        self.assertEqual(self.get_proposal(self.reviewer).status_code, 404)

    def test_the_migration_closes_items_left_by_earlier_revocations(self):
        # As revocation left them before it closed them: entry declined, item
        # pending in a sent batch.
        entry = self.make_entry(invitation_status=POOL.DECLINED)
        item = self.make_item(entry)
        live = factories.AssignmentItemFactory(
            batch=self.make_item(self.make_entry_for_other()).batch,
            proposal=factories.ProposalFactory(round=self.round),
        )
        close_migration.close_assignments_of_departed_reviewers(apps, None)
        item.refresh_from_db()
        item.batch.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(item.status, ITEM.EXPIRED)
        self.assertEqual(item.batch.status, BATCH.RESPONDED)
        self.assertEqual(live.status, ITEM.PENDING)

        self.regrant()
        self.assertEqual(self.get_proposal(self.reviewer).status_code, 404)

    def make_entry_for_other(self):
        return factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=factories.ReviewerProfileFactory(),
            invitation_status=POOL.ACCEPTED,
        )
