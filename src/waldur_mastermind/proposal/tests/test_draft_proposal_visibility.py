"""A draft is the applicant team's until it is submitted."""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures


@ddt
class DraftProposalVisibilityTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.draft = self.fixture.proposal
        self.assertEqual(self.draft.state, ProposalStates.DRAFT)
        self.submitted = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.SUBMITTED
        )
        # The call's reviewer role is its pool: a reviewer reads a proposal
        # through the review they hold on it.
        factories.ReviewFactory(
            proposal=self.submitted, reviewer=self.fixture.reviewer_1
        )

    def _listed(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProposalFactory.get_list_url(),
            {"round_uuid": self.fixture.round.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {p["uuid"] for p in response.data}

    def _retrieved(self, user, proposal, action=None):
        self.client.force_authenticate(user)
        return self.client.get(factories.ProposalFactory.get_url(proposal, action))

    @data("reviewer_1", "panel_member")
    def test_evaluator_of_the_call_does_not_read_the_draft(self, role):
        user = getattr(self.fixture, role)

        listed = self._listed(user)
        self.assertNotIn(self.draft.uuid.hex, listed)
        self.assertIn(self.submitted.uuid.hex, listed)
        self.assertEqual(
            self._retrieved(user, self.draft).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(
            self._retrieved(user, self.draft, "resources").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self._retrieved(user, self.submitted).status_code, status.HTTP_200_OK
        )

    @data("reviewer_1", "panel_member")
    def test_evaluator_reads_the_proposal_once_submitted(self, role):
        user = getattr(self.fixture, role)
        self.draft.state = ProposalStates.SUBMITTED
        self.draft.save()
        factories.ReviewFactory(proposal=self.draft, reviewer=self.fixture.reviewer_1)

        self.assertEqual(
            self._retrieved(user, self.draft).status_code, status.HTTP_200_OK
        )

    @data("call_manager", "staff")
    def test_call_manager_and_staff_still_read_the_draft(self, role):
        user = getattr(self.fixture, role)

        self.assertIn(self.draft.uuid.hex, self._listed(user))
        self.assertEqual(
            self._retrieved(user, self.draft).status_code, status.HTTP_200_OK
        )

    def test_applicant_team_still_reads_the_draft(self):
        member = structure_factories.UserFactory()
        self.draft.add_user(member, ProposalRole.MEMBER)

        for user in (self.fixture.proposal_creator, member):
            with self.subTest(user=user.username):
                self.assertEqual(
                    self._retrieved(user, self.draft).status_code, status.HTTP_200_OK
                )

    def test_evaluator_who_is_on_the_applicant_team_reads_the_draft(self):
        panel_member = self.fixture.panel_member
        self.draft.add_user(panel_member, ProposalRole.MEMBER)

        self.assertEqual(
            self._retrieved(panel_member, self.draft).status_code, status.HTTP_200_OK
        )

    def test_reviewer_of_another_call_does_not_read_the_draft(self):
        other_call = factories.CallFactory(manager=self.fixture.manager)
        reviewer = structure_factories.UserFactory()
        other_call.add_user(reviewer, CallRole.REVIEWER)

        self.assertEqual(
            self._retrieved(reviewer, self.draft).status_code,
            status.HTTP_404_NOT_FOUND,
        )
