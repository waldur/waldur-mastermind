"""An applicant withdrawing a submitted proposal from evaluation."""

from unittest import mock

from django.core import mail
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Notification
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks
from waldur_mastermind.proposal.enums import (
    AssignmentItemStatuses,
    ProposalStates,
    StepContributionStatuses,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures


class ProposalWithdrawTest(test.APITestCase):
    def setUp(self):
        Notification.objects.update_or_create(
            key="proposal.proposal_withdrawn", defaults={"enabled": True}
        )
        self.fixture = fixtures.ProposalFixture()
        self.manager = self.fixture.call_manager
        self.proposal = self.fixture.proposal_submitted
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        self.applicant = self.proposal.created_by
        # The author acts on the proposal through its manager role.
        fixtures.author_as_manager(self.proposal)
        self.active = models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
        )
        self.pending = models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        self.review = self.fixture.review
        self.slot = models.StepContribution.objects.create(
            proposal_step=self.active,
            requested_offering=self.fixture.requested_offering_accepted,
        )
        self.assignment = factories.AssignmentItemFactory(
            proposal=self.proposal, status=AssignmentItemStatuses.PENDING
        )
        mail.outbox = []

    def withdraw(self, user, reason="Funded elsewhere."):
        self.client.force_authenticate(user)
        # The call team is told once the withdrawal has committed.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                factories.ProposalFactory.get_url(self.proposal, "withdraw"),
                {"reason": reason},
            )

    def test_applicant_withdraws_and_evaluation_stops(self):
        response = self.withdraw(self.applicant)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.CANCELED)
        for instance in (self.active, self.pending):
            instance.refresh_from_db()
            self.assertEqual(instance.status, WorkflowStepInstanceStatuses.SKIPPED)
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.REJECTED)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentItemStatuses.EXPIRED)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.status, StepContributionStatuses.EXPIRED)
        self.assertIsNone(self.proposal.current_step)

    def test_call_team_is_told(self):
        task = tasks.notify_call_managers_about_withdrawn_proposal
        # Run the queued task in-process: the mail is what is under test.
        with mock.patch.object(task, "delay", side_effect=task):
            self.withdraw(self.applicant)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.manager.email])
        self.assertIn(self.proposal.name, mail.outbox[0].subject)
        self.assertIn("Funded elsewhere.", mail.outbox[0].body)

    def test_withdrawal_is_logged_in_the_deployment_vocabulary(self):
        with override_config(SERVICE_ACCESS_MODE="marketplace"):
            self.withdraw(self.applicant)
        event = Event.objects.get(event_type=EventType.PROPOSAL_CANCELED)
        self.assertEqual(
            event.message,
            f"Access request {self.proposal.name} was withdrawn by "
            f"{self.applicant.full_name or self.applicant.username}.",
        )

    def test_outsider_cannot_withdraw(self):
        response = self.withdraw(structure_factories.UserFactory())
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    def test_decided_proposal_cannot_be_withdrawn(self):
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        response = self.withdraw(self.applicant)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_award_awaiting_an_answer_is_declined_not_withdrawn(self):
        self.active.status = WorkflowStepInstanceStatuses.COMPLETED
        self.active.save()
        models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="award_response",
            # A call carries its award response only when its decision asks
            # for one, so this one is added here.
            call_step=models.CallWorkflowStep.objects.create(
                call=self.proposal.round.call,
                key="award_response",
                name="Award response",
                kind="applicant_action",
                outcome_set="acceptance",
                completer="applicant",
                order=99,
            ),
            status=WorkflowStepInstanceStatuses.ACTIVE,
        )
        response = self.withdraw(self.applicant)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("decline", str(response.data))
