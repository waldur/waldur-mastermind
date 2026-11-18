"""A proposal's open reviews and assignments close once it is decided.

Whichever path accepts, rejects or cancels it -- a step completed or
rejected, an automatic decision, a lapsed step, a round completed with the
proposal undecided, a held decision released -- reviews still being written
are rejected as closed on the decision, and assignments not yet answered
lapse. Submitted reviews stay as they are.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.db import transaction
from django.utils import timezone
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    contributions,
    handlers,
    managers,
    models,
    views,
    workflow_service,
)
from waldur_mastermind.proposal import tasks as proposal_tasks
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    ContributionOutcomes,
    ProposalStates,
    ResultsPublication,
    ReviewRejectionCauses,
    RoundLifecycleStates,
    StepContributionStatuses,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tasks import mark_expired_workflow_steps
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_automatic_decisions import (
    AutomaticDecisionBase,
)

States = models.Review.States
NOTIFY = "waldur_mastermind.proposal.tasks.notify_proposal_decision"


class OpenReviewWork:
    """A review being written, one submitted, an assignment not yet answered
    and one accepted, on ``self.proposal``."""

    def add_review_work(self):
        call = self.proposal.round.call
        self.reviewer = structure_factories.UserFactory()
        self.open_review = factories.ReviewFactory(
            proposal=self.proposal, reviewer=self.reviewer, state=States.IN_REVIEW
        )
        self.submitted = factories.ReviewFactory(
            proposal=self.proposal, state=States.SUBMITTED, summary_score=4
        )
        self.batch = factories.AssignmentBatchFactory(
            call=call, status=AssignmentBatchStatuses.SENT
        )
        self.pending_item = factories.AssignmentItemFactory(
            batch=self.batch, proposal=self.proposal
        )
        self.accepted_item = factories.AssignmentItemFactory(
            batch=factories.AssignmentBatchFactory(
                call=call, status=AssignmentBatchStatuses.RESPONDED
            ),
            proposal=self.proposal,
            status=AssignmentItemStatuses.ACCEPTED,
            review=self.open_review,
        )

    def assert_closed(self):
        self.open_review.refresh_from_db()
        self.assertEqual(self.open_review.state, States.REJECTED)
        self.assertEqual(
            self.open_review.rejection_cause, ReviewRejectionCauses.PROPOSAL_DECIDED
        )
        # Its contribution, on whichever step, is withdrawn with it.
        self.assertFalse(
            models.StepContribution.objects.filter(
                review=self.open_review, status__in=StepContributionStatuses.OPEN
            ).exists()
        )
        # Submitted reviews and their scores are kept.
        self.submitted.refresh_from_db()
        self.assertEqual(self.submitted.state, States.SUBMITTED)
        self.assertEqual(self.submitted.rejection_cause, "")
        self.assertEqual(self.submitted.summary_score, 4)

        self.pending_item.refresh_from_db()
        self.assertEqual(self.pending_item.status, AssignmentItemStatuses.EXPIRED)
        self.assertEqual(self.pending_item.decline_reason, handlers.DECIDED_REASON)
        # An accepted item keeps its status: its review is closed instead.
        self.accepted_item.refresh_from_db()
        self.assertEqual(self.accepted_item.status, AssignmentItemStatuses.ACCEPTED)
        # The batch has nothing left to answer.
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, AssignmentBatchStatuses.RESPONDED)

        # Logged on the review's feed, once.
        events = Event.objects.filter(
            event_type=EventType.REVIEW_CANCELED,
            feed__content_type=ContentType.objects.get_for_model(models.Review),
            feed__object_id=self.open_review.id,
        )
        self.assertEqual(events.count(), 1)
        return events.get()

    def assert_open(self):
        self.open_review.refresh_from_db()
        self.assertEqual(self.open_review.state, States.IN_REVIEW)
        self.pending_item.refresh_from_db()
        self.assertEqual(self.pending_item.status, AssignmentItemStatuses.PENDING)


class ManualDecisionBase(OpenReviewWork, test.APITestCase):
    """A proposal in review at an administrative check, followed by the
    allocation decision."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")
        self.check = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.decision = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        self.proposal.refresh_from_db()
        self.add_review_work()

    def stand_at_decision(self):
        self.check.status = WorkflowStepInstanceStatuses.COMPLETED
        self.check.save()
        self.decision.status = WorkflowStepInstanceStatuses.ACTIVE
        self.decision.started_at = timezone.now()
        self.decision.save()
        factories.point_at_step(self.proposal, "allocation_decision")
        self.proposal.save()

    def post(self, action, payload):
        self.client.force_authenticate(self.fixture.staff)
        with mock.patch(NOTIFY):
            return self.client.post(
                factories.ProposalFactory.get_url(self.proposal, action=action),
                payload,
            )


