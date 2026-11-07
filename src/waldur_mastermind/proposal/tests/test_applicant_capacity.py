import datetime

from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures

LIMIT = 4


class ApplicantCapacityMixin:
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.round = self.fixture.round
        self.manager = self.fixture.manager
        self.manager.max_active_proposals_per_applicant = LIMIT
        self.manager.save()
        self.applicant = structure_factories.UserFactory()

    def pending(self, count=1, applicant=None, round=None):
        for _ in range(count):
            factories.ProposalFactory(
                round=round or self.round,
                created_by=applicant or self.applicant,
                state=ProposalStates.SUBMITTED,
                project=None,
            )

    def running(self, count=1, **project_kwargs):
        for _ in range(count):
            factories.ProposalFactory(
                round=self.round,
                created_by=self.applicant,
                state=ProposalStates.ACCEPTED,
                project=structure_factories.ProjectFactory(**project_kwargs),
            )

    def check(self):
        self.client.force_authenticate(self.applicant)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data


class ApplicantCapacityEligibilityTest(ApplicantCapacityMixin, test.APITestCase):
    def test_two_projects_and_one_proposal_are_eligible(self):
        self.running(2)
        self.pending(1)
        self.assertTrue(self.check()["is_eligible"])

    def test_three_projects_and_one_proposal_are_not_eligible(self):
        self.running(3)
        self.pending(1)
        data = self.check()
        self.assertFalse(data["is_eligible"])
        self.assertEqual(len(data["restrictions"]), 1)
        self.assertIn("maximum of 4 active projects", data["restrictions"][0])

    def test_four_projects_are_not_eligible(self):
        self.running(4)
        self.assertFalse(self.check()["is_eligible"])

    def test_no_limit_admits_any_number(self):
        self.manager.max_active_proposals_per_applicant = None
        self.manager.save()
        self.running(4)
        self.pending(2)
        self.assertTrue(self.check()["is_eligible"])

    def test_proposals_in_review_count(self):
        self.pending(3)
        factories.ProposalFactory(
            round=self.round,
            created_by=self.applicant,
            state=ProposalStates.IN_REVIEW,
            project=None,
        )
        self.assertFalse(self.check()["is_eligible"])

    def test_awarded_proposal_awaiting_its_project_counts(self):
        self.pending(3)
        factories.ProposalFactory(
            round=self.round,
            created_by=self.applicant,
            state=ProposalStates.ACCEPTED,
            project=None,
        )
        self.assertFalse(self.check()["is_eligible"])

    def test_closed_proposals_do_not_count(self):
        self.pending(3)
        for state in (
            ProposalStates.DRAFT,
            ProposalStates.REJECTED,
            ProposalStates.CANCELED,
        ):
            factories.ProposalFactory(
                round=self.round, created_by=self.applicant, state=state
            )
        self.assertTrue(self.check()["is_eligible"])

    def test_completed_project_does_not_count(self):
        self.pending(3)
        factories.ProposalFactory(
            round=self.round,
            created_by=self.applicant,
            state=ProposalStates.ACCEPTED,
            completed_at=timezone.now(),
        )
        self.assertTrue(self.check()["is_eligible"])

    def test_project_past_its_end_date_does_not_count(self):
        self.pending(3)
        self.running(end_date=timezone.localdate() - datetime.timedelta(days=1))
        self.assertTrue(self.check()["is_eligible"])

    def test_project_ending_today_still_counts(self):
        self.pending(3)
        self.running(end_date=timezone.localdate())
        self.assertFalse(self.check()["is_eligible"])

    def test_removed_project_does_not_count(self):
        self.pending(3)
        self.running(is_removed=True)
        self.assertTrue(self.check()["is_eligible"])

    def test_team_membership_on_others_proposals_does_not_count(self):
        self.pending(3)
        someone_else = structure_factories.UserFactory()
        self.pending(2, applicant=someone_else)
        for proposal in models.Proposal.objects.filter(created_by=someone_else):
            proposal.add_user(self.applicant, ProposalRole.MANAGER)
        self.assertTrue(self.check()["is_eligible"])

    def test_other_calls_of_the_organization_count(self):
        self.pending(3)
        other_call = factories.CallFactory(manager=self.manager)
        self.pending(1, round=factories.RoundFactory(call=other_call))
        self.assertFalse(self.check()["is_eligible"])

    def test_other_organizations_calls_do_not_count(self):
        self.pending(3)
        other_call = factories.CallFactory()
        self.pending(2, round=factories.RoundFactory(call=other_call))
        self.assertTrue(self.check()["is_eligible"])

    def test_limit_is_an_eligibility_restriction(self):
        self.client.force_authenticate(self.applicant)
        response = self.client.get(factories.CallFactory.get_public_url(self.call))
        self.assertTrue(response.data["has_eligibility_restrictions"])

        self.manager.max_active_proposals_per_applicant = None
        self.manager.save()
        response = self.client.get(factories.CallFactory.get_public_url(self.call))
        self.assertFalse(response.data["has_eligibility_restrictions"])


