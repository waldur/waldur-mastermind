from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.tests import factories, fixtures


class ProtectedCallListUsersPermissionTest(test.APITestCase):
    """The call "Team" tab lists the call team via
    ``proposal-protected-calls/<uuid>/list_users`` (``ProtectedCallViewSet``).

    A Call organizer holds ``CUSTOMER.CALL_ORGANIZER`` only on the managing
    organisation (``CallManagingOrganisation``, scope ``call_organizer``) — not
    on the customer or a project. The generic ``UserRoleMixin.list_users`` walks
    Call -> Customer -> Projects and never reaches that binding, so the organiser
    gets 403 on the Team tab of a call they can otherwise open for editing,
    while staff/owners pass. ``ProtectedCallViewSet`` (unlike ``ProposalViewSet``)
    does not override ``can_view_scope_team`` to add the call-organiser path.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = factories.CallFactory.get_protected_url(
            self.call, action="list_users"
        )

    def _organizer_bound_only_to_managing_organisation(self):
        # Faithful to the production grant flow: CallManagingOrganisation.add_user
        # binds CUSTOMER.CALL_ORGANIZER to the managing organisation only.
        user = structure_factories.UserFactory()
        self.fixture.manager.add_user(user, self.fixture.call_organizer_role)
        return user

    def test_staff_can_list_call_users(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_call_organizer_can_list_call_users(self):
        organizer = self._organizer_bound_only_to_managing_organisation()
        self.client.force_authenticate(organizer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_multiple_role_filter_returns_all_requested_roles(self):
        # The Team tab requests several roles at once as a repeated query param
        # (role=CALL.MANAGER&role=CALL.PANEL_MEMBER). list_users must honour all
        # of them; previously ``query_params.get("role")`` kept only the last
        # value, so call managers were dropped from the roster while still
        # appearing in the Permissions/events log (WAL-10149).
        manager = structure_factories.UserFactory()
        self.call.add_user(manager, CallRole.MANAGER)
        panel_member = structure_factories.UserFactory()
        self.call.add_user(panel_member, CallRole.PANEL_MEMBER)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"role": [CallRole.MANAGER.name, CallRole.PANEL_MEMBER.name]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        returned = {
            (str(entry["user_uuid"]), entry["role_name"]) for entry in response.data
        }
        self.assertIn((str(manager.uuid), CallRole.MANAGER.name), returned)
        self.assertIn((str(panel_member.uuid), CallRole.PANEL_MEMBER.name), returned)

    def test_single_role_filter_still_works(self):
        # A single role value must keep filtering to exactly that role — the
        # getlist change is a strict superset of the previous single-value path.
        manager = structure_factories.UserFactory()
        self.call.add_user(manager, CallRole.MANAGER)
        panel_member = structure_factories.UserFactory()
        self.call.add_user(panel_member, CallRole.PANEL_MEMBER)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"role": CallRole.MANAGER.name})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        returned_roles = {entry["role_name"] for entry in response.data}
        self.assertEqual(returned_roles, {CallRole.MANAGER.name})


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

    def test_creator_can_list_own_proposal_team(self):
        # The proposal author views their own team even without the
        # ProposalRole.MANAGER grant (e.g. preset/imported proposals, where
        # perform_create's role assignment never ran).
        creator = structure_factories.UserFactory()
        proposal = factories.ProposalFactory(
            round=self.fixture.round, created_by=creator
        )
        url = factories.ProposalFactory.get_url(proposal, action="list_users")
        self.client.force_authenticate(creator)
        response = self.client.get(url)
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