class ManualDecisionTest(ManualDecisionBase):
    def test_accepting_at_the_end_of_the_workflow_closes_them(self):
        self.stand_at_decision()
        response = self.post(
            "complete_workflow_step",
            {
                "step_uuid": self.decision.uuid.hex,
                "outcome": WorkflowStepOutcomes.APPROVED,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assert_closed()

    def test_rejecting_at_a_step_closes_them(self):
        response = self.post(
            "reject_workflow_step",
            {"step_uuid": self.check.uuid.hex, "reason": "Not eligible"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)
        event = self.assert_closed()
        # Logged as no one's, not as the call manager's who decided.
        self.assertNotIn("user_uuid", event.context)

    def test_completing_a_step_that_does_not_end_the_workflow_leaves_them(self):
        response = self.post(
            "complete_workflow_step",
            {"step_uuid": self.check.uuid.hex, "outcome": "eligible"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assert_open()

    def test_rejecting_an_undecided_proposal_at_round_completion_closes_them(self):
        with transaction.atomic():
            proposal = models.Proposal.objects.select_for_update().get(
                pk=self.proposal.pk
            )
            workflow_service.reject_undecided(proposal, self.fixture.staff)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)
        self.assert_closed()

    def test_a_lapsed_last_step_rejects_and_closes_them(self):
        self.decision.delete()
        self.check.deadline = timezone.now() - timedelta(hours=1)
        self.check.save()
        with mock.patch(NOTIFY):
            self.assertEqual(mark_expired_workflow_steps(), 1)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)
        self.assert_closed()

    def test_withdrawal_closes_them_as_before(self):
        fixtures.author_as_manager(self.proposal)
        self.client.force_authenticate(self.proposal.created_by)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                factories.ProposalFactory.get_url(self.proposal, "withdraw"),
                {"reason": "Funded elsewhere."},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.open_review.refresh_from_db()
        self.assertEqual(self.open_review.state, States.REJECTED)
        # A withdrawal is not a decision: the review keeps the closure it
        # had before.
        self.assertEqual(self.open_review.rejection_cause, "")
        self.pending_item.refresh_from_db()
        self.assertEqual(self.pending_item.status, AssignmentItemStatuses.EXPIRED)
        self.assertEqual(self.pending_item.decline_reason, "")
        self.submitted.refresh_from_db()
        self.assertEqual(self.submitted.state, States.SUBMITTED)

    def test_each_final_state_closes_them(self):
        for state in (
            ProposalStates.ACCEPTED,
            ProposalStates.REJECTED,
            ProposalStates.CANCELED,
        ):
            with self.subTest(state=state):
                self.add_review_work()
                self.proposal.state = state
                self.proposal.save()
                self.assert_closed()
                self.proposal.state = ProposalStates.IN_REVIEW
                self.proposal.save()

    def test_a_proposal_still_in_evaluation_keeps_them(self):
        # Sent back to its applicant, say: the proposal stays in review.
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        self.assert_open()

    def test_a_save_that_does_not_change_the_state_leaves_them(self):
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        reopened = factories.ReviewFactory(
            proposal=self.proposal, state=States.IN_REVIEW
        )
        self.proposal.name = "Renamed"
        self.proposal.save()
        reopened.refresh_from_db()
        self.assertEqual(reopened.state, States.IN_REVIEW)

    def test_the_api_tells_why_the_review_was_closed(self):
        self.proposal.state = ProposalStates.REJECTED
        self.proposal.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.ReviewFactory.get_url(self.open_review))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["rejection_cause"], ReviewRejectionCauses.PROPOSAL_DECIDED
        )

    def test_the_cause_cannot_be_written(self):
        self.client.force_authenticate(self.reviewer)
        response = self.client.patch(
            factories.ReviewFactory.get_url(self.open_review),
            {"rejection_cause": ReviewRejectionCauses.DECLINED},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.open_review.refresh_from_db()
        self.assertEqual(self.open_review.rejection_cause, "")


class LaterReviewStepTest(ManualDecisionBase):
    """The reviewer accepted a batch for a review step after the screening:
    their contribution sits on a step the rejection at the screening does
    not end."""

    def setUp(self):
        super().setUp()
        review_step = factories.CallWorkflowStepFactory(
            call=self.call, step="expert_review"
        )
        self.review_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="expert_review",
            call_step=review_step,
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        batch = self.accepted_item.batch
        batch.call_step = review_step
        batch.save(update_fields=["call_step"])
        self.contribution = contributions.attach_review(
            self.open_review, assignment_item=self.accepted_item
        )
        self.assertEqual(self.contribution.proposal_step, self.review_instance)
        self.assertEqual(self.contribution.status, StepContributionStatuses.ACCEPTED)

    def test_rejecting_at_the_screening_withdraws_its_contribution(self):
        response = self.post(
            "reject_workflow_step",
            {"step_uuid": self.check.uuid.hex, "reason": "Not eligible"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assert_closed()
        self.contribution.refresh_from_db()
        self.assertEqual(self.contribution.status, StepContributionStatuses.WITHDRAWN)


class ConcurrentAnswerTest(ManualDecisionBase):
    """What a reviewer does while the proposal is being decided."""

    def test_a_review_accepted_while_the_items_are_locked_is_closed(self):
        # An accept holding the item's lock commits its review before the
        # decision's lock on the items is granted: the review it made is
        # closed too.
        expire = handlers._expire_pending_assignments

        def accepted_meanwhile(proposal):
            review = factories.ReviewFactory(proposal=proposal, state=States.IN_REVIEW)
            models.AssignmentItem.objects.filter(pk=self.pending_item.pk).update(
                status=AssignmentItemStatuses.ACCEPTED, review=review
            )
            self.late_review = review
            return expire(proposal)

        with mock.patch.object(
            handlers, "_expire_pending_assignments", side_effect=accepted_meanwhile
        ):
            self.proposal.state = ProposalStates.REJECTED
            self.proposal.save()
        self.late_review.refresh_from_db()
        self.assertEqual(self.late_review.state, States.REJECTED)
        self.assertEqual(
            self.late_review.rejection_cause, ReviewRejectionCauses.PROPOSAL_DECIDED
        )

    def test_a_submission_validated_before_the_closure_does_not_reopen_it(self):
        # The validator read the review in review; the decision closed it
        # before the submission took its lock.
        self.proposal.state = ProposalStates.REJECTED
        self.proposal.save()
        self.client.force_authenticate(self.reviewer)
        with mock.patch.object(views.ReviewViewSet, "submit_validators", []):
            response = self.client.post(
                factories.ReviewFactory.get_url(self.open_review, "submit"),
                {"summary_score": "4"},
            )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.open_review.refresh_from_db()
        self.assertEqual(self.open_review.state, States.REJECTED)
        self.assertEqual(
            self.open_review.rejection_cause, ReviewRejectionCauses.PROPOSAL_DECIDED
        )


class ReviewerAfterDecisionTest(ManualDecisionBase):
    """The reviewer whose review was closed: the claims of the issue,
    checked before and after the decision."""

    def test_reviewer_no_longer_reads_the_proposal_through_it(self):
        self.assertTrue(managers.holds_live_review(self.reviewer, self.proposal))
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        self.assertFalse(managers.holds_live_review(self.reviewer, self.proposal))
        self.assertNotIn(
            self.proposal.id, list(managers.get_reviewed_proposals(self.reviewer))
        )

    def test_reviewer_is_no_longer_reminded_of_its_deadline(self):
        round_ = self.proposal.round
        round_.review_duration_in_days = 2
        round_.save()

        def reminded():
            with mock.patch.object(
                proposal_tasks.core_utils, "broadcast_mail"
            ) as broadcast:
                proposal_tasks.notify_reviewer_on_review_deadline_approaching()
            return [
                call.args[3]
                for call in broadcast.call_args_list
                if call.args[1] == "review_deadline_approaching"
                and self.reviewer.email in call.args[3]
            ]

        self.assertEqual(len(reminded()), 1)
        self.proposal.state = ProposalStates.REJECTED
        self.proposal.save()
        self.assertEqual(reminded(), [])

    def test_reviewer_is_not_emailed_about_the_closure(self):
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.proposal.state = ProposalStates.REJECTED
            self.proposal.save()
        self.assertFalse([m for m in mail.outbox if self.reviewer.email in m.to])


class HeldDecisionTest(ManualDecisionBase):
    """A decision held until the round publishes its results closes nothing
    until it is released."""

    def setUp(self):
        super().setUp()
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save(update_fields=["publish_results"])
        round_ = self.proposal.round
        round_.lifecycle_state = RoundLifecycleStates.EVALUATING
        round_.save(update_fields=["lifecycle_state"])
        self.stand_at_decision()

    def test_held_decision_closes_them_on_release(self):
        response = self.post(
            "complete_workflow_step",
            {
                "step_uuid": self.decision.uuid.hex,
                "outcome": WorkflowStepOutcomes.APPROVED,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertTrue(self.proposal.decision_held)
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assert_open()

        with transaction.atomic(), mock.patch(NOTIFY):
            proposal = models.Proposal.objects.select_for_update().get(
                pk=self.proposal.pk
            )
            workflow_service.release_held_decision(proposal)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assert_closed()

    def test_held_rejection_closes_them_on_release(self):
        response = self.post(
            "reject_workflow_step",
            {"step_uuid": self.decision.uuid.hex, "reason": "Out of budget"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertTrue(self.proposal.decision_held)
        self.assert_open()

        with transaction.atomic(), mock.patch(NOTIFY):
            proposal = models.Proposal.objects.select_for_update().get(
                pk=self.proposal.pk
            )
            workflow_service.release_held_decision(proposal)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)
        self.assert_closed()


class AutomaticDecisionClosureTest(OpenReviewWork, AutomaticDecisionBase):
    def setUp(self):
        super().setUp()
        self.add_review_work()

    def test_automatic_award_closes_them(self):
        with mock.patch(NOTIFY):
            response = self._assess_both()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        event = self.assert_closed()
        # The assessor's request decided the proposal; the closure of
        # another reviewer's review is not theirs.
        self.assertNotIn("user_uuid", event.context)

    def test_a_negative_outcome_ending_the_workflow_closes_them(self):
        with mock.patch(NOTIFY):
            self._assess_both(ContributionOutcomes.REJECTED)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)
        event = self.assert_closed()
        # The assessor's request rejected the proposal; the closure of
        # another reviewer's review is not theirs.
        self.assertNotIn("user_uuid", event.context)
