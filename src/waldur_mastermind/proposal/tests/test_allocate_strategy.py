from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.fixtures import ProjectRole, ProposalRole
from waldur_core.permissions.utils import add_user, has_user
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class ManualAllocateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = models.Proposal.States.IN_REVISION
        self.proposal.save()
        self.allocate_url = factories.ProposalFactory.get_url(self.proposal, "allocate")
        self.reject_url = factories.ProposalFactory.get_url(self.proposal, "reject")

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_allocate_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.allocate_url, {"allocation_comment": "done"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, models.Proposal.States.ACCEPTED)
        self.assertEqual(self.proposal.allocation_comment, "done")
        self.assertTrue(self.proposal.requestedresource_set.first().resource)
        resource = self.proposal.requestedresource_set.first().resource
        self.assertTrue(
            marketplace_models.Order.objects.filter(resource=resource).exists()
        )

    def test_when_proposal_allocated_users_are_added(self):
        user = UserFactory()
        self.fixture.call.default_project_role = ProjectRole.MEMBER
        self.fixture.call.save()
        add_user(self.proposal, user, ProposalRole.MEMBER)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.allocate_url, {"allocation_comment": "done"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(has_user(self.proposal.project, user, ProjectRole.MEMBER))

    @data(
        "proposal_creator",
    )
    def test_user_can_not_allocate_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.allocate_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        "owner",
        "customer_support",
    )
    def test_customer_user_can_not_allocate_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.allocate_url)
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
        self.assertEqual(self.proposal.state, models.Proposal.States.REJECTED)
        self.assertEqual(self.proposal.allocation_comment, "done")

    @data(
        "proposal_creator",
    )
    def test_user_can_not_reject_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.reject_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
