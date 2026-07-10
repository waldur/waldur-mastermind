from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.tests import factories, fixtures


class ProposalListUsersPermissionTest(test.APITestCase):
    """The proposal-aware list_users override allows call managers.

    The generic ``UserRoleMixin.list_users`` rejects a user whose only role
    is ``CALL.MANAGER`` on the call (no role on the managing customer or
    its projects). ``ProposalViewSet.list_users`` extends that with a
    call-side check so the call manager can view the proposal team.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.url = factories.ProposalFactory.get_url(self.proposal, action="list_users")

    def test_staff_can_list_users(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_call_manager_can_list_users(self):
        # call_manager only holds CALL.MANAGER on the call — no role on the
        # managing customer or its projects. Before the override this 403'd.
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_call_reviewer_is_allowed(self):
        # Reviewers (and panel members) assigned to the call may view the
        # proposal team read-only, so the review interface renders and they can
        # comment on the team section.
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_outsider_cannot_see_proposal_at_all(self):
        # Unrelated user — the queryset filter hides the proposal entirely,
        # yielding 404 before the permission check fires.
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_extra_call_manager_added_via_role(self):
        # Sanity-check the path with an additional manager assigned at
        # runtime (not via the fixture's cached_property).
        manager = structure_factories.UserFactory()
        self.fixture.call.add_user(manager, CallRole.MANAGER)
        self.client.force_authenticate(manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
