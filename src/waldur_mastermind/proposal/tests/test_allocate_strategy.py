from rest_framework import test

from waldur_core.permissions.fixtures import ProjectRole, ProposalRole
from waldur_core.permissions.utils import has_user
from waldur_core.structure.models import Project
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models, utils
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import fixtures


# The legacy one-click approve/reject endpoints were removed; the workflow
# engine is the single decision path (see test_workflow.py). These tests cover
# allocate_proposal — the shared provisioning routine both the workflow terminal
# and (historically) the legacy Accept relied on — directly.
class AllocateProposalTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.project = None
        self.proposal.save()

    def test_allocation_provisions_project_resource_and_order(self):
        utils.allocate_proposal(self.proposal, approved_by=self.fixture.staff)
        self.proposal.refresh_from_db()

        self.assertIsNotNone(self.proposal.project)
        self.assertEqual(self.proposal.approved_by, self.fixture.staff)
        resource = self.proposal.requestedresource_set.first().resource
        self.assertTrue(resource)
        self.assertTrue(
            marketplace_models.Order.objects.filter(resource=resource).exists()
        )

    def test_allocation_is_idempotent(self):
        # Re-allocating an already-provisioned proposal must NOT create a second
        # project / resource / order (the core F6.7 correctness guard).
        utils.allocate_proposal(self.proposal, approved_by=self.fixture.staff)
        self.proposal.refresh_from_db()
        project = self.proposal.project
        resource = self.proposal.requestedresource_set.first().resource
        order_count = marketplace_models.Order.objects.filter(resource=resource).count()
        project_count = Project.available_objects.count()

        utils.allocate_proposal(self.proposal, approved_by=self.fixture.staff)
        self.proposal.refresh_from_db()

        self.assertEqual(self.proposal.project, project)
        self.assertEqual(Project.available_objects.count(), project_count)
        self.assertEqual(
            marketplace_models.Order.objects.filter(resource=resource).count(),
            order_count,
        )

    def _allocate_and_check_membership(self, proposal_role, project_role=None) -> bool:
        user = UserFactory()
        self.proposal.add_user(user, proposal_role)
        utils.allocate_proposal(self.proposal, approved_by=self.fixture.staff)
        self.proposal.refresh_from_db()
        return has_user(self.proposal.project, user, project_role)

    def test_mapped_roles_are_added_to_the_project_on_allocation(self):
        models.ProposalProjectRoleMapping.objects.create(
            call=self.fixture.call,
            project_role=ProjectRole.MEMBER,
            proposal_role=ProposalRole.MEMBER,
        )
        self.assertTrue(
            self._allocate_and_check_membership(ProposalRole.MEMBER, ProjectRole.MEMBER)
        )

    def test_unmapped_roles_are_not_added_to_the_project_on_allocation(self):
        models.ProposalProjectRoleMapping.objects.create(
            call=self.fixture.call,
            proposal_role=ProposalRole.MANAGER,
        )
        self.assertFalse(self._allocate_and_check_membership(ProposalRole.MANAGER))
