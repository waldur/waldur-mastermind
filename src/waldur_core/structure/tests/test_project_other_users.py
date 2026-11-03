"""`GET /api/projects/{uuid}/other_users/` lists the users of the caller's
other projects in the same organization: the people the project team dialog
offers to add.

The project in the URL is found as `GET /api/projects/{uuid}/` finds it. A
personal access token sees the users of a project only if its bindings reach
that project, so it is never shown a user whom `list_users` on that user's
project would hide from it.
"""

import uuid

from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.tests.pat_utils import UNRELATED, use_pat
from waldur_core.structure import models
from waldur_core.structure.tests import factories


def other_users_url(project_uuid):
    return "http://testserver" + reverse(
        "project-other-users-list", kwargs={"project_uuid": project_uuid}
    )


@override_config(PAT_ENABLED=True)
class ProjectOtherUsersTest(test.APITestCase):
    def setUp(self):
        # Roles start bare in tests: grant what seeing a project and its team
        # takes, as the default roles do.
        for role in (ProjectRole.MANAGER, ProjectRole.MEMBER):
            role.add_permission(PermissionEnum.LIST_PROJECTS)
            role.add_permission(PermissionEnum.VIEW_PROJECT_TEAM)

        self.customer = factories.CustomerFactory()
        # The project in the URL, and another one the caller also manages.
        self.project = factories.ProjectFactory(customer=self.customer)
        self.sibling = factories.ProjectFactory(customer=self.customer)
        # A project of the same organization the caller has no role in.
        self.unrelated = factories.ProjectFactory(customer=self.customer)

        self.user = factories.UserFactory()
        self.project.add_user(self.user, ProjectRole.MANAGER)
        self.sibling.add_user(self.user, ProjectRole.MANAGER)

        self.colleague = factories.UserFactory()
        self.sibling.add_user(self.colleague, ProjectRole.MEMBER)
        self.newcomer = factories.UserFactory()
        self.project.add_user(self.newcomer, ProjectRole.MEMBER)
        self.stranger = factories.UserFactory()
        self.unrelated.add_user(self.stranger, ProjectRole.MEMBER)

        # Another organization in which the caller manages a project.
        self.other_customer = factories.CustomerFactory()
        self.other_project = factories.ProjectFactory(customer=self.other_customer)
        self.other_project.add_user(self.user, ProjectRole.MANAGER)
        self.outsider = factories.UserFactory()
        self.other_project.add_user(self.outsider, ProjectRole.MEMBER)

        self.client.force_authenticate(self.user)

    def get(self, project_uuid):
        return self.client.get(other_users_url(project_uuid))

    def listed(self, project=None):
        response = self.get((project or self.project).uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {item["uuid"] for item in response.data}

    def uuids(self, *users):
        return {user.uuid.hex for user in users}

    def team(self, project):
        """The users list_users shows the client for ``project``."""
        response = self.client.get(
            factories.ProjectFactory.get_url(project, action="list_users")
        )
        if response.status_code != status.HTTP_200_OK:
            return set()
        return {uuid.UUID(str(row["user_uuid"])).hex for row in response.data}

    def assert_not_found(self, project_uuid):
        response = self.get(project_uuid)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_lists_users_of_the_callers_other_projects(self):
        self.assertEqual(self.listed(), self.uuids(self.user, self.colleague))

    def test_unknown_project_is_not_found(self):
        self.assert_not_found(uuid.uuid4().hex)

    def test_malformed_project_uuid_is_not_found(self):
        self.assert_not_found("not-a-uuid")

    def test_removed_project_is_not_found(self):
        models.Project.all_objects.filter(pk=self.project.pk).update(is_removed=True)
        self.assert_not_found(self.project.uuid.hex)

    def test_project_the_caller_does_not_see_is_not_found(self):
        # It belongs to an organization the caller works in, but the caller
        # has no role on it or on the organization.
        self.assert_not_found(self.unrelated.uuid.hex)

    def test_unbound_token_lists_what_the_session_lists(self):
        use_pat(self.client, self.user, UNRELATED)
        self.assertEqual(self.listed(), self.uuids(self.user, self.colleague))

    def test_token_without_a_team_scope_is_served_as_list_users_serves_it(self):
        # Team reads are filtered by the user's roles and the token's
        # bindings, not by its permission scopes: list_users shows the same
        # token the sibling's team.
        use_pat(self.client, self.user, UNRELATED)
        self.assertEqual(self.listed(), self.team(self.sibling))

    def test_token_bound_to_the_organization_lists_its_projects_users(self):
        use_pat(self.client, self.user, UNRELATED, bindings=[self.customer])
        self.assertEqual(self.listed(), self.uuids(self.user, self.colleague))

    def test_token_bound_to_another_organization_does_not_find_the_project(self):
        use_pat(self.client, self.user, UNRELATED, bindings=[self.other_customer])
        self.assert_not_found(self.project.uuid.hex)

    def test_token_bound_to_another_project_does_not_find_the_project(self):
        use_pat(self.client, self.user, UNRELATED, bindings=[self.sibling])
        self.assert_not_found(self.project.uuid.hex)

    def test_token_bound_to_the_project_lists_no_users_of_other_projects(self):
        use_pat(self.client, self.user, UNRELATED, bindings=[self.project])
        self.assertEqual(self.listed(), set())

    def test_token_is_never_shown_a_user_list_users_hides_from_it(self):
        for bindings in ([], [self.customer], [self.project]):
            with self.subTest(bindings=bindings):
                use_pat(self.client, self.user, UNRELATED, bindings=bindings)
                visible = self.team(self.sibling) | self.team(self.unrelated)
                self.assertLessEqual(self.listed(), visible)
