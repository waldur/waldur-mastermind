"""Tests for the 0064 stranded-workflow backfill.

The migration function is exercised directly against the live app registry
(django_test_migrations is not available), mirroring
marketplace/tests/test_enable_posix_account_backfill.py.
"""

from django.apps import apps as live_apps
from rest_framework import test

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    WORKFLOW_STEPS,
    ProposalStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.migrations._backfill_stranded_workflow_instances import (
    backfill_stranded_workflow_instances,
)
from waldur_mastermind.proposal.tests import factories, fixtures

ACTIVE = WorkflowStepInstanceStatuses.ACTIVE
PENDING = WorkflowStepInstanceStatuses.PENDING


class BackfillStrandedWorkflowTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        # new_call is auto-seeded with default workflow steps on creation.
        self.call = self.fixture.new_call
        self.round = factories.RoundFactory(call=self.call)

    def _make_stranded(self, state=ProposalStates.SUBMITTED):
        proposal = factories.ProposalFactory(round=self.round, state=state)
        proposal.workflow_step_instances.all().delete()
        proposal.workflow_step = None
        proposal.save(update_fields=["workflow_step"])
        return proposal

    def test_backfills_submitted_proposal_into_in_review(self):
        proposal = self._make_stranded()

        backfill_stranded_workflow_instances(live_apps, None)

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertIsNotNone(proposal.workflow_step)
        self.assertEqual(proposal.workflow_step_instances.count(), len(WORKFLOW_STEPS))
        self.assertEqual(
            proposal.workflow_step_instances.filter(status=ACTIVE).count(), 1
        )

    def test_seeds_default_steps_when_call_has_none(self):
        # A pre-engine call with no step config must still be repaired.
        models.CallWorkflowStep.objects.filter(call=self.call).delete()
        proposal = self._make_stranded()

        backfill_stranded_workflow_instances(live_apps, None)

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

        backfill_stranded_workflow_instances(live_apps, None)

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)

    def test_is_idempotent(self):
        proposal = self._make_stranded()

        backfill_stranded_workflow_instances(live_apps, None)
        backfill_stranded_workflow_instances(live_apps, None)

        proposal.refresh_from_db()
        self.assertEqual(proposal.workflow_step_instances.count(), len(WORKFLOW_STEPS))
        self.assertEqual(
            proposal.workflow_step_instances.filter(status=ACTIVE).count(), 1
        )

    def test_skips_proposal_that_already_has_instances(self):
        proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        models.ProposalWorkflowStepInstance.objects.create(
            proposal=proposal, step="administrative_check", status=PENDING
        )

        backfill_stranded_workflow_instances(live_apps, None)

        proposal.refresh_from_db()
        # Untouched: still exactly the one instance we added.
        self.assertEqual(proposal.workflow_step_instances.count(), 1)

    def test_skips_terminal_proposal(self):
        proposal = self._make_stranded(state=ProposalStates.ACCEPTED)

        backfill_stranded_workflow_instances(live_apps, None)

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(proposal.workflow_step_instances.count(), 0)
