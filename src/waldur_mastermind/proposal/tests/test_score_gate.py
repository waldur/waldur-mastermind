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
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, workflow_service
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    OutcomeSets,
    ProposalStates,
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
