"""A step action is about the step it names, and is checked again under the lock.

The permission check of complete/reject runs before the proposal's lock. A
request naming a step that is not active yet is refused outright, and the
one that is active when the lock is taken is checked again: another request
may have moved the proposal on, or changed who the user is to it, meanwhile.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import views, workflow_service
from waldur_mastermind.proposal.enums import (
    ProposalStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_applicant_team import TeamTestMixin


class StepActionStandingTest(TeamTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_statement()
        self.statement_instance = self.proposal.current_step
        self.next_instance = (
            self.proposal.workflow_step_instances.filter(
                status=WorkflowStepInstanceStatuses.PENDING
            )
            .order_by("call_step__order", "id")
            .first()
        )

    def post(self, action, user, step_uuid, **payload):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action),
            {"step_uuid": step_uuid, **payload},
        )

    def race(self, side_effect):
        """Run ``side_effect`` once, after the request's permission check and
        before its lock, as a concurrent request committing would."""
        named_step = views.ProposalViewSet._named_step

        def racing(proposal, step_uuid):
            if not racing.done:
                racing.done = True
                side_effect()
            return named_step(proposal, step_uuid)

        racing.done = False
        return mock.patch.object(
            views.ProposalViewSet, "_named_step", staticmethod(racing)
        )

    def complete_statement(self):
        workflow_service.complete_step(
            proposal=self.proposal,
            current_instance=self.proposal.workflow_step_instances.get(
                pk=self.statement_instance.pk
            ),
            outcome="submitted",
            outcome_reason="",
            completed_by=self.author,
        )

    def test_applicant_does_not_complete_the_step_after_their_own(self):
        with self.race(self.complete_statement):
            response = self.post(
                "complete_workflow_step",
                self.author,
                self.next_instance.uuid.hex,
                outcome="approved",
            )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.next_instance.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertIsNone(self.next_instance.completed_by)
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    def test_applicant_does_not_reject_at_the_step_after_their_own(self):
        with self.race(self.complete_statement):
            response = self.post(
                "reject_workflow_step",
                self.author,
                self.next_instance.uuid.hex,
                reason="No",
            )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    def test_a_step_not_yet_reached_is_refused_to_whoever_cannot_act_on_it(self):
        response = self.post(
            "complete_workflow_step",
            self.author,
            self.next_instance.uuid.hex,
            outcome="approved",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_standing_lost_before_the_lock_is_refused(self):
        self.complete_statement()
        self.proposal.refresh_from_db()
        call_manager = structure_factories.UserFactory()
        self.call.add_user(call_manager, CallRole.MANAGER)
        with self.race(
            lambda: self.proposal.add_user(call_manager, ProposalRole.MEMBER)
        ):
            response = self.post(
                "complete_workflow_step",
                call_manager,
                self.next_instance.uuid.hex,
                outcome="approved",
            )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.next_instance.refresh_from_db()
        self.assertEqual(self.next_instance.status, WorkflowStepInstanceStatuses.ACTIVE)

    def test_the_completer_of_a_step_that_moved_on_is_told_to_refresh(self):
        self.complete_statement()
        self.proposal.refresh_from_db()
        response = self.post(
            "complete_workflow_step",
            self.author,
            self.statement_instance.uuid.hex,
            # Read against the step the proposal stands in now.
            outcome="approved",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
