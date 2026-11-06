"""What an award does outside the proposal waits for the award itself: the
provider of an offering an item was moved to reads the proposal once the
award is released, and the award's approver is whoever decided it."""

from django.db import transaction
from rest_framework import status

from waldur_core.core.utils import get_system_robot
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models, workflow_service
from waldur_mastermind.proposal.enums import (
    AWARD_RESPONSE_KEY,
    ProposalStates,
    ResultsPublication,
    RoundLifecycleStates,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_automatic_decisions import (
    AutomaticDecisionBase,
)
from waldur_mastermind.proposal.tests.test_awarded_resources import (
    AwardedResourcesBase,
)


class MovedAwardVisibilityTest(AwardedResourcesBase):
    """An item moved onto an offering nobody requested shows the proposal to
    that offering's manager only once the award is released."""

    def setUp(self):
        super().setUp()
        CallRole.MANAGER.add_permission(PermissionEnum.UPDATE_CALL)
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save(update_fields=["publish_results"])
        self.round = self.proposal.round
        self.round.lifecycle_state = RoundLifecycleStates.EVALUATING
        self.round.save(update_fields=["lifecycle_state"])
        self._start_allocation()
        awarded = self._awarded()
        awarded.requested_offering = self.other_requested_offering
        awarded.plan = self.other_plan
        awarded.save()
        self.other_manager = structure_factories.UserFactory()
        self.other_offering.add_user(self.other_manager, OfferingRole.MANAGER)

    def get_as_other_manager(self):
        self.client.force_authenticate(self.other_manager)
        return self.client.get(factories.ProposalFactory.get_url(self.proposal))

    def test_not_while_the_award_is_a_draft(self):
        self.assertEqual(
            self.get_as_other_manager().status_code, status.HTTP_404_NOT_FOUND
        )

    def test_not_while_the_decision_is_held(self):
        self._decide()
        self.proposal.refresh_from_db()
        self.assertTrue(self.proposal.decision_held)

        self.assertEqual(
            self.get_as_other_manager().status_code, status.HTTP_404_NOT_FOUND
        )

    def test_once_the_award_is_released(self):
        self._decide()
        models.Proposal.objects.filter(pk=self.proposal.pk).update(decision_held=False)

        self.assertEqual(self.get_as_other_manager().status_code, status.HTTP_200_OK)


class AwardApproverTest(AutomaticDecisionBase):
    """The applicant accepting the award is not its approver: the decision's
    completer is, the system for an automatic one, on the proposal and as
    the consumer reviewer of its orders."""

    def configure(self):
        models.CallWorkflowStep.objects.filter(
            call=self.call, key=AWARD_RESPONSE_KEY
        ).delete()
        models.CallWorkflowStep.create_default(
            self.call, AWARD_RESPONSE_KEY, order=self.decision.order + 1
        )

    def test_applicant_accepting_the_award_is_not_its_approver(self):
        self._assess_both()
        self.proposal.refresh_from_db()
        applicant = self.proposal.created_by
        step = self.proposal.current_step
        self.assertEqual(step.step, AWARD_RESPONSE_KEY)

        with transaction.atomic():
            proposal = models.Proposal.objects.select_for_update().get(
                pk=self.proposal.pk
            )
            workflow_service.complete_step(
                proposal=proposal,
                current_instance=step,
                outcome=WorkflowStepOutcomes.ACCEPTED,
                outcome_reason="",
                completed_by=applicant,
            )

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        robot = get_system_robot()
        self.assertEqual(self.proposal.approved_by, robot)
        orders = marketplace_models.Order.objects.filter(project=self.proposal.project)
        self.assertTrue(orders.exists())
        for order in orders:
            self.assertEqual(order.consumer_reviewed_by, robot)
