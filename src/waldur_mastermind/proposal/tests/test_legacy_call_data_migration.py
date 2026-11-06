"""Develop's call data the call workflow migrations must carry over unharmed.

Each class migrates the test database back to the state before the migration
under test, fills it the way develop left real deployments, migrates forward
again and checks what became of it:

- a step form shared by two steps, whose answers both steps read;
- draft and active calls older than the step rows, which have none;
- holders of a call's reviewer role whose pool entry is an invitation sent to
  their address, or one they declined;
- what is left for a person to settle, which the migrations log: a score
  threshold nothing scores, and imported reviewer stats that are dropped.
"""

import logging
from contextlib import contextmanager

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.utils import timezone

from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import contributions, models, workflow_service
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    ReviewerPoolInvitationStatuses,
    StepContributionStatuses,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, migration_helpers

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
SKIPPED = WorkflowStepInstanceStatuses.SKIPPED
Statuses = ReviewerPoolInvitationStatuses


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextmanager
def logged_by(migration):
    """The messages ``migration`` logs while the block runs."""
    logger = logging.getLogger(f"waldur_mastermind.proposal.migrations.{migration}")
    handler, level = _Collect(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


@override_settings(MIGRATION_MODULES={})
class LegacyCallStepMigrationTest(TestCase):
    """The backfill: shared step forms and calls without step rows."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.call = factories.CallFactory()
        cls.round = factories.RoundFactory(call=cls.call)
        cls.form = checklist_factories.ChecklistFactory()
        cls.question = checklist_factories.QuestionFactory(
            checklist=cls.form, required=True
        )
        cls.assessor = structure_factories.UserFactory()
        # Its administrative check is over; the technical assessment, which
        # uses the same form, is under way.
        cls.proposal = factories.ProposalFactory(
            round=cls.round, state=ProposalStates.IN_REVIEW
        )
        # Older than the step rows: develop's 0056 seeded the decision for
        # draft calls only, and nothing seeded the others.
        cls.legacy = {
            state: factories.CallFactory(state=state)
            for state in (CallStates.ACTIVE, CallStates.DRAFT, CallStates.ARCHIVED)
        }
        factories.RoundFactory(call=cls.legacy[CallStates.ACTIVE])
        # A score threshold on the decision, with expert review disabled in
        # one call and enabled in the other.
        cls.unscored = factories.CallFactory(state=CallStates.ACTIVE)
        cls.scored = factories.CallFactory(state=CallStates.ACTIVE)
        models.CallWorkflowStep.objects.filter(
            call__in=[cls.call, cls.unscored, cls.scored, *cls.legacy.values()]
        ).delete()

        apps = migration_helpers.migrate_back(
            [("proposal", "0092_delete_empty_proposal_documents")]
        )
        cls._fill(apps)
        cls._fill_score_gates(apps)
        with logged_by("0094_call_workflow_backfill") as cls.log:
            migration_helpers.migrate_forward()

    @classmethod
    def _fill_score_gates(cls, apps):
        Step = apps.get_model("proposal", "CallWorkflowStep")
        for call, reviewed in ((cls.unscored, False), (cls.scored, True)):
            Step.objects.create(
                call_id=call.pk, step="administrative_check", is_enabled=True
            )
            Step.objects.create(
                call_id=call.pk, step="expert_review", is_enabled=reviewed
            )
            Step.objects.create(
                call_id=call.pk,
                step="allocation_decision",
                is_enabled=True,
                min_score_threshold=3,
            )

    @classmethod
    def _fill(cls, apps):
        Step = apps.get_model("proposal", "CallWorkflowStep")
        Instance = apps.get_model("proposal", "ProposalWorkflowStepInstance")
        Proposal = apps.get_model("proposal", "Proposal")
        Completion = apps.get_model("checklist", "ChecklistCompletion")
        Answer = apps.get_model("checklist", "Answer")

        shares_form = ("administrative_check", "technical_assessment")
        for key in CATALOGUE:
            Step.objects.create(
                call_id=cls.call.pk,
                step=key,
                is_enabled=key in (*shares_form, "allocation_decision"),
                checklist_id=cls.form.pk if key in shares_form else None,
            )
        statuses = {
            "administrative_check": COMPLETED,
            "technical_assessment": ACTIVE,
            "allocation_decision": PENDING,
        }
        now = timezone.now()
        for key in CATALOGUE:
            status = statuses.get(key, SKIPPED)
            Instance.objects.create(
                proposal_id=cls.proposal.pk,
                step=key,
                status=status,
                started_at=now if status != PENDING else None,
            )
        Proposal.objects.filter(pk=cls.proposal.pk).update(
            workflow_step="technical_assessment"
        )
        completion = Completion.objects.create(
            scope_content_type_id=ContentType.objects.get_for_model(models.Proposal).pk,
            scope_object_id=cls.proposal.pk,
            checklist_id=cls.form.pk,
        )
        Answer.objects.create(
            completion_id=completion.pk,
            question_id=cls.question.pk,
            user_id=cls.assessor.pk,
            answer_data="eligible and feasible",
        )

    def _instance(self, key):
        return self.proposal.workflow_step_instances.get(call_step__key=key)

    def test_every_step_sharing_the_form_has_the_answers(self):
        for key, status in (
            ("administrative_check", StepContributionStatuses.SUBMITTED),
            ("technical_assessment", StepContributionStatuses.PENDING),
        ):
            contribution = self._instance(key).contributions.get(
                contributor=self.assessor
            )
            self.assertEqual(contribution.status, status, key)
            answer = contribution.get_checklist_completion(self.form).answers.get()
            self.assertEqual(answer.answer_data, "eligible and feasible", key)

    def test_the_step_in_progress_can_still_be_completed(self):
        proposal = models.Proposal.objects.get(pk=self.proposal.pk)
        instance = self._instance("technical_assessment")
        self.assertFalse(contributions.unanswered_required_questions(instance).exists())
        workflow_service.complete_step(
            proposal, instance, WorkflowStepOutcomes.FEASIBLE, "", None
        )
        proposal.refresh_from_db()
        self.assertEqual(proposal.current_step.call_step.key, "allocation_decision")

    def test_open_calls_without_steps_get_the_call_manager_steps(self):
        for state in (CallStates.ACTIVE, CallStates.DRAFT):
            enabled = set(
                self.legacy[state]
                .workflow_steps.filter(is_enabled=True)
                .values_list("key", flat=True)
            )
            self.assertEqual(
                enabled, {"administrative_check", "allocation_decision"}, state
            )

    def test_an_archived_call_keeps_its_history(self):
        self.assertFalse(
            self.legacy[CallStates.ARCHIVED]
            .workflow_steps.filter(is_enabled=True)
            .exists()
        )

    def test_a_score_threshold_nothing_scores_is_logged_and_kept(self):
        def logged(call):
            return any(
                message.startswith(f"Call {call.pk}: the allocation_decision step")
                for message in self.log
            )

        self.assertTrue(logged(self.unscored))
        self.assertFalse(logged(self.scored))
        self.assertEqual(
            self.unscored.workflow_steps.get(
                key="allocation_decision"
            ).min_score_threshold,
            3,
        )
        self.assertFalse(
            self.unscored.workflow_steps.get(key="expert_review").is_enabled
        )

    def test_a_proposal_submitted_to_an_old_call_starts_its_evaluation(self):
        call = self.legacy[CallStates.ACTIVE]
        proposal = factories.ProposalFactory(round=call.round_set.get())
        workflow_service.create_step_instances(proposal)
        self.assertEqual(
            workflow_service.activate_first_step(proposal).call_step.key,
            "administrative_check",
        )


@override_settings(MIGRATION_MODULES={})
class ReviewerSyncMigrationTest(TestCase):
    """The backfill: a call's reviewer role holders join its pool."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.call = factories.CallFactory()
        cls.role = CallRole.REVIEWER
        cls.no_profile = structure_factories.UserFactory()
        cls.profiles = {
            name: factories.ReviewerProfileFactory()
            for name in ("by_address", "declined", "pending", "accepted")
        }
        cls._fill(
            migration_helpers.migrate_back([("proposal", "0093_call_workflow_expand")])
        )
        migration_helpers.migrate_forward()

    @classmethod
    def _fill(cls, apps):
        Pool = apps.get_model("proposal", "CallReviewerPool")
        UserRole = apps.get_model("permissions", "UserRole")
        call_type = ContentType.objects.get_for_model(models.Call)

        def grant(user):
            UserRole.objects.create(
                content_type_id=call_type.pk,
                object_id=cls.call.pk,
                user_id=user.pk,
                role_id=cls.role.pk,
                is_active=True,
            )

        def entry(status, **fields):
            return Pool.objects.create(
                call_id=cls.call.pk, invitation_status=status, **fields
            ).pk

        # Invited by e-mail before they had an account, then given the role.
        grant(cls.no_profile)
        cls.no_profile_entry = entry(
            Statuses.PENDING, invited_email=cls.no_profile.email
        )
        # The same, with a profile and the address in other case.
        by_address = cls.profiles["by_address"].user
        grant(by_address)
        cls.by_address_entry = entry(
            Statuses.PENDING, invited_email=by_address.email.upper()
        )
        # Said no, yet holds the role.
        grant(cls.profiles["declined"].user)
        cls.declined_entry = entry(
            Statuses.DECLINED, reviewer_id=cls.profiles["declined"].pk
        )
        # Never answered, and holds the role.
        grant(cls.profiles["pending"].user)
        cls.pending_entry = entry(
            Statuses.PENDING, reviewer_id=cls.profiles["pending"].pk
        )
        # Accepted, never given the role.
        cls.accepted_entry = entry(
            Statuses.ACCEPTED, reviewer_id=cls.profiles["accepted"].pk
        )

    def _entry(self, pk):
        return models.CallReviewerPool.objects.get(pk=pk)

    def test_an_invitation_to_the_address_is_taken_up_not_duplicated(self):
        entry = self._entry(self.no_profile_entry)
        self.assertEqual(entry.invitation_status, Statuses.ACCEPTED)
        self.assertEqual(entry.invited_user, self.no_profile)
        self.assertEqual(
            models.CallReviewerPool.objects.filter(
                call=self.call, invited_email__iexact=self.no_profile.email
            ).count(),
            1,
        )

    def test_an_invitation_to_a_profiles_address_is_bound_to_the_profile(self):
        entry = self._entry(self.by_address_entry)
        self.assertEqual(entry.invitation_status, Statuses.ACCEPTED)
        self.assertEqual(entry.reviewer, self.profiles["by_address"])
        self.assertEqual(
            models.CallReviewerPool.objects.filter(
                call=self.call, reviewer=self.profiles["by_address"]
            ).count(),
            1,
        )

    def test_a_decline_is_kept_and_so_is_the_role(self):
        self.assertEqual(
            self._entry(self.declined_entry).invitation_status, Statuses.DECLINED
        )
        self.assertTrue(
            self.call.has_user(self.profiles["declined"].user, CallRole.REVIEWER)
        )

    def test_an_unanswered_invitation_is_accepted_for_a_role_holder(self):
        self.assertEqual(
            self._entry(self.pending_entry).invitation_status, Statuses.ACCEPTED
        )

    def test_an_accepted_member_gets_the_role(self):
        self.assertTrue(
            self.call.has_user(self.profiles["accepted"].user, CallRole.REVIEWER)
        )


@override_settings(MIGRATION_MODULES={})
class ReviewerStatsDropMigrationTest(TestCase):
    """Imported reviewer stats are dropped, and the backfill logs them."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.profile = factories.ReviewerProfileFactory()
        apps = migration_helpers.migrate_back(
            [("proposal", "0093_call_workflow_expand")]
        )
        apps.get_model("proposal", "ReviewerStats").objects.create(
            reviewer_profile_id=cls.profile.pk, total_reviews_completed=12
        )
        with logged_by("0094_call_workflow_backfill") as cls.log:
            migration_helpers.migrate_forward()

    def test_the_dropped_rows_are_logged(self):
        self.assertIn(
            "Dropping 1 imported reviewer stats rows (reviewer profiles "
            f"{self.profile.pk}). Reviewer figures are now worked out from "
            "this database's reviews; the imported ones are not kept.",
            self.log,
        )
