"""Whether a call has the people its evaluator roles and steps need.

The call manager configuring the call is warned when fewer people may hold
an evaluator role than its minimum of holders, or when a step takes more
contributions than the people it takes them from.
"""

from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    OutcomeSets,
    ProposalRoleEligibility,
    StepKinds,
)
from waldur_mastermind.proposal.tests.test_proposal_roles import (
    EvaluatorRoleTestMixin,
    call_url,
)


class StaffingWarningsTest(EvaluatorRoleTestMixin, test.APITestCase):
    def roles(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(call_url(self.call, "proposal_roles"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {role["key"]: role for role in response.data}

    def steps(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(call_url(self.call, "workflow_steps"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {step["key"]: step for step in response.data}

    def test_a_role_the_panel_can_staff_has_no_warning(self):
        role = self.roles()["rapporteur"]
        self.assertEqual(role["eligible_count"], 3)
        self.assertIsNone(role["holders_warning"])

    def test_a_role_the_panel_cannot_staff_says_what_is_missing(self):
        for member in (self.panel_2, self.panel_3):
            self.call.remove_user(member, CallRole.PANEL_MEMBER)
        role = self.roles()["rapporteur"]
        self.assertEqual(role["eligible_count"], 1)
        self.assertIn("only 1 panel member", role["holders_warning"])
        self.assertIn("2 holders of the role 'Rapporteur'", role["holders_warning"])
        self.assertIn("Team tab", role["holders_warning"])

    def test_a_role_anyone_may_hold_has_no_count(self):
        self.role.eligible_from = ProposalRoleEligibility.ANY_USER
        self.role.save()
        role = self.roles()["rapporteur"]
        self.assertIsNone(role["eligible_count"])
        self.assertIsNone(role["holders_warning"])

    def test_a_step_of_the_role_carries_its_warning(self):
        self.add_step(
            "questions",
            StepKinds.ASSESSMENT,
            "Questions",
            2,
            outcome_set=OutcomeSets.DONE,
            contributor_source=f"{ContributorSources.PROPOSAL_ROLE}:{self.role.pk}",
        )
        self.assertIsNone(self.steps()["questions"]["contributors_warning"])
        for member in (self.panel_1, self.panel_2, self.panel_3):
            self.call.remove_user(member, CallRole.PANEL_MEMBER)
        self.assertIn(
            "no panel members", self.steps()["questions"]["contributors_warning"]
        )

    def test_a_step_needing_more_panel_contributions_than_the_panel(self):
        self.add_step(
            "panel",
            StepKinds.DECISION,
            "Panel discussion",
            2,
            contributor_source="call_role:panel_member",
            min_contributions=4,
        )
        warning = self.steps()["panel"]["contributors_warning"]
        self.assertIn("only 3 panel members", warning)
        self.assertIn("needs 4 contributions", warning)

    def test_a_review_step_without_reviewers_in_the_pool(self):
        review = self.call.workflow_steps.get(key="expert_review")
        review.contributor_source = ContributorSources.ASSIGNED_REVIEWERS
        review.min_contributions = 2
        review.save()
        warning = self.steps()["expert_review"]["contributors_warning"]
        self.assertIn("needs 2 reviews", warning)
        self.assertIn("reviewer pool", warning)
