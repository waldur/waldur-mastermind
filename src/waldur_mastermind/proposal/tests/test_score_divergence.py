"""A step that runs only where an earlier step's scores diverge.

The AI for Science consensus report: the experts' evaluation is followed by
a consensus report only when the experts' scores lie more than a number of
points apart; otherwise the proposal skips it and nobody is told about it.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    contributions,
    models,
    serializers,
    workflow_checks,
    workflow_description,
    workflow_scenarios,
)
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    OutcomeSets,
    ResponsibleRoles,
    StepKinds,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_call_steps import CallStepsTestMixin
from waldur_mastermind.proposal.tests.test_workflow_dry_run import DryRunMixin

DISPATCH = "waldur_mastermind.proposal.notification_rules.dispatch_step_event"


class ScoreDivergenceTest(CallStepsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.evaluation = self.add_step(
            "expert-evaluation",
            StepKinds.ASSESSMENT,
            "Expert evaluation",
            1,
            contributor_source=ContributorSources.ASSIGNED_REVIEWERS,
            completer=ResponsibleRoles.CALL_MANAGER,
            outcome_set=OutcomeSets.REVIEWED,
        )
        self.consensus = self.add_step(
            "consensus-report",
            StepKinds.ASSESSMENT,
            "Consensus report",
            2,
            completer=ResponsibleRoles.CALL_MANAGER,
            outcome_set=OutcomeSets.DONE,
            run_condition={"step": "expert-evaluation", "score_spread_above": 1},
        )

    def review(self, score):
        review = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=structure_factories.UserFactory(),
            state=models.Review.States.SUBMITTED,
            summary_score=score,
        )
        contributions.attach_review(review)

    def evaluate(self, *scores):
        self.submit()
        self.assertEqual(self.complete("eligible").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.workflow_step, "expert-evaluation")
        for score in scores:
            self.review(score)
        with mock.patch(DISPATCH) as dispatch:
            response = self.complete("reviewed")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [call.args[0].step for call in dispatch.call_args_list]

    def consensus_status(self):
        return self.proposal.workflow_step_instances.get(step="consensus-report").status

    def test_diverging_scores_run_the_step(self):
        notified = self.evaluate(2, 5)
        self.assertEqual(self.proposal.workflow_step, "consensus-report")
        self.assertEqual(self.consensus_status(), WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIn("consensus-report", notified)

    def test_agreeing_scores_skip_it_without_mail(self):
        notified = self.evaluate(4, 5)
        self.assertEqual(self.consensus_status(), WorkflowStepInstanceStatuses.SKIPPED)
        self.assertEqual(self.proposal.workflow_step, "allocation_decision")
        self.assertNotIn("consensus-report", notified)

    def test_a_single_score_never_diverges(self):
        self.evaluate(1)
        self.assertEqual(self.consensus_status(), WorkflowStepInstanceStatuses.SKIPPED)

    def test_the_spread_is_read_from_the_steps_reviews(self):
        self.evaluate(1, 3, 2)
        instance = self.proposal.workflow_step_instances.get(step="expert-evaluation")
        self.assertEqual(sorted(contributions.contribution_scores(instance)), [1, 2, 3])
        self.assertEqual(contributions.score_spread(instance), 2)

    def test_the_condition_is_validated(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CallWorkflowStepFactory.get_url(self.call, self.consensus)
        for condition in (
            {"step": "expert-evaluation", "score_spread_above": -1},
            {"step": "expert-evaluation", "score_spread_above": "far"},
            {"step": "expert-evaluation", "score_spread_above": 1, "outcomes": []},
        ):
            response = self.client.patch(
                url, {"run_condition": condition}, format="json"
            )
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, condition
            )
        # The administrative check has no contributors to score anything.
        response = self.client.patch(
            url,
            {
                "run_condition": {
                    "step": "administrative_check",
                    "score_spread_above": 1,
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no contributors", str(response.data))

    def test_the_description_and_the_checker_read_it(self):
        description = workflow_description.describe_call(self.call)
        step = next(s for s in description["steps"] if s["key"] == "consensus-report")
        self.assertEqual(
            step["run_condition"],
            {
                "step": "expert-evaluation",
                "outcomes": [],
                "score_spread_above": 1,
            },
        )
        codes = {f["code"] for f in workflow_checks.check_call(self.call)}
        self.assertNotIn("condition_scores_cannot_diverge", codes)
        self.evaluation.max_contributions = 1
        self.evaluation.save()
        findings = [
            f
            for f in workflow_checks.check_call(self.call)
            if f["code"] == "condition_scores_cannot_diverge"
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["step"], "consensus-report")


class ScoreDivergenceDryRunTest(DryRunMixin, test.APITestCase):
    """The AI for Science template's consensus report in a dry run."""

    def run_with(self, scores):
        scenario = {
            "key": "scores",
            "name": "Scores",
            "request": {
                "offerings": 1,
                "actions": [
                    {"step": "administrative_check", "outcome": "eligible"},
                    {"step": "technical_assessment", "outcome": "feasible"},
                    {"advance": True},
                    {
                        "step": "expert_evaluation",
                        "outcome": "reviewed",
                        "scores": scores,
                    },
                ],
            },
            "assertions": [],
        }
        call = self.scenario_call("ai_for_science", offerings=1)
        return workflow_scenarios.run_scenario(call, scenario)

    def test_diverging_scores_open_the_consensus_report(self):
        result = self.run_with([1, 4])
        self.assertEqual(result["status"], "passed", result)
        self.assertIn(["consensus_report", "active"], result["path"])

    def test_agreeing_scores_skip_it(self):
        result = self.run_with([4, 4])
        self.assertEqual(result["status"], "passed", result)
        self.assertIn(["consensus_report", "skipped"], result["path"])

    def test_more_scores_than_the_step_takes_are_refused(self):
        result = self.run_with([1, 2, 3])
        self.assertEqual(result["status"], "error")
        self.assertIn("at most 2 reviews", result["error"])

    def test_scores_go_with_an_outcome(self):
        serializer = serializers.DryRunActionSerializer(
            data={"step": "expert_evaluation", "scores": [1, 2]}
        )
        self.assertFalse(serializer.is_valid())
