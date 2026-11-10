"""Tests for the 0064 stranded-workflow backfill.

The migration function runs against the schema it was written for: the test
database is migrated back to before steps had a kind, the function runs with
the historical models, and the database is migrated forward again, so the
proposals are read back through the current models. Every case is set up
first, on a call of its own where the call's steps matter, and the database
is migrated once for all of them.
"""

import pytest
from django.test import override_settings
from rest_framework import test

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    ProposalStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.migrations._backfill_stranded_workflow_instances import (
    CATALOGUE_STEPS,
    backfill_stranded_workflow_instances,
)
from waldur_mastermind.proposal.tests import factories, fixtures, migration_helpers

pytestmark = pytest.mark.nightly

ACTIVE = WorkflowStepInstanceStatuses.ACTIVE
PENDING = WorkflowStepInstanceStatuses.PENDING

# Any state before steps had a kind has the columns the function uses.
HISTORICAL = [("proposal", "0089_awardedresource")]


@override_settings(MIGRATION_MODULES={})
class BackfillStrandedWorkflowTest(migration_helpers.OneMigrationRun, test.APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # new_call is auto-seeded with default workflow steps on creation.
        cls.round = cls._round(fixtures.ProposalFixture().new_call)
        cls.submitted = cls._make_stranded(cls.round)
        cls.terminal = cls._make_stranded(cls.round, state=ProposalStates.ACCEPTED)
        cls.with_instances = factories.ProposalFactory(
            round=cls.round, state=ProposalStates.SUBMITTED
        )
        factories.ProposalWorkflowStepInstanceFactory(
            proposal=cls.with_instances, step="administrative_check", status=PENDING
        )

        # A pre-engine call with no step config must still be repaired.
        cls.stepless_call = fixtures.ProposalFixture().new_call
        models.CallWorkflowStep.objects.filter(call=cls.stepless_call).delete()
        cls.on_stepless_call = cls._make_stranded(cls._round(cls.stepless_call))

        # get_or_create alone would leave disabled rows disabled; the backfill
        # must force-enable the call-manager defaults so the proposal advances.
        disabled_call = fixtures.ProposalFixture().new_call
        models.CallWorkflowStep.objects.filter(call=disabled_call).update(
            is_enabled=False
        )
        cls.on_disabled_call = cls._make_stranded(cls._round(disabled_call))

        apps = migration_helpers.migrate_back(HISTORICAL)
        backfill_stranded_workflow_instances(apps, None)
        cls.after_one_run = cls._snapshot(apps)
        backfill_stranded_workflow_instances(apps, None)
        cls.after_two_runs = cls._snapshot(apps)
        migration_helpers.migrate_forward()

    @staticmethod
    def _round(call):
        return factories.RoundFactory(call=call)

    @staticmethod
    def _make_stranded(round_, state=ProposalStates.SUBMITTED):
        proposal = factories.ProposalFactory(round=round_, state=state)
        proposal.current_step = None
        proposal.save(update_fields=["current_step"])
        proposal.workflow_step_instances.all().delete()
        return proposal

    @staticmethod
    def _snapshot(apps):
        Proposal = apps.get_model("proposal", "Proposal")
        Instance = apps.get_model("proposal", "ProposalWorkflowStepInstance")
        return (
            sorted(Proposal.objects.values_list("pk", "state", "workflow_step")),
            sorted(Instance.objects.values_list("proposal_id", "step", "status")),
        )

    def _read(self, proposal):
        return models.Proposal.objects.get(pk=proposal.pk)

    def check_backfills_submitted_proposal_into_in_review(self):
        proposal = self._read(self.submitted)
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertIsNotNone(proposal.workflow_step)
        self.assertEqual(proposal.workflow_step_instances.count(), len(CATALOGUE_STEPS))
        self.assertEqual(
            proposal.workflow_step_instances.filter(status=ACTIVE).count(), 1
        )

    def check_seeds_default_steps_when_call_has_none(self):
        self.assertEqual(
            self._read(self.on_stepless_call).state, ProposalStates.IN_REVIEW
        )
        self.assertTrue(
            models.CallWorkflowStep.objects.filter(
                call=self.stepless_call, is_enabled=True
            ).exists()
        )

    def check_force_enables_when_all_steps_disabled(self):
        self.assertEqual(
            self._read(self.on_disabled_call).state, ProposalStates.IN_REVIEW
        )

    def check_is_idempotent(self):
        self.assertEqual(self.after_two_runs, self.after_one_run)

    def check_skips_proposal_that_already_has_instances(self):
        # Untouched: still exactly the one instance we added.
        self.assertEqual(
            self._read(self.with_instances).workflow_step_instances.count(), 1
        )

    def check_skips_terminal_proposal(self):
        proposal = self._read(self.terminal)
        self.assertEqual(proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(proposal.workflow_step_instances.count(), 0)
