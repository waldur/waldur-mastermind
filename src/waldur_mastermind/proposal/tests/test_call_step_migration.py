"""Migrating calls from the fixed step catalogue to call steps of a kind.

The test database is migrated back to before the change, filled the way the
catalogue engine left it -- proposals in flight at every step, skipped steps,
a step parked on a manual advance, and a call whose display order runs against
the catalogue -- and migrated forward again. Every proposal must stand on the
same step with the same path ahead of it, and the engine must carry it on as
it did.
"""

import logging

import pytest
from django.test import TestCase, override_settings
from django.utils import timezone

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, workflow_service
from waldur_mastermind.proposal.enums import (
    OnNegativeOutcomes,
    OutcomeSets,
    ProposalStates,
    ResponsibleRoles,
    StepKinds,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, migration_helpers

pytestmark = pytest.mark.nightly

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
SKIPPED = WorkflowStepInstanceStatuses.SKIPPED


@override_settings(MIGRATION_MODULES={})
class CallStepMigrationTest(migration_helpers.OneMigrationRun, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = structure_factories.UserFactory()
        cls.call = factories.CallFactory()
        cls.sparse_call = factories.CallFactory()
        call_round = factories.RoundFactory(call=cls.call)
        sparse_round = factories.RoundFactory(call=cls.sparse_call)
        # The catalogue engine predates them; it is rebuilt below.
        models.CallWorkflowStep.objects.filter(
            call__in=[cls.call, cls.sparse_call]
        ).delete()

        # name -> (round, state, {key: status}, current step key)
        cls.scenarios = {}
        for index, key in enumerate(CATALOGUE):
            statuses = {
                other: COMPLETED if i < index else ACTIVE if i == index else PENDING
                for i, other in enumerate(CATALOGUE)
            }
            cls.scenarios[f"at_{key}"] = (
                call_round,
                ProposalStates.IN_REVIEW,
                statuses,
                key,
            )
        # Submitted while the technical assessment was disabled.
        cls.scenarios["skipped_technical"] = (
            call_round,
            ProposalStates.IN_REVIEW,
            {
                "administrative_check": COMPLETED,
                "technical_assessment": SKIPPED,
                "expert_review": ACTIVE,
                "panel_review": PENDING,
                "allocation_decision": PENDING,
                "award_response": SKIPPED,
            },
            "expert_review",
        )
        # Completed, waiting to be moved on by hand.
        cls.scenarios["parked"] = (
            call_round,
            ProposalStates.IN_REVIEW,
            {
                "administrative_check": COMPLETED,
                "technical_assessment": PENDING,
                "expert_review": PENDING,
                "panel_review": PENDING,
                "allocation_decision": PENDING,
                "award_response": PENDING,
            },
            "administrative_check",
        )
        cls.scenarios["accepted"] = (
            call_round,
            ProposalStates.ACCEPTED,
            dict.fromkeys(CATALOGUE, COMPLETED),
            None,
        )
        # Its call never had rows for most of the catalogue: submit skipped them.
        cls.scenarios["sparse"] = (
            sparse_round,
            ProposalStates.IN_REVIEW,
            {
                "administrative_check": COMPLETED,
                "technical_assessment": SKIPPED,
                "expert_review": SKIPPED,
                "panel_review": SKIPPED,
                "allocation_decision": ACTIVE,
                "award_response": SKIPPED,
            },
            "allocation_decision",
        )
        cls.proposals = {
            name: factories.ProposalFactory(round=round_, state=state)
            for name, (round_, state, _, _) in cls.scenarios.items()
        }
        # Data the API never let in, which the migration must not trip on.
        cls.unknown_key_proposal = factories.ProposalFactory(
            round=sparse_round, state=ProposalStates.IN_REVIEW
        )
        cls.lost_step_proposal = factories.ProposalFactory(
            round=sparse_round, state=ProposalStates.IN_REVIEW
        )

        cls._fill_catalogue_world(migration_helpers.migrate_back(BEFORE))
        handler = _ListHandler()
        migration_logger = logging.getLogger(
            "waldur_mastermind.proposal.migrations.0094_call_workflow_backfill"
        )
        migration_logger.addHandler(handler)
        try:
            migration_helpers.migrate_forward()
        finally:
            migration_logger.removeHandler(handler)
        cls.migration_log = [record.getMessage() for record in handler.records]

    @classmethod
    def _fill_catalogue_world(cls, apps):
        Step = apps.get_model("proposal", "CallWorkflowStep")
        Instance = apps.get_model("proposal", "ProposalWorkflowStepInstance")
        Proposal = apps.get_model("proposal", "Proposal")

        # The display order runs against the catalogue; the engine ignored it.
        for display_order, key in enumerate(reversed(CATALOGUE)):
            Step.objects.create(
                call_id=cls.call.pk,
                step=key,
                is_enabled=True,
                display_order=display_order,
                include_award_response=key == "allocation_decision",
                transition_mode=(
                    "manual"
                    if key == "administrative_check"
                    else "automatic_on_completion"
                ),
                # A call manager handed the check to the offering managers.
                responsible_role=(
                    ResponsibleRoles.OFFERING_MANAGER
                    if key == "administrative_check"
                    else None
                ),
            )
        Step.objects.create(
            call_id=cls.sparse_call.pk, step="administrative_check", is_enabled=True
        )
        Step.objects.create(
            call_id=cls.sparse_call.pk,
            step="allocation_decision",
            is_enabled=True,
            responsible_role=ResponsibleRoles.CALL_MANAGER,
        )

        now = timezone.now()
        for name, (_, _, statuses, current) in cls.scenarios.items():
            proposal = cls.proposals[name]
            for key in CATALOGUE:
                Instance.objects.create(
                    proposal_id=proposal.pk,
                    step=key,
                    status=statuses[key],
                    started_at=now if statuses[key] in (ACTIVE, COMPLETED) else None,
                    outcome="eligible"
                    if statuses[key] == COMPLETED and key == "administrative_check"
                    else None,
                )
            Proposal.objects.filter(pk=proposal.pk).update(workflow_step=current)

        Instance.objects.create(
            proposal_id=cls.unknown_key_proposal.pk,
            step="legacy_step",
            status=ACTIVE,
            started_at=now,
        )
        Proposal.objects.filter(pk=cls.unknown_key_proposal.pk).update(
            workflow_step="legacy_step"
        )
        Proposal.objects.filter(pk=cls.lost_step_proposal.pk).update(
            workflow_step="panel_review"
        )

    def _path(self, proposal):
        return [
            (instance.step, instance.status)
            for instance in proposal.workflow_step_instances.order_by("order")
        ]

    def check_every_proposal_keeps_its_step_and_path(self):
        for name, (_, _, statuses, current) in self.scenarios.items():
            proposal = models.Proposal.objects.get(pk=self.proposals[name].pk)
            with self.subTest(proposal=name):
                self.assertEqual(proposal.workflow_step, current)
                self.assertEqual(
                    self._path(proposal),
                    [(key, statuses[key]) for key in CATALOGUE],
                )

    def check_every_instance_points_at_its_calls_step(self):
        instances = models.ProposalWorkflowStepInstance.objects.filter(
            proposal__in=self.proposals.values()
        ).select_related("call_step", "proposal__round")
        self.assertEqual(instances.count(), len(self.proposals) * len(CATALOGUE))
        for instance in instances:
            self.assertEqual(
                instance.call_step.call_id, instance.proposal.round.call_id
            )
            self.assertEqual(instance.call_step.key, instance.step)
            # The proposal's own copy of its path.
            self.assertEqual(instance.order, CATALOGUE.index(instance.step))
            self.assertEqual(
                instance.is_allocation_decision,
                instance.step == "allocation_decision",
            )

    def check_order_is_the_catalogue_index_not_the_display_order(self):
        for call in (self.call, self.sparse_call):
            with self.subTest(call=call.name):
                self.assertEqual(
                    list(
                        call.workflow_steps.filter(key__in=CATALOGUE)
                        .order_by("order")
                        .values_list("key", "order")
                    ),
                    [(key, index) for index, key in enumerate(CATALOGUE)],
                )
        # The display order itself is kept, deprecated.
        self.assertEqual(
            self.call.workflow_steps.get(key="award_response").display_order, 0
        )

    def check_missing_catalogue_steps_are_added_disabled(self):
        steps = {
            step.key: step
            for step in self.sparse_call.workflow_steps.filter(key__in=CATALOGUE)
        }
        self.assertEqual(set(steps), set(CATALOGUE))
        self.assertEqual(
            {key for key, step in steps.items() if step.is_enabled},
            {"administrative_check", "allocation_decision"},
        )

    def check_steps_take_the_kind_of_the_catalogue_step(self):
        expected = {
            "administrative_check": (StepKinds.DECISION, OutcomeSets.ELIGIBILITY),
            "technical_assessment": (StepKinds.ASSESSMENT, OutcomeSets.FEASIBILITY),
            "expert_review": (StepKinds.ASSESSMENT, OutcomeSets.REVIEWED),
            "panel_review": (StepKinds.DECISION, OutcomeSets.APPROVAL),
            "allocation_decision": (StepKinds.DECISION, OutcomeSets.APPROVAL),
            "award_response": (StepKinds.APPLICANT_ACTION, OutcomeSets.ACCEPTANCE),
        }
        for step in self.call.workflow_steps.all():
            with self.subTest(step=step.key):
                self.assertEqual((step.kind, step.outcome_set), expected[step.key])
                self.assertEqual(step.name, step.key.replace("_", " ").capitalize())
                self.assertEqual(
                    step.is_allocation_decision, step.key == "allocation_decision"
                )
                self.assertEqual(
                    step.on_negative,
                    OnNegativeOutcomes.CANCEL
                    if step.key == "award_response"
                    else OnNegativeOutcomes.REJECT,
                )

    def check_completer_keeps_the_configured_role_or_takes_the_default(self):
        completers = dict(self.call.workflow_steps.values_list("key", "completer"))
        self.assertEqual(
            completers,
            {
                # Configured on the call.
                "administrative_check": ResponsibleRoles.OFFERING_MANAGER,
                # Left empty: the catalogue default.
                "technical_assessment": ResponsibleRoles.OFFERING_MANAGER,
                "expert_review": ResponsibleRoles.REVIEWER,
                "panel_review": ResponsibleRoles.PANEL_MEMBER,
                "allocation_decision": ResponsibleRoles.CALL_MANAGER,
                "award_response": ResponsibleRoles.APPLICANT,
            },
        )

    def check_engine_continues_on_the_old_path(self):
        proposal = models.Proposal.objects.get(
            pk=self.proposals["skipped_technical"].pk
        )
        next_instance = workflow_service.complete_step(
            proposal, proposal.current_step, "reviewed", "", self.user
        )
        self.assertEqual(next_instance.step, "panel_review")
        proposal.refresh_from_db()
        self.assertEqual(proposal.workflow_step, "panel_review")

    def check_parked_proposal_still_awaits_its_manual_advance(self):
        proposal = models.Proposal.objects.get(pk=self.proposals["parked"].pk)
        self.assertTrue(workflow_service.is_awaiting_manual_advance(proposal))
        next_instance = workflow_service.advance_step(proposal, self.user)
        self.assertEqual(next_instance.step, "technical_assessment")

    def check_declining_a_migrated_award_still_cancels(self):
        proposal = models.Proposal.objects.get(
            pk=self.proposals["at_award_response"].pk
        )
        workflow_service.complete_step(
            proposal, proposal.current_step, "declined", "", proposal.created_by
        )
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.CANCELED)

    def check_allocation_timing_is_read_from_the_migrated_decision(self):
        decision = models.CallWorkflowStep.allocation_decision_for(self.call)
        self.assertEqual(decision.key, "allocation_decision")
        self.assertTrue(decision.include_award_response)

    def check_a_key_the_call_had_no_row_for_gets_a_disabled_one(self):
        proposal = models.Proposal.objects.get(pk=self.unknown_key_proposal.pk)
        self.assertEqual(proposal.workflow_step, "legacy_step")
        step = proposal.current_step.call_step
        self.assertEqual(
            (step.key, step.call_id, step.is_enabled, step.order),
            ("legacy_step", self.sparse_call.pk, False, len(CATALOGUE)),
        )
        self.assertTrue(any("legacy_step" in message for message in self.migration_log))

    def check_a_step_without_an_instance_is_logged(self):
        proposal = models.Proposal.objects.get(pk=self.lost_step_proposal.pk)
        self.assertIsNone(proposal.current_step)
        self.assertTrue(
            any(
                str(proposal.pk) in message and "panel_review" in message
                for message in self.migration_log
            )
        )


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)
