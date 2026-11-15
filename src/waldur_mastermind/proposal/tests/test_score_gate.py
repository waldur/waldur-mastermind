"""A decision's quality threshold checked against the score of its input steps.

The call runs an expert evaluation (assigned reviewers), a consensus report
(one expert grades the proposal on rating questions) and the allocation
decision, which reads both and carries the threshold.
"""

from decimal import Decimal

from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, permissions, workflow_service
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    OutcomeSets,
    ProposalStates,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, fixtures

COMPLETED = WorkflowStepInstanceStatuses.COMPLETED


class ScoreGateTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()

        self.expert_step = self._add_step(
            "gate-experts",
            "Expert evaluation",
            20,
            contributor_source=ContributorSources.ASSIGNED_REVIEWERS,
            outcome_set=OutcomeSets.REVIEWED,
        )
        self.form = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.WORKFLOW_STEP
        )
        self.ratings = [
            checklist_factories.QuestionFactory(
                checklist=self.form,
                description=f"Score {n}",
                question_type=checklist_enums.QuestionTypes.RATING,
                required=False,
            )
            for n in range(2)
        ]
        self.comment = checklist_factories.QuestionFactory(
            checklist=self.form,
            description="Comment",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
            required=False,
        )
        self.consensus_step = self._add_step(
            "gate-consensus",
            "Consensus report",
            21,
            contributor_source="call_role:call_manager",
            outcome_set=OutcomeSets.DONE,
            checklist=self.form,
        )
        self.decision_step = factories.CallWorkflowStepFactory(
            call=self.call,
            step="allocation_decision",
            min_score_threshold=Decimal("3.00"),
            order=22,
        )
        self.decision_step.input_steps.set([self.expert_step, self.consensus_step])

        self.expert_instance = self._instance(self.expert_step, COMPLETED)
        self.consensus_instance = self._instance(self.consensus_step, COMPLETED)
        self.decision = self._instance(
            self.decision_step, WorkflowStepInstanceStatuses.ACTIVE
        )
        self.decision.input_steps.set([self.expert_step, self.consensus_step])

    def _add_step(self, key, name, order, **values):
        models.CallWorkflowStep.make_room(self.call.pk, order)
        return models.CallWorkflowStep.objects.create(
            call=self.call,
            key=key,
            kind=StepKinds.ASSESSMENT,
            name=name,
            order=order,
            **values,
        )

    def _instance(self, call_step, status):
        return factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            call_step=call_step,
            step=call_step.key,
            order=call_step.order,
            status=status,
            started_at=timezone.now(),
            is_allocation_decision=call_step.is_allocation_decision,
        )

    def _expert_reviews(self, *scores, reviewers=()):
        reviewers = list(reviewers)
        for score in scores:
            review = factories.ReviewFactory(
                proposal=self.proposal,
                reviewer=reviewers.pop(0)
                if reviewers
                else structure_factories.UserFactory(),
                state=models.Review.States.SUBMITTED,
                summary_score=score,
            )
            # A review comes with its contribution; it counts on the experts' step.
            models.StepContribution.objects.update_or_create(
                review=review,
                defaults={
                    "proposal_step": self.expert_instance,
                    "contributor": review.reviewer,
                    "status": StepContributionStatuses.SUBMITTED,
                },
            )

    def _consensus(self, *ratings, text="Agreed."):
        user = self.fixture.call_manager
        contribution = factories.StepContributionFactory(
            proposal_step=self.consensus_instance,
            contributor=user,
            status=StepContributionStatuses.SUBMITTED,
        )
        completion = contribution.ensure_checklist_completion(self.form)
        for question, value in zip(self.ratings, ratings):
            checklist_models.Answer.objects.create(
                completion=completion, question=question, user=user, answer_data=value
            )
        checklist_models.Answer.objects.create(
            completion=completion, question=self.comment, user=user, answer_data=text
        )

    def _gate(self, outcome=WorkflowStepOutcomes.APPROVED):
        workflow_service._enforce_step_gates(
            self.proposal, self.decision, outcome, self.decision_step
        )


