"""Allocating a round first come, first served, until its pools run out."""

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework import status

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import ProposalRole
from waldur_mastermind.proposal import (
    budgets,
    models,
    round_lifecycle,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    ChangeRequestParts,
    ProposalStates,
    ResultsPublication,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_resource_budgets import BudgetTestBase

NOTIFY = "waldur_mastermind.proposal.tasks.notify_proposal_decision"


class AllocateInSubmissionOrderTest(BudgetTestBase):
    def setUp(self):
        super().setUp()
        self.start = timezone.now() - timedelta(days=20)

    def submitted(self, amount, minutes):
        return self.make_decidable(
            amount, submitted_at=self.start + timedelta(minutes=minutes)
        )

    def allocate(self, user=None, **data):
        self.client.force_authenticate(user or self.call_manager)
        url = factories.RoundFactory.get_url(
            self.call, self.round, "allocate_in_submission_order"
        )
        with mock.patch(NOTIFY), self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data, format="json")

    def planned(self, response):
        return [
            (row["proposal_name"], row["planned_outcome"])
            for row in response.data["plan"]
        ]

    def test_preview_plans_in_submission_order_and_decides_nothing(self):
        self.budget(offered_total=100)
        late = self.submitted(30, minutes=30)
        early = self.submitted(60, minutes=10)
        middle = self.submitted(50, minutes=20)

        response = self.allocate()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["preview"])
        self.assertEqual(
            self.planned(response),
            [
                (early.name, "approved"),
                # Does not fit what is left, 40; the later, smaller one does.
                (middle.name, "declined"),
                (late.name, "approved"),
            ],
        )
        [row] = response.data["budgets"]
        self.assertEqual(row["remaining_before"], 100)
        self.assertEqual(row["remaining_after"], 10)
        for proposal in (early, middle, late):
            self.assertEqual(
                self.decision(proposal).status, WorkflowStepInstanceStatuses.ACTIVE
            )

    def test_applying_decides_in_order(self):
        self.budget(offered_total=100)
        early = self.submitted(60, minutes=10)
        middle = self.submitted(50, minutes=20)
        late = self.submitted(30, minutes=30)

        response = self.allocate(preview=False)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["completed"]), 3)
        for proposal in (early, middle, late):
            proposal.refresh_from_db()
        self.assertEqual(early.state, ProposalStates.ACCEPTED)
        self.assertEqual(middle.state, ProposalStates.REJECTED)
        self.assertEqual(late.state, ProposalStates.ACCEPTED)
        self.assertEqual(
            self.decision(middle).outcome_reason,
            "The resources of this cut-off are depleted.",
        )
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.ROUND_ALLOCATED_IN_SUBMISSION_ORDER
            ).exists()
        )

    def test_an_award_must_fit_both_the_round_and_the_call_wide_pool(self):
        earlier = factories.RoundFactory(
            call=self.call,
            start_time=self.round.start_time - timedelta(days=60),
            cutoff_time=self.round.start_time - timedelta(days=31),
        )
        self.budget(offered_total=100)
        self.budget(offered_total=70, call_round=self.round)
        # The earlier cut-off took half of what the call offers.
        self.decide(self.make_decidable(50, call_round=earlier))
        first = self.submitted(40, minutes=10)
        # Fits the round's 30 left, not the call's 10.
        second = self.submitted(20, minutes=20)
        third = self.submitted(10, minutes=30)

        response = self.allocate()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.planned(response),
            [
                (first.name, "approved"),
                (second.name, "declined"),
                (third.name, "approved"),
            ],
        )
        remaining = {
            row["round_name"]: (row["remaining_before"], row["remaining_after"])
            for row in response.data["budgets"]
        }
        self.assertEqual(remaining, {None: (50, 0), self.round.name: (70, 20)})

    def test_leave_keeps_what_does_not_fit_for_a_decision_by_hand(self):
        self.budget(offered_total=50)
        self.submitted(40, minutes=10)
        second = self.submitted(40, minutes=20)
        response = self.allocate(preview=False, on_depleted="leave")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        second.refresh_from_db()
        self.assertEqual(second.state, ProposalStates.IN_REVIEW)
        self.assertEqual(
            [entry["proposal_name"] for entry in response.data["skipped"]],
            [second.name],
        )

    def test_below_the_quality_threshold_is_declined_even_with_resources_left(self):
        self.budget(offered_total=1000)
        weak = self.submitted(10, minutes=10)
        strong = self.submitted(10, minutes=20)
        decision_step = self.decision(weak).call_step
        decision_step.min_score_threshold = 3
        decision_step.save()
        factories.ReviewFactory(
            proposal=weak, state=models.Review.States.SUBMITTED, summary_score=2
        )
        factories.ReviewFactory(
            proposal=strong, state=models.Review.States.SUBMITTED, summary_score=4
        )
        response = self.allocate()
        self.assertEqual(
            self.planned(response),
            [(weak.name, "declined"), (strong.name, "approved")],
        )
        self.assertEqual(
            response.data["plan"][0]["reason"], "Below the quality threshold."
        )

    def test_refused_while_a_proposal_is_before_the_decision(self):
        self.submitted(10, minutes=10)
        factories.ProposalFactory(round=self.round, state=ProposalStates.SUBMITTED)
        response = self.allocate()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["waiting_count"], 1)

        response = self.allocate(force=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reason", response.data)

        response = self.allocate(force=True, reason="Board meets today")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_refused_before_the_cutoff(self):
        self.round.cutoff_time = timezone.now() + timedelta(days=1)
        self.round.save()
        self.assertEqual(self.allocate().status_code, status.HTTP_400_BAD_REQUEST)

    def test_without_a_budget_everything_that_passes_is_approved(self):
        first = self.submitted(10_000, minutes=10)
        second = self.submitted(10_000, minutes=20)
        response = self.allocate()
        self.assertFalse(response.data["has_budget"])
        self.assertEqual(
            self.planned(response),
            [(first.name, "approved"), (second.name, "approved")],
        )

    def test_held_decisions_wait_for_publication(self):
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save()
        self.budget(offered_total=100)
        proposal = self.submitted(60, minutes=10)
        with mock.patch(
            "waldur_mastermind.proposal.tasks.notify_proposal_decision"
        ) as notify:
            self.client.force_authenticate(self.call_manager)
            url = factories.RoundFactory.get_url(
                self.call, self.round, "allocate_in_submission_order"
            )
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(url, {"preview": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        proposal.refresh_from_db()
        self.assertTrue(proposal.decision_held)
        notify.assert_not_called()
        # The held award still reserves its amount.
        self.assertEqual(
            budgets.awarded_so_far(models.CallResourcePool.objects.get()), 60
        )

    def test_a_concurrent_approval_makes_the_planned_one_fail_alone(self):
        self.call.budget_enforcement = "hard"
        self.call.save()
        self.budget(offered_total=100)
        first = self.submitted(60, minutes=10)
        second = self.submitted(30, minutes=20)
        manual = self.make_decidable(30)
        manual.submitted_at = None
        manual.save()
        stale = round_lifecycle.plan_submission_order(self.round, self.call_manager)

        # A call manager approves another proposal by hand meanwhile.
        self.assertEqual(self.decide(manual).status_code, status.HTTP_200_OK)

        with mock.patch.object(
            round_lifecycle, "plan_submission_order", return_value=stale
        ):
            response = self.allocate(preview=False)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, ProposalStates.ACCEPTED)
        self.assertEqual(second.state, ProposalStates.IN_REVIEW)
        [failed] = response.data["failed"]
        self.assertEqual(failed["proposal_name"], second.name)
        self.assertIn("budget", failed["detail"])

    def test_staff_on_the_applicant_team_is_skipped(self):
        proposal = self.submitted(10, minutes=10)
        other = self.submitted(10, minutes=20)
        proposal.add_user(self.staff, ProposalRole.MEMBER)
        response = self.allocate(user=self.staff, preview=False)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        proposal.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(other.state, ProposalStates.ACCEPTED)
        self.assertEqual(
            [entry["proposal_name"] for entry in response.data["skipped"]],
            [proposal.name],
        )

    def test_a_call_manager_on_an_applicant_team_cannot_see_the_plan(self):
        proposal = self.submitted(10, minutes=10)
        proposal.add_user(self.call_manager, ProposalRole.MEMBER)
        self.assertEqual(self.allocate().status_code, status.HTTP_403_FORBIDDEN)

    def test_only_those_updating_the_call_may_allocate(self):
        proposal = self.submitted(10, minutes=10)
        self.assertIn(
            self.allocate(user=proposal.created_by).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertIn(
            self.allocate(user=self.support).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_resubmitting_requested_changes_keeps_the_submission_time(self):
        first = self.submitted(10, minutes=10)
        second = self.submitted(10, minutes=20)
        submitted_at = first.submitted_at
        # Changes are asked at a step before the decision; the check stands
        # in for one, active again for the purpose.
        instance = first.workflow_step_instances.get(step="administrative_check")
        instance.status = WorkflowStepInstanceStatuses.ACTIVE
        instance.save()
        instance.call_step.allows_change_requests = True
        instance.call_step.save()
        workflow_service.request_changes(
            first,
            instance,
            self.call_manager,
            "Fix the plan",
            [ChangeRequestParts.DOCUMENTS],
        )
        workflow_service.submit_changes(first, first.created_by)
        instance.status = WorkflowStepInstanceStatuses.COMPLETED
        instance.save()
        first.refresh_from_db()
        self.assertEqual(first.submitted_at, submitted_at)
        response = self.allocate()
        self.assertEqual(
            [row["proposal_name"] for row in response.data["plan"]],
            [first.name, second.name],
        )
