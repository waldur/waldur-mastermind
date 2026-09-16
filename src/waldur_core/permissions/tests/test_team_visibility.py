"""Team listing (``list_users``) is gated on a view-team permission.

A role on an organization needs ``CUSTOMER.VIEW_TEAM`` and a role on one of
its projects needs ``PROJECT.VIEW_TEAM`` before its holder may enumerate the
organization's or project's members. Holding *some* role is no longer enough.
"""

from pathlib import Path

import yaml
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase
from rest_framework import status, test

from waldur_core.permissions.enums import SYSTEM_ROLE_SCOPES, PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import Role
from waldur_core.permissions.serializers import clone_role_for_customer
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories
from waldur_core.structure.tests.utils import client_list_users

PERMISSIONS_YAML = Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"


class TeamVisibilityTest(test.APITestCase):
    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.user = factories.UserFactory()
        # Project visibility itself (the queryset behind list_users) is
        # governed by PROJECT.LIST, which is not what these tests are about.
        for role in (
            CustomerRole.OWNER,
            CustomerRole.SUPPORT,
            CustomerRole.READER,
            ProjectRole.ADMIN,
            ProjectRole.MANAGER,
            ProjectRole.MEMBER,
        ):
            role.add_permission(PermissionEnum.LIST_PROJECTS)

    def assert_team_visible(self, *scopes):
        for scope in scopes:
            response = client_list_users(self.client, self.user, scope)
            self.assertEqual(response.status_code, status.HTTP_200_OK, scope)

    def assert_team_hidden(self, *scopes):
        for scope in scopes:
            response = client_list_users(self.client, self.user, scope)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, scope)

    # System roles keep today's visibility.

    def test_customer_owner_sees_organization_and_project_teams(self):
        self.customer.add_user(self.user, CustomerRole.OWNER)
        self.assert_team_visible(self.customer, self.project)

    def test_customer_support_sees_organization_and_project_teams(self):
        self.customer.add_user(self.user, CustomerRole.SUPPORT)
        self.assert_team_visible(self.customer, self.project)

    def test_customer_reader_sees_organization_and_project_teams(self):
        self.customer.add_user(self.user, CustomerRole.READER)
        self.assert_team_visible(self.customer, self.project)

    def test_project_roles_see_organization_and_project_teams(self):
        for role in (ProjectRole.ADMIN, ProjectRole.MANAGER, ProjectRole.MEMBER):
            with self.subTest(role=role.name):
                user = factories.UserFactory()
                self.project.add_user(user, role)
                self.user = user
                self.assert_team_visible(self.customer, self.project)

    # A role without the permission hides the team.

    def test_customer_role_without_permission_gets_403(self):
        CustomerRole.READER.delete_permission(PermissionEnum.VIEW_CUSTOMER_TEAM)
        self.customer.add_user(self.user, CustomerRole.READER)
        self.assert_team_hidden(self.customer, self.project)

    def test_project_role_without_permission_gets_403(self):
        ProjectRole.MEMBER.delete_permission(PermissionEnum.VIEW_PROJECT_TEAM)
        self.project.add_user(self.user, ProjectRole.MEMBER)
        self.assert_team_hidden(self.customer, self.project)

    def test_permission_must_match_the_scope_type_of_the_role(self):
        # PROJECT.VIEW_TEAM on a customer-scoped role does not stand in for
        # CUSTOMER.VIEW_TEAM, and vice versa.
        CustomerRole.READER.delete_permission(PermissionEnum.VIEW_CUSTOMER_TEAM)
        CustomerRole.READER.add_permission(PermissionEnum.VIEW_PROJECT_TEAM)
        ProjectRole.MEMBER.delete_permission(PermissionEnum.VIEW_PROJECT_TEAM)
        ProjectRole.MEMBER.add_permission(PermissionEnum.VIEW_CUSTOMER_TEAM)
        self.customer.add_user(self.user, CustomerRole.READER)
        self.project.add_user(self.user, ProjectRole.MEMBER)
        self.assert_team_hidden(self.customer, self.project)

    def test_custom_customer_role_without_permission_gets_403(self):
        # A placeholder-style custom role: it may see the projects, but not
        # who is in them.
        placeholder = Role.objects.create(
            name="CUSTOMER.PLACEHOLDER",
            content_type=ContentType.objects.get_for_model(Customer),
        )
        placeholder.add_permission(PermissionEnum.LIST_PROJECTS)
        self.customer.add_user(self.user, placeholder)
        self.assert_team_hidden(self.customer, self.project)

    def test_permission_on_one_role_is_enough(self):
        CustomerRole.READER.delete_permission(PermissionEnum.VIEW_CUSTOMER_TEAM)
        self.customer.add_user(self.user, CustomerRole.READER)
        self.project.add_user(self.user, ProjectRole.MEMBER)
        self.assert_team_visible(self.customer, self.project)

    def test_inactive_role_does_not_grant_visibility(self):
        self.customer.add_user(self.user, CustomerRole.OWNER)
        self.customer.remove_user(self.user, CustomerRole.OWNER)
        response = client_list_users(self.client, self.user, self.customer)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    # Cloned roles inherit the permission from their template.

    def test_clone_of_role_with_permission_sees_team(self):
        clone = clone_role_for_customer(CustomerRole.READER, self.customer)
        self.assertTrue(
            clone.permissions.filter(
                permission=PermissionEnum.VIEW_CUSTOMER_TEAM
            ).exists()
        )
        self.customer.add_user(self.user, clone)
        self.assert_team_visible(self.customer, self.project)

    def test_clone_of_role_without_permission_does_not_see_team(self):
        CustomerRole.READER.delete_permission(PermissionEnum.VIEW_CUSTOMER_TEAM)
        clone = clone_role_for_customer(CustomerRole.READER, self.customer)
        self.customer.add_user(self.user, clone)
        self.assert_team_hidden(self.customer, self.project)

    def test_project_role_clone_inherits_permission(self):
        clone = clone_role_for_customer(ProjectRole.MEMBER, self.customer)
        self.project.add_user(self.user, clone)
        self.assert_team_visible(self.customer, self.project)

    # Staff and support are unaffected.

    def test_staff_and_support_see_any_team(self):
        for user in (
            factories.UserFactory(is_staff=True),
            factories.UserFactory(is_support=True),
        ):
            self.user = user
            self.assert_team_visible(self.customer, self.project)


class PermissionsYamlTeamVisibilityTest(SimpleTestCase):
    """Upgrading keeps team visibility for every customer and project role."""

    def test_every_customer_and_project_system_role_can_view_the_team(self):
        roles = yaml.safe_load(PERMISSIONS_YAML.read_text())
        expected = {
            "customer": PermissionEnum.VIEW_CUSTOMER_TEAM.value,
            "project": PermissionEnum.VIEW_PROJECT_TEAM.value,
        }
        checked = set()
        for role in roles:
            permission = expected.get(role["scope"])
            if permission is None:
                continue
            checked.add(role["role"])
            self.assertIn(permission, role["permissions"], role["role"])
        system_roles = {
            name for name, (_, model) in SYSTEM_ROLE_SCOPES.items() if model in expected
        }
        self.assertEqual(checked, system_roles)
