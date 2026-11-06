"""Why an active step cannot move on, for the call manager reading it.

A step whose people are missing -- a completer role nobody holds, a partition
without a team, an evaluator role without holders, too few reviewers -- says
so on the proposal's workflow states, with the way forward.
"""

from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole, OfferingRole
from waldur_core.permissions.utils import get_users
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    COIStatuses,
    ContributorSources,
    OutcomeSets,
    ResponsibleRoles,
    StepKinds,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_call_steps import CallStepsTestMixin
from waldur_mastermind.proposal.tests.test_lead_only_contributions import (
    LeadOnlyStepMixin,
)
from waldur_mastermind.proposal.tests.test_proposal_roles import (
    EvaluatorRoleTestMixin,
)
from waldur_mastermind.proposal.tests.test_step_contributions import (
    PerOfferingAssessmentBase,
)


def waiting_for(testcase, proposal, user):
    testcase.client.force_authenticate(user)
    response = testcase.client.get(
        factories.ProposalFactory.get_url(proposal, action="workflow_states")
    )
    testcase.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
    active = [step for step in response.data if step["status"] == "active"]
    testcase.assertEqual(len(active), 1, response.data)
    return active[0]["waiting_for"]


STAFFING_FIELDS = ("waiting_for", "awaiting_lead", "completion_warning")


def assert_applicant_reads_no_staffing(testcase, proposal):
    """The applicant team reads none of why a step waits: who is missing,
    who has a conflict of interest, how many reviewers are assigned."""
    testcase.client.force_authenticate(testcase.fixture.proposal_creator)
    response = testcase.client.get(
        factories.ProposalFactory.get_url(proposal, action="workflow_states")
    )
    testcase.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
    for step in response.data:
        for field in STAFFING_FIELDS:
            testcase.assertIsNone(step[field], (step["step"], field))


class CompleterNobodyHoldsTest(CallStepsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.committee = self.add_step(
            "committee",
            StepKinds.DECISION,
            "Committee meeting",
            1,
            outcome_set=OutcomeSets.APPROVAL,
            completer=ResponsibleRoles.PANEL_MEMBER,
        )

    def reach_committee(self):
        self.submit()
        response = self.complete("eligible")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.workflow_step, "committee")

    def test_a_panel_step_without_a_panel_says_so(self):
        self.assertFalse(
            get_users(self.call, role_name=CallRole.PANEL_MEMBER.name).exists()
        )
        self.reach_committee()
        message = waiting_for(self, self.proposal, self.fixture.call_manager)
        self.assertIn("panel member", message)
        self.assertIn("Team tab", message)

    def test_a_panel_step_with_a_panel_does_not(self):
        self.call.add_user(self.fixture.panel_member, CallRole.PANEL_MEMBER)
        self.reach_committee()
        self.assertIsNone(waiting_for(self, self.proposal, self.fixture.call_manager))

    def test_a_call_manager_step_never_waits_on_anyone(self):
        self.submit()
        self.assertIsNone(waiting_for(self, self.proposal, self.fixture.call_manager))


class TeamlessPartitionTest(PerOfferingAssessmentBase):
    def test_an_offering_without_a_team_is_named(self):
        self.offering_b.remove_user(self.manager_b, OfferingRole.MANAGER)
        message = waiting_for(self, self.proposal, self.call_manager)
        self.assertIn(self.offering_b.name, message)
        self.assertNotIn(self.offering_a.name, message)
        self.assertIn("call manager can submit", message)

    def test_offerings_with_teams_wait_for_them(self):
        self.assertIsNone(waiting_for(self, self.proposal, self.call_manager))


class EvaluatorRoleWithoutHoldersTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.questions = self.add_step(
            "questions",
            StepKinds.ASSESSMENT,
            "Questions",
            2,
            outcome_set=OutcomeSets.DONE,
            contributor_source=f"{ContributorSources.PROPOSAL_ROLE}:{self.role.pk}",
            completer=ResponsibleRoles.CALL_MANAGER,
        )

    def test_the_assignment_step_names_the_holders_missing(self):
        self.reach_assignment_step()
        message = waiting_for(self, self.proposal, self.manager)
        self.assertIn("0 of 2 required holders", message)
        assert_applicant_reads_no_staffing(self, self.proposal)

    def test_holders_yet_to_confirm_are_named(self):
        self.role.min_holders = 0
        self.role.allows_lead = False
        self.role.save()
        self.reach_assignment_step()
        self.assign(self.panel_1)
        models.ProposalRoleAssignment.objects.filter(proposal=self.proposal).update(
            coi_confirmation_required=True, coi_confirmed_at=None
        )
        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        message = waiting_for(self, self.proposal, self.manager)
        self.assertIn("conflict of interest", message)

    def test_a_role_without_holders_is_named(self):
        self.role.min_holders = 0
        self.role.allows_lead = False
        self.role.save()
        self.reach_assignment_step()
        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        message = waiting_for(self, self.proposal, self.manager)
        self.assertIn("Nobody holds the role 'Rapporteur'", message)


class TooFewReviewersTest(CallStepsTestMixin, test.APITestCase):
    def test_a_review_step_without_reviewers_asks_for_more(self):
        review = self.call.workflow_steps.get(key="expert_review")
        review.is_enabled = True
        review.contributor_source = ContributorSources.ASSIGNED_REVIEWERS
        review.min_contributions = 2
        review.save()
        models.CallWorkflowStep.objects.filter(
            call=self.call, key="administrative_check"
        ).update(is_enabled=False)
        self.submit()
        self.assertEqual(self.proposal.workflow_step, "expert_review")
        message = waiting_for(self, self.proposal, self.fixture.call_manager)
        self.assertIn("0 of 2 required reviews", message)


class LeadConflictWordingTest(LeadOnlyStepMixin, test.APITestCase):
    """A lead who has not confirmed is asked to; a lead with a conflict on
    record is to be replaced."""

    def setUp(self):
        super().setUp()
        self.reach_consolidated_step()

    def test_a_lead_yet_to_confirm(self):
        models.ProposalRoleAssignment.objects.filter(
            proposal=self.proposal, user=self.lead
        ).update(coi_confirmation_required=True, coi_confirmed_at=None)
        message = waiting_for(self, self.proposal, self.manager)
        self.assertIn("has not yet confirmed having no conflict of interest", message)
        self.assertNotIn("recorded", message)

    def test_a_lead_with_a_recorded_conflict(self):
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.lead),
            proposal=self.proposal,
            status=COIStatuses.PENDING,
        )
        message = waiting_for(self, self.proposal, self.manager)
        self.assertIn("has a recorded conflict of interest", message)
        self.assertIn("replace them", message)
        self.assertIn("conflict of interest", self.awaiting_lead())
        assert_applicant_reads_no_staffing(self, self.proposal)


class ReviewersShortAheadTest(CallStepsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.add_step(
            "reviewer-assignment", StepKinds.ASSIGNMENT, "Reviewer assignment", 1
        )
        review = self.call.workflow_steps.get(key="expert_review")
        review.is_enabled = True
        review.contributor_source = ContributorSources.ASSIGNED_REVIEWERS
        review.min_contributions = 2
        review.save()
        self.review_name = review.name
        self.submit()
        response = self.complete("eligible")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def warning(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        )
        active = next(s for s in response.data if s["status"] == "active")
        self.assertEqual(active["step"], "reviewer-assignment")
        return active["completion_warning"]

    def test_the_assignment_says_how_many_reviewers_are_missing(self):
        self.assertEqual(
            self.warning(),
            f"{self.review_name} needs 2 reviews; 0 reviewers are assigned.",
        )

    def test_the_applicant_reads_no_warning(self):
        self.assertIsNotNone(self.warning())
        assert_applicant_reads_no_staffing(self, self.proposal)

    def test_enough_reviewers_need_no_warning(self):
        for reviewer in (self.fixture.reviewer_1, self.fixture.reviewer_2):
            factories.ReviewFactory(proposal=self.proposal, reviewer=reviewer)
        self.assertIsNone(self.warning())

    def test_completing_anyway_is_not_refused(self):
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
