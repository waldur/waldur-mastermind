"""Migrating the work already done on proposals into contributions.

The test database is migrated back to before the call workflow engine, filled
the way the catalogue engine left it -- reviews on the review step, technical
assessment answers told apart by their user on the proposal's completion --
and migrated forward again. The answers must appear as one contribution per
answering user with the same answers, and the review gate must count the same
reviews as before.
"""

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.utils import timezone

from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, workflow_service
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    AssignmentSources,
    ContributorSources,
    ProposalStates,
    StepContributionStatuses,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, migration_helpers

BEFORE = [("proposal", "0092_delete_empty_proposal_documents")]

CATALOGUE = [
    "administrative_check",
    "technical_assessment",
    "expert_review",
    "panel_review",
    "allocation_decision",
    "award_response",
]

ACTIVE = WorkflowStepInstanceStatuses.ACTIVE
COMPLETED = WorkflowStepInstanceStatuses.COMPLETED
PENDING = WorkflowStepInstanceStatuses.PENDING


@override_settings(MIGRATION_MODULES={})
class StepContributionMigrationTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.call = factories.CallFactory()
        cls.round = factories.RoundFactory(call=cls.call)
        # The catalogue engine predates them; it is rebuilt below.
        models.CallWorkflowStep.objects.filter(call=cls.call).delete()
        cls.form = checklist_factories.ChecklistFactory()
        cls.decision = checklist_factories.QuestionFactory(
            checklist=cls.form, required=True
        )
        cls.comment = checklist_factories.QuestionFactory(checklist=cls.form)
        cls.compliance = checklist_factories.ChecklistFactory()
        cls.compliance_question = checklist_factories.QuestionFactory(
            checklist=cls.compliance
        )
        cls.call.compliance_checklist = cls.compliance
        cls.call.save()

        cls.assessors = [structure_factories.UserFactory() for _ in range(2)]
        cls.reviewers = [structure_factories.UserFactory() for _ in range(3)]
        cls.pool_entry = factories.CallReviewerPoolFactory(call=cls.call)

        # Past its technical assessment, standing in the expert review.
        cls.reviewed = factories.ProposalFactory(
            round=cls.round, state=ProposalStates.IN_REVIEW
        )
        # Still in its technical assessment, one assessor half way through.
        cls.assessing = factories.ProposalFactory(
            round=cls.round, state=ProposalStates.IN_REVIEW
        )

        cls._fill_catalogue_world(migration_helpers.migrate_back(BEFORE))
        migration_helpers.migrate_forward()

    @classmethod
    def _fill_catalogue_world(cls, apps):
        Step = apps.get_model("proposal", "CallWorkflowStep")
        Instance = apps.get_model("proposal", "ProposalWorkflowStepInstance")
        Proposal = apps.get_model("proposal", "Proposal")
        Review = apps.get_model("proposal", "Review")
        AssignmentBatch = apps.get_model("proposal", "AssignmentBatch")
        AssignmentItem = apps.get_model("proposal", "AssignmentItem")
        Completion = apps.get_model("checklist", "ChecklistCompletion")
        Answer = apps.get_model("checklist", "Answer")

        for display_order, key in enumerate(CATALOGUE):
            Step.objects.create(
                call_id=cls.call.pk,
                step=key,
                is_enabled=True,
                display_order=display_order,
                checklist_id=cls.form.pk if key == "technical_assessment" else None,
                min_reviewers=3 if key == "expert_review" else None,
            )

        now = timezone.now()
        for proposal, statuses in (
            (
                cls.reviewed,
                {
                    "administrative_check": COMPLETED,
                    "technical_assessment": COMPLETED,
                    "expert_review": ACTIVE,
                },
            ),
            (
                cls.assessing,
                {
                    "administrative_check": COMPLETED,
                    "technical_assessment": ACTIVE,
                },
            ),
        ):
            current = None
            for key in CATALOGUE:
                status = statuses.get(key, PENDING)
                Instance.objects.create(
                    proposal_id=proposal.pk,
                    step=key,
                    status=status,
                    started_at=now if status != PENDING else None,
                )
                if status == ACTIVE:
                    current = key
            Proposal.objects.filter(pk=proposal.pk).update(workflow_step=current)

        cls.submitted_review_ids = [
            Review.objects.create(
                proposal_id=cls.reviewed.pk,
                reviewer_id=reviewer.pk,
                state=models.Review.States.SUBMITTED,
            ).pk
            for reviewer in cls.reviewers[:2]
        ]
        open_review = Review.objects.create(
            proposal_id=cls.reviewed.pk,
            reviewer_id=cls.reviewers[2].pk,
            state=models.Review.States.IN_REVIEW,
        )
        cls.open_review_id = open_review.pk
        cls.open_reviewer_id = open_review.reviewer_id
        batch = AssignmentBatch.objects.create(
            call_id=cls.call.pk,
            reviewer_pool_entry_id=cls.pool_entry.pk,
            status=AssignmentBatchStatuses.DRAFT,
            source=AssignmentSources.MANUAL,
            invitation_token="historical-batch",
        )
        cls.batch_id = batch.pk
        cls.item_id = AssignmentItem.objects.create(
            batch_id=batch.pk,
            proposal_id=cls.reviewed.pk,
            status=AssignmentItemStatuses.ACCEPTED,
            review_id=open_review.pk,
        ).pk

        proposal_ct = ContentType.objects.get_for_model(models.Proposal)

        # Creating the proposal already opened its compliance completion.
        def completion(proposal, checklist):
            return Completion.objects.get_or_create(
                scope_content_type_id=proposal_ct.pk,
                scope_object_id=proposal.pk,
                checklist_id=checklist.pk,
            )[0]

        def answer(completion, question, user, value):
            Answer.objects.create(
                completion_id=completion.pk,
                question_id=question.pk,
                user_id=user.pk,
                answer_data=value,
            )

        # Both assessors answered on the proposal's one completion of the form.
        form_completion = completion(cls.reviewed, cls.form)
        for index, user in enumerate(cls.assessors):
            answer(form_completion, cls.decision, user, f"decision {index}")
            answer(form_completion, cls.comment, user, f"comment {index}")
        answer(
            completion(cls.reviewed, cls.compliance),
            cls.compliance_question,
            cls.reviewed.created_by,
            "compliant",
        )
        answer(
            completion(cls.assessing, cls.form),
            cls.comment,
            cls.assessors[0],
            "draft",
        )

    def _instance(self, proposal, key):
        return proposal.workflow_step_instances.get(step=key)

    def test_steps_say_who_contributes(self):
        steps = {step.key: step for step in self.call.workflow_steps.all()}
        self.assertEqual(
            steps["technical_assessment"].contributor_source,
            ContributorSources.REQUESTED_OFFERING_TEAM,
        )
        self.assertEqual(steps["technical_assessment"].checklist, self.form)
        self.assertEqual(
            steps["expert_review"].contributor_source,
            ContributorSources.ASSIGNED_REVIEWERS,
        )
        self.assertEqual(steps["expert_review"].min_contributions, 3)
        self.assertEqual(
            steps["panel_review"].contributor_source, ContributorSources.NONE
        )

    def test_every_review_has_a_contribution_on_the_review_step(self):
        instance = self._instance(self.reviewed, "expert_review")
        contributions = {c.review_id: c for c in instance.contributions.all()}
        self.assertEqual(
            set(contributions), {*self.submitted_review_ids, self.open_review_id}
        )
        for review_id in self.submitted_review_ids:
            self.assertEqual(
                contributions[review_id].status, StepContributionStatuses.SUBMITTED
            )
        opened = contributions[self.open_review_id]
        self.assertEqual(opened.status, StepContributionStatuses.ACCEPTED)
        self.assertEqual(opened.assignment_item_id, self.item_id)
        self.assertEqual(opened.contributor_id, self.open_reviewer_id)

    def test_review_gate_counts_the_same_reviews(self):
        proposal = models.Proposal.objects.get(pk=self.reviewed.pk)
        instance = self._instance(proposal, "expert_review")
        self.assertEqual(proposal.current_step, instance)
        with self.assertRaisesRegex(ValueError, "2 of 3 required reviews"):
            workflow_service.complete_step(
                proposal, instance, WorkflowStepOutcomes.REVIEWED, "", None
            )

    def test_answers_become_one_contribution_per_answering_user(self):
        instance = self._instance(self.reviewed, "technical_assessment")
        contributions = list(instance.contributions.order_by("contributor_id"))
        self.assertEqual(
            [c.contributor_id for c in contributions],
            sorted(user.id for user in self.assessors),
        )
        for contribution in contributions:
            self.assertEqual(contribution.status, StepContributionStatuses.SUBMITTED)
            index = self.assessors.index(contribution.contributor)
            completion = contribution.get_checklist_completion(self.form)
            self.assertEqual(
                {a.question_id: a.answer_data for a in completion.answers.all()},
                {
                    self.decision.id: f"decision {index}",
                    self.comment.id: f"comment {index}",
                },
            )
            self.assertTrue(completion.is_completed)

    def test_the_proposals_completion_of_the_form_is_gone(self):
        self.assertIsNone(self.reviewed.get_checklist_completion_for(self.form))
        self.assertIsNone(self.assessing.get_checklist_completion_for(self.form))

    def test_compliance_answers_stay_on_the_proposal(self):
        completion = self.reviewed.get_checklist_completion_for(self.compliance)
        self.assertEqual(completion.answers.get().answer_data, "compliant")

    def test_answers_of_a_step_in_progress_stay_editable(self):
        instance = self._instance(self.assessing, "technical_assessment")
        contribution = instance.contributions.get()
        self.assertEqual(contribution.contributor, self.assessors[0])
        self.assertEqual(contribution.status, StepContributionStatuses.PENDING)
        completion = contribution.get_checklist_completion(self.form)
        self.assertEqual(completion.answers.get().answer_data, "draft")
        self.assertFalse(completion.is_completed)

    def test_batches_contribute_to_the_review_step(self):
        batch = models.AssignmentBatch.objects.get(pk=self.batch_id)
        self.assertEqual(
            batch.call_step,
            self.call.workflow_steps.get(key="expert_review"),
        )