class ApplicantCapacityEnforcementTest(ApplicantCapacityMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        # Mirrors permissions.yaml.
        ProposalRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL)

    def create(self):
        self.client.force_authenticate(self.applicant)
        return self.client.post(
            factories.ProposalFactory.get_list_url(),
            {"round_uuid": self.round.uuid.hex, "name": "Fifth"},
            format="json",
        )

    def draft(self):
        proposal = factories.ProposalFactory(
            round=self.round, created_by=self.applicant
        )
        proposal.add_user(self.applicant, ProposalRole.MANAGER)
        return proposal

    def submit(self, proposal, user=None):
        self.client.force_authenticate(user or self.applicant)
        return self.client.post(factories.ProposalFactory.get_url(proposal, "submit"))

    def test_proposal_cannot_be_created_at_the_limit(self):
        self.pending(4)
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("maximum of 4", str(response.data))

    def test_proposal_can_be_created_below_the_limit(self):
        self.pending(3)
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_draft_cannot_be_submitted_at_the_limit(self):
        proposal = self.draft()
        self.pending(4)
        response = self.submit(proposal)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("maximum of 4", str(response.data))
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.DRAFT)

    def test_draft_is_submitted_below_the_limit(self):
        proposal = self.draft()
        self.pending(3)
        response = self.submit(proposal)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_co_manager_submission_counts_against_the_applicant(self):
        proposal = self.draft()
        co_manager = structure_factories.UserFactory()
        proposal.add_user(co_manager, ProposalRole.MANAGER)
        self.pending(4)
        response = self.submit(proposal, user=co_manager)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("maximum of 4", str(response.data))


class CallManagingOrganisationLimitTest(test.APITestCase):
    def test_staff_sets_the_limit(self):
        fixture = fixtures.ProposalFixture()
        self.client.force_authenticate(fixture.staff)
        response = self.client.patch(
            factories.CallManagingOrganisationFactory.get_url(fixture.manager),
            {"max_active_proposals_per_applicant": 3},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["max_active_proposals_per_applicant"], 3)
        fixture.manager.refresh_from_db()
        self.assertEqual(fixture.manager.max_active_proposals_per_applicant, 3)

        response = self.client.patch(
            factories.CallManagingOrganisationFactory.get_url(fixture.manager),
            {"max_active_proposals_per_applicant": None},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        fixture.manager.refresh_from_db()
        self.assertIsNone(fixture.manager.max_active_proposals_per_applicant)

    def test_limit_must_be_positive(self):
        fixture = fixtures.ProposalFixture()
        self.client.force_authenticate(fixture.staff)
        response = self.client.patch(
            factories.CallManagingOrganisationFactory.get_url(fixture.manager),
            {"max_active_proposals_per_applicant": 0},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