class ScoreFromInputStepsTest(ScoreGateTestBase):
    def test_consensus_grading_decides_when_made(self):
        # The experts scored high, the consensus graded low: the grading counts.
        self._expert_reviews(5, 5)
        self._consensus(2, 3)
        score, source = workflow_service.gate_score(self.decision)
        self.assertEqual(score, 2.5)
        self.assertEqual(source, self.consensus_instance)
        with self.assertRaisesRegex(ValueError, "Consensus report.*2.50.*3.00"):
            self._gate()

    def test_consensus_grading_above_threshold_passes(self):
        self._expert_reviews(1, 2)
        self._consensus(4, "5")
        self._gate()  # must not raise

    def test_experts_average_decides_without_a_consensus_report(self):
        self._expert_reviews(4, 3)
        score, source = workflow_service.gate_score(self.decision)
        self.assertEqual(score, 3.5)
        self.assertEqual(source, self.expert_instance)
        self._gate()

    def test_a_report_without_ratings_falls_back_to_the_experts(self):
        self._expert_reviews(2, 2)
        self._consensus()
        with self.assertRaisesRegex(ValueError, "Expert evaluation.*2.00"):
            self._gate()

    def test_an_input_step_still_open_is_not_read(self):
        self._expert_reviews(4, 4)
        self._consensus(1, 1)
        self.consensus_instance.status = WorkflowStepInstanceStatuses.EXPIRED
        self.consensus_instance.save()
        _, source = workflow_service.gate_score(self.decision)
        self.assertEqual(source, self.expert_instance)

    def test_declining_is_always_allowed(self):
        self._expert_reviews(1, 1)
        self._consensus(1, 1)
        self._gate(WorkflowStepOutcomes.DECLINED)  # must not raise

    def test_approving_below_the_threshold_is_refused_through_the_api(self):
        self._expert_reviews(1, 1)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="complete_workflow_step"
            ),
            {
                "step_uuid": self.decision.uuid.hex,
                "outcome": WorkflowStepOutcomes.APPROVED,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("3.00", str(response.data))
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, WorkflowStepInstanceStatuses.ACTIVE)

    def test_without_input_steps_all_reviews_count_as_before(self):
        self.decision.input_steps.clear()
        self._consensus(1, 1)
        self._expert_reviews(4, 4)
        score, source = workflow_service.gate_score(self.decision)
        self.assertEqual(score, 4)
        self.assertIsNone(source)


