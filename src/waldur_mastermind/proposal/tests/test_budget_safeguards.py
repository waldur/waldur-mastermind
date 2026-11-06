"""A budget declines no proposal it has room for, and tells its figures only
to those who read the call's budgets."""

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import budgets, round_lifecycle
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_resource_budgets import BudgetTestBase

NOTIFY = "waldur_mastermind.proposal.tasks.notify_proposal_decision"


class FirstComeFirstServedRecheckTest(BudgetTestBase):
    def submitted(self, amount, minutes):
        start = timezone.now() - timedelta(days=20)
        return self.make_decidable(
            amount, submitted_at=start + timedelta(minutes=minutes)
        )

    def allocate(self, **data):
        self.client.force_authenticate(self.call_manager)
        url = factories.RoundFactory.get_url(
            self.call, self.round, "allocate_in_submission_order"
        )
        with mock.patch(NOTIFY), self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data, format="json")

    def test_budget_freed_after_planning_declines_nothing(self):
        row = self.budget(offered_total=100)
        first = self.submitted(60, minutes=10)
        second = self.submitted(50, minutes=20)
        stale = round_lifecycle.plan_submission_order(self.round, self.call_manager)
        self.assertEqual(
            [entry["planned_outcome"] for entry in stale[0]],
            ["approved", "declined"],
        )
        # The budget grows before the plan is carried out.
        row.offered_total = 200
        row.save(update_fields=["offered_total"])

        with mock.patch.object(
            round_lifecycle, "plan_submission_order", return_value=stale
        ):
            response = self.allocate(preview=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, ProposalStates.ACCEPTED)
        self.assertEqual(second.state, ProposalStates.IN_REVIEW)
        self.assertEqual(
            [entry["proposal_name"] for entry in response.data["skipped"]],
            [second.name],
        )

    def test_still_depleted_is_declined(self):
        self.budget(offered_total=100)
        self.submitted(60, minutes=10)
        second = self.submitted(50, minutes=20)

        response = self.allocate(preview=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        second.refresh_from_db()
        self.assertEqual(second.state, ProposalStates.REJECTED)


class BudgetFiguresDisclosureTest(BudgetTestBase):
    def setUp(self):
        super().setUp()
        self.call.budget_enforcement = "hard"
        self.call.save()
        self.budget(offered_total=100)
        self.decide(self.make_decidable(80))
        self.proposal = self.make_decidable(50)

    def refusal(self, user):
        with self.assertRaises(budgets.BudgetExceeded) as context:
            budgets.enforce(self.proposal, user)
        return str(context.exception)

    def test_the_call_team_reads_what_remains(self):
        self.assertIn("remaining 20", self.refusal(self.call_manager))

    def test_a_decider_outside_the_call_team_does_not(self):
        outsider = structure_factories.UserFactory()

        message = self.refusal(outsider)

        self.assertIn("exceeds what remains", message)
        self.assertNotIn("20", message)
        self.assertNotIn("remaining", message)

    def test_an_automatic_decision_is_explained_to_the_call_team(self):
        self.assertIn("remaining 20", self.refusal(None))
