"""What of a call's workflow those outside its team read.

The panel and the reviewers read the call's steps -- their names, kinds and
completers -- but not who holds each step's role, its notification rules,
status labels, run condition or score threshold. A call manager who applies
to their own call does not learn a decision held from them through the
call's aggregates.
"""

import datetime

from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    ProposalStates,
    ResultsPublication,
    RoundLifecycleStates,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, fixtures


class WorkflowStepsReadTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.steps_url = factories.CallFactory.get_protected_url(
            self.call, "workflow_steps"
        )

    def read(self, user, url=None):
        self.client.force_authenticate(user)
        response = self.client.get(url or self.steps_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_evaluators_read_the_steps_without_the_teams_configuration(self):
        for user in (self.fixture.reviewer_1, self.fixture.panel_member):
            steps = self.read(user)
            self.assertTrue(steps)
            for step in steps:
                self.assertTrue(step["key"])
                self.assertIn("completer", step)
                self.assertEqual(step["responsible_users"], [])
                self.assertEqual(step["notification_rules"], [])
                self.assertEqual(step["status_labels"], {})
                self.assertEqual(step["run_condition"], {})
                self.assertIsNone(step["min_score_threshold"])

    def test_evaluators_read_one_step_the_same_way(self):
        step = self.call.workflow_steps.order_by("order").first()
        url = factories.CallFactory.get_protected_url(
            self.call, f"workflow_steps/{step.uuid.hex}"
        )
        data = self.read(self.fixture.reviewer_1, url)
        self.assertEqual(data["key"], step.key)
        self.assertEqual(data["responsible_users"], [])
        self.assertEqual(data["notification_rules"], [])

    def test_the_call_team_reads_everything(self):
        _ = self.fixture.reviewer_1
        steps = self.read(self.fixture.call_manager)
        self.assertTrue(any(step["notification_rules"] for step in steps))
        self.assertTrue(
            any(
                user.get("email")
                for step in steps
                for user in step["responsible_users"]
            )
        )


class HeldDecisionMixin:
    """A proposal whose approved allocation decision is held for the round's
    publication, applied for by one of the call's managers."""

    outcome = WorkflowStepOutcomes.APPROVED

    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.UPDATE_CALL)
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save(update_fields=["publish_results"])
        now = timezone.now()
        self.round = factories.RoundFactory(
            call=self.call,
            start_time=now - datetime.timedelta(days=60),
            cutoff_time=now - datetime.timedelta(days=30),
        )
        self.round.lifecycle_state = RoundLifecycleStates.EVALUATING
        self.round.save(update_fields=["lifecycle_state"])
        self.proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.IN_REVIEW
        )
        step = models.CallWorkflowStep.objects.filter(
            call=self.call, is_allocation_decision=True
        ).first()
        factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            call_step=step,
            step=step.key,
            order=step.order,
            status=WorkflowStepInstanceStatuses.COMPLETED,
            started_at=now - datetime.timedelta(days=2),
            completed_at=now - datetime.timedelta(days=1),
            outcome=self.outcome,
            is_allocation_decision=True,
        )
        self.proposal.decision_held = True
        self.proposal.save(update_fields=["decision_held"])
        self.applicant = fixtures.author_as_manager(self.proposal)
        self.call.add_user(self.applicant, CallRole.MANAGER)

    def get(self, user, action, **params):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.CallFactory.get_protected_url(self.call, action), params
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data


class HeldDecisionAggregatesTest(HeldDecisionMixin, test.APITestCase):
    def results(self, user):
        data = self.get(user, "workflow-paths", round=self.round.uuid.hex)
        return [step["result"] for path in data["paths"] for step in path["steps"]]

    def outcomes(self, user):
        data = self.get(user, "workflow-progress", round=self.round.uuid.hex)
        return [o["outcome"] for step in data["steps"] for o in step["outcomes"]]

    def test_the_applicant_managing_the_call_does_not_read_it(self):
        self.assertNotIn(self.outcome, self.results(self.applicant))
        self.assertNotIn(self.outcome, self.outcomes(self.applicant))

    def test_the_rest_of_the_call_team_does(self):
        manager = self.fixture.call_manager
        self.assertIn(self.outcome, self.results(manager))
        self.assertIn(self.outcome, self.outcomes(manager))

    def test_the_held_decision_is_not_counted_for_the_applicant(self):
        url = factories.RoundFactory.get_url(self.call, self.round)
        self.client.force_authenticate(self.applicant)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["held_decisions_count"], 0)
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(url)
        self.assertEqual(response.data["held_decisions_count"], 1)


class HeldAwardBudgetTest(HeldDecisionMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        offering = self.fixture.requested_offering_accepted
        rule = models.CallOfferingComponent.objects.create(
            requested_offering=offering, component_type="cpu"
        )
        models.CallResourcePool.objects.create(
            offering_component=rule, offered_total=100
        )
        models.CallResourcePool.objects.create(
            offering_component=rule, round=self.proposal.round, offered_total=50
        )
        models.AwardedResource.objects.create(
            proposal=self.proposal,
            requested_offering=offering,
            limits={"cpu": 40},
        )

    def awarded(self, user):
        [row] = self.get(user, "resource_pool_usage")
        return {
            cell["round_name"]: (float(cell["awarded"]), cell["remaining"])
            for cell in row["cells"]
        }

    def test_the_applicant_does_not_read_their_held_award_in_the_pools(self):
        name = self.proposal.round.name
        cells = self.awarded(self.applicant)
        self.assertEqual((cells[None], cells[name]), ((0, 100), (0, 50)))
        cells = self.awarded(self.fixture.call_manager)
        self.assertEqual((cells[None], cells[name]), ((40, 60), (40, 10)))
