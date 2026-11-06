"""What a call's reviewers and panel members read of its proposals.

The call's reviewer role is the reviewer pool: it grants no proposal by
itself. A reviewer reads only the proposals they hold work on -- a live
review, a sent assignment they have not declined, a contribution that still
counts, an evaluator role -- and nothing else of the call.

The panel reads the call's submitted proposals as the committee, but the
call team's side of a step (its internal notes, who completed it, the
outcome and reason masked from readers outside the call team) only of the
steps it takes part in: those it completes or contributes to, and their
inputs. The call's managers read everything, as before. Step mail follows.
"""

from datetime import timedelta

from django.core import mail
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.models import Notification
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    ResponsibleRoles,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)

from . import factories

ADMIN_NOTES = "Admin check internal note"
ADMIN_REASON = "Admin check reason"
REVIEW_NOTES = "Expert review internal note"
REVIEW_REASON = "Expert review verdict reason"
PANEL_NOTES = "Panel internal note"
PANEL_REASON = "Panel recommendation reason"


class CallRoleScopingBase(test.APITestCase):
    def setUp(self):
        self.call = factories.CallFactory(reviews_visible_to_submitters=False)
        self.round = factories.RoundFactory(call=self.call)
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)

        self.assigned = factories.ProposalFactory(
            round=self.round, state=ProposalStates.IN_REVIEW
        )
        self.unassigned = factories.ProposalFactory(
            round=self.round, state=ProposalStates.IN_REVIEW
        )
        self.draft = factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT
        )

        # A pool reviewer reviewing ``assigned`` only.
        self.reviewer = structure_factories.UserFactory()
        self.call.add_user(self.reviewer, CallRole.REVIEWER)
        factories.ReviewFactory(proposal=self.assigned, reviewer=self.reviewer)

        # A pool reviewer holding no work at all.
        self.idle_reviewer = structure_factories.UserFactory()
        self.call.add_user(self.idle_reviewer, CallRole.REVIEWER)

        self.panel_member = structure_factories.UserFactory()
        self.call.add_user(self.panel_member, CallRole.PANEL_MEMBER)

    def list_names(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(factories.ProposalFactory.get_list_url(), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def get(self, user, proposal, action=None):
        self.client.force_authenticate(user)
        return self.client.get(factories.ProposalFactory.get_url(proposal, action))


class ReviewerScopeTest(CallRoleScopingBase):
    def test_reviewer_lists_only_the_proposal_they_review(self):
        self.assertEqual(self.list_names(self.reviewer), {self.assigned.uuid.hex})

    def test_reviewer_cannot_open_an_unassigned_proposal(self):
        self.assertEqual(
            self.get(self.reviewer, self.assigned).status_code, status.HTTP_200_OK
        )
        for action in (None, "workflow_states", "released_reports", "resources"):
            response = self.get(self.reviewer, self.unassigned, action)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, action)

    def test_reviewer_as_role_filter_lists_only_their_work(self):
        self.assertEqual(
            self.list_names(self.reviewer, as_role=ResponsibleRoles.REVIEWER),
            {self.assigned.uuid.hex},
        )
        self.assertEqual(
            self.list_names(self.idle_reviewer, as_role=ResponsibleRoles.REVIEWER),
            set(),
        )

    def test_pool_reviewer_without_work_sees_nothing(self):
        self.assertEqual(self.list_names(self.idle_reviewer), set())

    def _assignment(self, item_status, batch_status=AssignmentBatchStatuses.SENT):
        profile = factories.ReviewerProfileFactory(user=self.idle_reviewer)
        pool_entry = factories.CallReviewerPoolFactory(call=self.call, reviewer=profile)
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=pool_entry,
            status=batch_status,
            expires_at=timezone.now() + timedelta(days=7),
        )
        return factories.AssignmentItemFactory(
            batch=batch, proposal=self.unassigned, status=item_status
        )

    def test_pending_assignment_grants_the_proposal(self):
        self._assignment(AssignmentItemStatuses.PENDING)
        self.assertEqual(
            self.list_names(self.idle_reviewer), {self.unassigned.uuid.hex}
        )

    def test_unsent_assignment_grants_nothing(self):
        self._assignment(
            AssignmentItemStatuses.PENDING, batch_status=AssignmentBatchStatuses.DRAFT
        )
        self.assertEqual(self.list_names(self.idle_reviewer), set())

    def test_declined_assignment_grants_nothing(self):
        item = self._assignment(AssignmentItemStatuses.PENDING)
        item.status = AssignmentItemStatuses.DECLINED
        item.save()
        self.assertEqual(self.list_names(self.idle_reviewer), set())
        self.assertEqual(
            self.get(self.idle_reviewer, self.unassigned).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_rejected_review_grants_nothing(self):
        models.Review.objects.filter(reviewer=self.reviewer).update(
            state=models.Review.States.REJECTED
        )
        self.assertEqual(self.list_names(self.reviewer), set())

    def test_call_manager_still_reads_every_proposal(self):
        self.assertEqual(
            self.list_names(self.manager),
            {self.assigned.uuid.hex, self.unassigned.uuid.hex, self.draft.uuid.hex},
        )


class PanelMemberScopeTest(CallRoleScopingBase):
    def test_panel_member_reads_submitted_proposals_not_drafts(self):
        self.assertEqual(
            self.list_names(self.panel_member),
            {self.assigned.uuid.hex, self.unassigned.uuid.hex},
        )
        self.assertEqual(
            self.list_names(self.panel_member, as_role=ResponsibleRoles.PANEL_MEMBER),
            {self.assigned.uuid.hex, self.unassigned.uuid.hex},
        )


class StepInternalsTest(CallRoleScopingBase):
    """Steps of ``assigned``: the call manager's administrative check, the
    reviewers' expert review and the panel's review, all completed with
    internal notes and a reason."""

    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.instances = {}
        for key, outcome, reason, notes in (
            (
                "administrative_check",
                WorkflowStepOutcomes.ELIGIBLE,
                ADMIN_REASON,
                ADMIN_NOTES,
            ),
            (
                "expert_review",
                WorkflowStepOutcomes.REVIEWED,
                REVIEW_REASON,
                REVIEW_NOTES,
            ),
            ("panel_review", WorkflowStepOutcomes.APPROVED, PANEL_REASON, PANEL_NOTES),
        ):
            factories.ensure_default_step(self.call, key)
            self.instances[key] = factories.ProposalWorkflowStepInstanceFactory(
                proposal=self.assigned,
                step=key,
                status=WorkflowStepInstanceStatuses.COMPLETED,
                started_at=timezone.now(),
                completed_at=timezone.now(),
                outcome=outcome,
                outcome_reason=reason,
                internal_notes=notes,
                completed_by=self.manager,
            )

    def states(self, user):
        response = self.get(user, self.assigned, "workflow_states")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["step"]: item for item in response.data}

    def test_panel_member_reads_internals_of_their_own_step_only(self):
        states = self.states(self.panel_member)
        self.assertEqual(states["panel_review"]["internal_notes"], PANEL_NOTES)
        self.assertEqual(states["panel_review"]["outcome_reason"], PANEL_REASON)
        self.assertIsNotNone(states["panel_review"]["completed_by"])
        # A step they take no part in: no notes, no actor, no masked verdict.
        self.assertIsNone(states["administrative_check"]["internal_notes"])
        self.assertIsNone(states["administrative_check"]["completed_by"])
        self.assertIsNone(states["expert_review"]["internal_notes"])
        self.assertIsNone(states["expert_review"]["outcome"])
        self.assertEqual(states["expert_review"]["outcome_reason"], "")
        # The administrative check's outcome is not masked from anyone.
        self.assertEqual(states["administrative_check"]["outcome_reason"], ADMIN_REASON)

    def test_panel_member_reads_the_inputs_of_their_step(self):
        self.instances["panel_review"].input_steps.add(
            self.instances["expert_review"].call_step
        )
        states = self.states(self.panel_member)
        self.assertEqual(states["expert_review"]["internal_notes"], REVIEW_NOTES)
        self.assertEqual(states["expert_review"]["outcome_reason"], REVIEW_REASON)
        self.assertIsNone(states["administrative_check"]["internal_notes"])

    def test_reviewer_reads_internals_of_the_review_step_only(self):
        states = self.states(self.reviewer)
        self.assertEqual(states["expert_review"]["internal_notes"], REVIEW_NOTES)
        self.assertEqual(states["expert_review"]["outcome_reason"], REVIEW_REASON)
        self.assertIsNone(states["administrative_check"]["internal_notes"])
        self.assertIsNone(states["panel_review"]["internal_notes"])
        self.assertIsNone(states["panel_review"]["outcome"])
        # Nor any other step's outcome and reason, masked or not.
        self.assertIsNone(states["administrative_check"]["outcome"])
        self.assertEqual(states["administrative_check"]["outcome_reason"], "")

    def test_reviewer_without_the_call_role_reads_only_their_step_outcome(self):
        reviewer = structure_factories.UserFactory()
        factories.ReviewFactory(proposal=self.assigned, reviewer=reviewer)
        states = self.states(reviewer)
        self.assertIsNone(states["administrative_check"]["outcome"])
        self.assertEqual(states["administrative_check"]["outcome_reason"], "")
        self.assertIsNone(states["administrative_check"]["internal_notes"])

    def test_call_manager_reads_every_step(self):
        states = self.states(self.manager)
        for key, notes in (
            ("administrative_check", ADMIN_NOTES),
            ("expert_review", REVIEW_NOTES),
            ("panel_review", PANEL_NOTES),
        ):
            self.assertEqual(states[key]["internal_notes"], notes)

    def _send(self, key, recipient):
        rule = models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.instances[key].call_step,
            trigger=NotificationRuleTriggers.STEP_COMPLETED,
            recipient=recipient,
        )
        mail.outbox = []
        tasks._send_step_event(
            self.instances[key], NotificationRuleTriggers.STEP_COMPLETED, [rule]
        )
        return {
            address: message.body for message in mail.outbox for address in message.to
        }

    def test_pool_reviewer_mail_carries_only_the_outcome_of_their_step(self):
        bodies = self._send(
            "administrative_check", NotificationRuleRecipients.ALL_POOL_REVIEWERS
        )
        self.assertNotIn(ADMIN_REASON, bodies[self.reviewer.email])
        self.assertNotIn(ADMIN_REASON, bodies[self.idle_reviewer.email])
        bodies = self._send(
            "expert_review", NotificationRuleRecipients.ALL_POOL_REVIEWERS
        )
        self.assertIn(REVIEW_REASON, bodies[self.reviewer.email])
        self.assertNotIn(REVIEW_REASON, bodies[self.idle_reviewer.email])

    def test_panel_chair_mail_carries_only_the_outcomes_the_api_shows(self):
        self.call.panel_chair = self.panel_member
        self.call.save(update_fields=["panel_chair"])
        bodies = self._send("expert_review", NotificationRuleRecipients.PANEL_CHAIR)
        self.assertNotIn(REVIEW_REASON, bodies[self.panel_member.email])
        bodies = self._send("panel_review", NotificationRuleRecipients.PANEL_CHAIR)
        self.assertIn(PANEL_REASON, bodies[self.panel_member.email])

    def test_call_manager_mail_carries_the_outcome(self):
        bodies = self._send("expert_review", NotificationRuleRecipients.CALL_MANAGERS)
        self.assertIn(REVIEW_REASON, bodies[self.manager.email])