class ScoreGateVisibilityTest(ScoreGateTestBase):
    def setUp(self):
        super().setUp()
        # One of the experts is a call reviewer: their part is the experts'
        # step, not the decision.
        self._expert_reviews(2, 3, reviewers=[self.fixture.reviewer_1])
        self._consensus(2, 2)

    def _score_gate(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = {row["uuid"]: row for row in response.data}
        return rows[self.decision.uuid.hex]["score_gate"]

    def test_call_manager_sees_the_gate(self):
        gate = self._score_gate(self.fixture.call_manager)
        self.assertEqual(
            gate,
            {
                "score": 2.0,
                "threshold": 3.0,
                "source_step_name": "Consensus report",
                "passes": False,
            },
        )

    def test_staff_sees_the_gate(self):
        self.assertIsNotNone(self._score_gate(self.fixture.staff))

    def test_applicant_does_not_see_the_gate(self):
        self.assertIsNone(self._score_gate(self.fixture.proposal_creator))

    def test_expert_outside_the_decision_does_not_see_the_gate(self):
        self.assertIsNone(self._score_gate(self.fixture.reviewer_1))

    def test_no_gate_without_a_threshold(self):
        self.decision_step.min_score_threshold = None
        self.decision_step.save()
        self.assertIsNone(self._score_gate(self.fixture.call_manager))

    def test_no_gate_once_decided(self):
        self.decision.status = COMPLETED
        self.decision.save()
        self.assertIsNone(self._score_gate(self.fixture.call_manager))


class ReviewerStepScoreConcealmentTest(test.APITestCase):
    """On a step whose gate averages its own reviews, the score is the call
    team's. A reviewer taking part in the step reads neither the score nor
    whether it passes: with two reviewers, the average and their own score
    would tell them their peer's."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        models.CallWorkflowStep.make_room(self.call.pk, 20)
        self.expert_step = models.CallWorkflowStep.objects.create(
            call=self.call,
            key="gate-experts",
            kind=StepKinds.ASSESSMENT,
            name="Expert evaluation",
            order=20,
            contributor_source=ContributorSources.ASSIGNED_REVIEWERS,
            outcome_set=OutcomeSets.REVIEWED,
            completer=ResponsibleRoles.REVIEWER,
            min_score_threshold=Decimal("4.00"),
        )
        self.instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            call_step=self.expert_step,
            step=self.expert_step.key,
            order=self.expert_step.order,
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.reviewer = self.fixture.reviewer_1
        for reviewer, score in ((self.reviewer, 5), (self.fixture.reviewer_2, 1)):
            review = factories.ReviewFactory(
                proposal=self.proposal,
                reviewer=reviewer,
                state=models.Review.States.SUBMITTED,
                summary_score=score,
            )
            models.StepContribution.objects.update_or_create(
                review=review,
                defaults={
                    "proposal_step": self.instance,
                    "contributor": reviewer,
                    "status": StepContributionStatuses.SUBMITTED,
                },
            )

    def _row(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = {row["uuid"]: row for row in response.data}
        return rows[self.instance.uuid.hex]

    def _complete(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="complete_workflow_step"
            ),
            {
                "step_uuid": self.instance.uuid.hex,
                "outcome": WorkflowStepOutcomes.REVIEWED,
            },
        )

    def test_call_manager_sees_the_score(self):
        self.assertEqual(
            self._row(self.fixture.call_manager)["score_gate"],
            {
                "score": 3.0,
                "threshold": 4.0,
                "source_step_name": None,
                "passes": False,
            },
        )

    def test_reviewer_on_the_step_does_not_see_the_score(self):
        self.assertIsNone(self._row(self.reviewer)["score_gate"])

    def test_reviewer_readiness_does_not_follow_the_score(self):
        failing = self._row(self.reviewer)["ready_to_complete"]
        self.expert_step.min_score_threshold = Decimal("2.00")
        self.expert_step.save()
        passing = self._row(self.reviewer)["ready_to_complete"]
        self.assertEqual(failing, passing)
        self.assertTrue(passing)

    def test_call_manager_readiness_follows_the_score(self):
        self.assertFalse(self._row(self.fixture.call_manager)["ready_to_complete"])

    def test_reviewer_completing_is_refused_without_the_numbers(self):
        response = self._complete(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = str(response.data["detail"])
        self.assertIn("below the required minimum", detail)
        self.assertNotIn("3.0", detail)
        self.assertNotIn("4.00", detail)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowStepInstanceStatuses.ACTIVE)

    def test_call_manager_completing_is_told_the_numbers(self):
        response = self._complete(self.fixture.call_manager)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = str(response.data["detail"])
        self.assertIn("3.0", detail)
        self.assertIn("4.00", detail)


class CompletingWhileEvaluationsAreOpenTest(test.APITestCase):
    """A completer who does not read the step's score cannot complete a step
    with a score threshold while its evaluations are still open: the gate's
    answer would tell a reviewer who has not submitted about the others'
    scores. The call team may still close the step early."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        models.CallWorkflowStep.make_room(self.call.pk, 20)
        self.expert_step = models.CallWorkflowStep.objects.create(
            call=self.call,
            key="gate-experts",
            kind=StepKinds.ASSESSMENT,
            name="Expert evaluation",
            order=20,
            contributor_source=ContributorSources.ASSIGNED_REVIEWERS,
            outcome_set=OutcomeSets.REVIEWED,
            completer=ResponsibleRoles.REVIEWER,
            min_score_threshold=Decimal("4.00"),
        )
        self.instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            call_step=self.expert_step,
            step=self.expert_step.key,
            order=self.expert_step.order,
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.reviewer = self.fixture.reviewer_1
        self.pending_review = self._review(
            self.reviewer,
            models.Review.States.IN_REVIEW,
            None,
            StepContributionStatuses.ACCEPTED,
        )
        self.peer_review = self._review(
            self.fixture.reviewer_2,
            models.Review.States.SUBMITTED,
            1,
            StepContributionStatuses.SUBMITTED,
        )

    def _review(self, reviewer, state, score, contribution_status):
        review = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=reviewer,
            state=state,
            **({} if score is None else {"summary_score": score}),
        )
        models.StepContribution.objects.update_or_create(
            review=review,
            defaults={
                "proposal_step": self.instance,
                "contributor": reviewer,
                "status": contribution_status,
            },
        )
        return review

    def _complete(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="complete_workflow_step"
            ),
            {
                "step_uuid": self.instance.uuid.hex,
                "outcome": WorkflowStepOutcomes.REVIEWED,
            },
        )

    def _assert_refused_while_open(self, response):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = str(response.data["detail"])
        self.assertIn("while its evaluations are still open", detail)
        self.assertNotIn("minimum", detail)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    def test_reviewer_is_refused_without_learning_a_low_score(self):
        self._assert_refused_while_open(self._complete(self.reviewer))

    def test_reviewer_is_refused_without_learning_a_passing_score(self):
        self.peer_review.summary_score = 5
        self.peer_review.save()
        self._assert_refused_while_open(self._complete(self.reviewer))

    def test_reviewer_completes_once_the_evaluations_are_closed(self):
        self.peer_review.summary_score = 5
        self.peer_review.save()
        self.pending_review.state = models.Review.States.SUBMITTED
        self.pending_review.summary_score = 4
        self.pending_review.save()
        models.StepContribution.objects.filter(review=self.pending_review).update(
            status=StepContributionStatuses.SUBMITTED
        )
        response = self._complete(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, COMPLETED)

    def test_call_manager_is_told_the_score_as_before(self):
        response = self._complete(self.fixture.call_manager)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = str(response.data["detail"])
        self.assertIn("below the required minimum", detail)
        self.assertIn("4.00", detail)

    def test_call_manager_closes_the_step_early_as_before(self):
        self.peer_review.summary_score = 5
        self.peer_review.save()
        response = self._complete(self.fixture.call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, COMPLETED)


class StepScoresAndStaffScopeTest(test.APITestCase):
    """A staff user reads a step's score as staff only when the request acts
    as staff; a token without the staff scope does not."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.staff = self.fixture.staff

    def test_staff_acting_as_staff_reads_scores(self):
        self.assertTrue(
            permissions.user_can_view_step_scores(self.staff, self.proposal, True)
        )
        self.assertTrue(
            permissions.user_can_view_step_scores(self.staff, self.proposal)
        )

    def test_staff_without_the_staff_scope_does_not_read_scores(self):
        self.assertFalse(
            permissions.user_can_view_step_scores(
                self.staff, self.proposal, staff=False
            )
        )
        self.assertFalse(
            permissions.user_can_view_review_scores(
                self.staff, self.proposal, staff=False
            )
        )

    def test_staff_without_the_staff_scope_reads_scores_by_call_role(self):
        self.fixture.call.add_user(self.staff, CallRole.MANAGER)
        self.assertTrue(
            permissions.user_can_view_step_scores(
                self.staff, self.proposal, staff=False
            )
        )
