from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.fixtures import ProjectRole, ProposalRole
from waldur_core.permissions.utils import has_user
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class ManualApproveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()
        self.approve_url = factories.ProposalFactory.get_url(self.proposal, "approve")
        self.reject_url = factories.ProposalFactory.get_url(self.proposal, "reject")

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_approve_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.approve_url, {"allocation_comment": "done"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(self.proposal.allocation_comment, "done")
        self.assertTrue(self.proposal.requestedresource_set.first().resource)
        resource = self.proposal.requestedresource_set.first().resource
        self.assertTrue(
            marketplace_models.Order.objects.filter(resource=resource).exists()
        )

    def _check_membership(self, proposal_role, project_role=None) -> bool:
        user = UserFactory()
        self.proposal.add_user(user, proposal_role)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.approve_url, {"allocation_comment": "done"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()

        return has_user(self.proposal.project, user, project_role)

    def test_when_proposal_approved_users_are_added_to_the_project(self):
        project_role = ProjectRole.MEMBER
        proposal_role = ProposalRole.MEMBER
        models.ProposalProjectRoleMapping.objects.create(
            call=self.fixture.call,
            project_role=project_role,
            proposal_role=proposal_role,
        )
        result = self._check_membership(proposal_role, project_role)
        self.assertTrue(result)

    def test_unmapped_roles_are_not_added_to_the_project(self):
        models.ProposalProjectRoleMapping.objects.create(
            call=self.fixture.call,
            proposal_role=ProposalRole.MANAGER,
        )
        result = self._check_membership(ProposalRole.MANAGER)
        self.assertFalse(result)

    @data(
        "proposal_creator",
    )
    def test_user_can_not_approve_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        "owner",
        "customer_support",
    )
    def test_customer_user_can_not_approve_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_reject_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.reject_url, {"allocation_comment": "done"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)
        self.assertEqual(self.proposal.allocation_comment, "done")

    @data(
        "proposal_creator",
    )
    def test_user_can_not_reject_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.reject_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
