"""Tests for the 0064 stranded-workflow backfill.

The migration function runs against the schema it was written for: the test
database is migrated back to before steps had a kind, the function runs with
the historical models, and the database is migrated forward again, so the
proposals are read back through the current models.
"""

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

ACTIVE = WorkflowStepInstanceStatuses.ACTIVE
PENDING = WorkflowStepInstanceStatuses.PENDING

# Any state before steps had a kind has the columns the function uses.
HISTORICAL = [("proposal", "0089_awardedresource")]


@override_settings(MIGRATION_MODULES={})
class BackfillStrandedWorkflowTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        # new_call is auto-seeded with default workflow steps on creation.
        self.call = self.fixture.new_call
        self.round = factories.RoundFactory(call=self.call)

    def _make_stranded(self, state=ProposalStates.SUBMITTED):
        proposal = factories.ProposalFactory(round=self.round, state=state)
        proposal.current_step = None
        proposal.save(update_fields=["current_step"])
        proposal.workflow_step_instances.all().delete()
        return proposal

    def _backfill(self, times=1):
        apps = migration_helpers.migrate_back(HISTORICAL)
        for _ in range(times):
            backfill_stranded_workflow_instances(apps, None)
        migration_helpers.migrate_forward()

    def test_backfills_submitted_proposal_into_in_review(self):
        proposal = self._make_stranded()

        self._backfill()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertIsNotNone(proposal.workflow_step)
        self.assertEqual(proposal.workflow_step_instances.count(), len(CATALOGUE_STEPS))
        self.assertEqual(
            proposal.workflow_step_instances.filter(status=ACTIVE).count(), 1
        )

    def test_seeds_default_steps_when_call_has_none(self):
        # A pre-engine call with no step config must still be repaired.
        models.CallWorkflowStep.objects.filter(call=self.call).delete()
        proposal = self._make_stranded()

        self._backfill()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertTrue(
            models.CallWorkflowStep.objects.filter(
                call=self.call, is_enabled=True
            ).exists()
        )

    def test_force_enables_when_all_steps_disabled(self):
        # get_or_create alone would leave disabled rows disabled; the backfill
        # must force-enable the call-manager defaults so the proposal advances.
        models.CallWorkflowStep.objects.filter(call=self.call).update(is_enabled=False)
        proposal = self._make_stranded()

        self._backfill()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)

    def test_is_idempotent(self):
        proposal = self._make_stranded()

        self._backfill(times=2)

        proposal.refresh_from_db()
        self.assertEqual(proposal.workflow_step_instances.count(), len(CATALOGUE_STEPS))
        self.assertEqual(
            proposal.workflow_step_instances.filter(status=ACTIVE).count(), 1
        )

    def test_skips_proposal_that_already_has_instances(self):
        proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        factories.ProposalWorkflowStepInstanceFactory(
            proposal=proposal, step="administrative_check", status=PENDING
        )

        self._backfill()

        proposal.refresh_from_db()
        # Untouched: still exactly the one instance we added.
        self.assertEqual(proposal.workflow_step_instances.count(), 1)

    def test_skips_terminal_proposal(self):
        proposal = self._make_stranded(state=ProposalStates.ACCEPTED)

        self._backfill()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(proposal.workflow_step_instances.count(), 0)
