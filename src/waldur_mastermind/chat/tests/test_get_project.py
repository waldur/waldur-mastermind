from uuid import UUID

from django.contrib.contenttypes.models import ContentType
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import add_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.get_project import GetProjectTool
from waldur_mastermind.chat.tools.account.list_projects import ListProjectsTool
from waldur_mastermind.chat.tools.base import MAX_LIST_RESULTS
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    END_USER_TOOLS,
    STAFF_TOOLS,
    SUPPORT_TOOLS,
)
from waldur_mastermind.marketplace.models import ResourceProject
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

ROLES = ("owner", "member", "user", "global_support", "staff")


class GetProjectToolTest(APITestCase):
    def setUp(self):
        self.tool = GetProjectTool()
        self.fixture = ProjectFixture()
        self.project = self.fixture.project
        # Populate the team before any role queries.
        self.fixture.admin
        self.fixture.manager
        self.fixture.member

    def _rest_get(self, user, action=None):
        self.client.force_authenticate(user)
        return self.client.get(
            structure_factories.ProjectFactory.get_url(self.project, action=action)
        )

    def _execute(self, user, **arguments):
        return self.tool.execute(
            user, {"project_uuid": str(self.project.uuid), **arguments}
        )

    def test_registered_in_account_category_and_role_sets(self):
        self.assertEqual(
            tool_registry.get(ToolName.GET_PROJECT).definition.category,
            ToolCategory.ACCOUNT,
        )
        for tool_set in (END_USER_TOOLS, SUPPORT_TOOLS, STAFF_TOOLS):
            self.assertIn(ToolName.GET_PROJECT, tool_set)

    def test_requires_uuid_or_name(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "validation_error")

    def test_visibility_matches_rest_retrieve(self):
        for role in ROLES:
            with self.subTest(role=role):
                user = getattr(self.fixture, role)
                rest_visible = self._rest_get(user).status_code == status.HTTP_200_OK
                result = self._execute(user)
                self.assertEqual(result["type"] == "success", rest_visible)

    def test_unrelated_user_cannot_see_project(self):
        self.assertEqual(self._execute(self.fixture.user)["type"], "error")

    def test_team_matches_rest_list_users(self):
        for role in ROLES:
            with self.subTest(role=role):
                user = getattr(self.fixture, role)
                result = self._execute(user)
                if result["type"] != "success":
                    continue
                response = self._rest_get(user, action="list_users")
                team = result["data"]["team"]
                if response.status_code == status.HTTP_403_FORBIDDEN:
                    self.assertIsNone(team)
                    continue
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    {(UUID(m["user_uuid"]).hex, m["role"]) for m in team["members"]},
                    {
                        (UUID(str(r["user_uuid"])).hex, r["role_name"])
                        for r in response.data
                    },
                )

    def test_caller_roles_match_list_tools(self):
        cases = (
            ("member", ProjectRole.MEMBER.name, None),
            ("manager", ProjectRole.MANAGER.name, None),
            ("owner", None, CustomerRole.OWNER.name),
            ("staff", None, None),
        )
        for name, project_role, organization_role in cases:
            with self.subTest(user=name):
                user = getattr(self.fixture, name)
                result = self._execute(user)
                data = result["data"]
                self.assertEqual(data["project"]["role"], project_role)
                self.assertEqual(data["organization"]["role"], organization_role)
                listed = ListProjectsTool().execute(
                    user, {"uuid": str(self.project.uuid)}
                )["data"]["projects"][0]
                self.assertEqual(data["project"]["role"], listed["role"])
                if project_role:
                    self.assertIn(f"Your role: {project_role}.", result["summary"])
                if organization_role:
                    self.assertIn(
                        f"Your organization role: {organization_role}.",
                        result["summary"],
                    )

    def test_caller_is_marked_in_team(self):
        members = self._execute(self.fixture.member)["data"]["team"]["members"]
        mine = [m["user_uuid"] for m in members if m["is_you"]]
        self.assertEqual(mine, [self.fixture.member.uuid.hex])
        owner_view = self._execute(self.fixture.owner)["data"]["team"]["members"]
        self.assertFalse(any(m["is_you"] for m in owner_view))

    def test_hidden_team_is_marked_restricted(self):
        resource = marketplace_factories.ResourceFactory(project=self.project)
        resource_project = ResourceProject.objects.create(
            resource=resource, name="Project A"
        )
        viewer = structure_factories.UserFactory()
        rp_role = Role.objects.create(
            name="RP",
            content_type=ContentType.objects.get_for_model(ResourceProject),
            is_system_role=False,
        )
        add_user(resource_project, viewer, rp_role)
        self.assertEqual(
            self._rest_get(viewer, action="list_users").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        result = self._execute(viewer)
        self.assertIsNone(result["data"]["team"])
        self.assertEqual(result["data"]["restricted_fields"], ["team"])
        self.assertIn("may or may not have members", result["summary"])
        visible = self._execute(self.fixture.owner)["data"]
        self.assertEqual(visible["restricted_fields"], [])

    def test_team_lists_roles(self):
        members = self._execute(self.fixture.owner)["data"]["team"]["members"]
        self.assertIn(
            (self.fixture.member.uuid.hex, ProjectRole.MEMBER.name),
            {(m["user_uuid"], m["role"]) for m in members},
        )

    def test_team_is_capped(self):
        for _ in range(MAX_LIST_RESULTS):
            self.project.add_user(structure_factories.UserFactory(), ProjectRole.MEMBER)
        team = self._execute(self.fixture.staff)["data"]["team"]
        self.assertEqual(len(team["members"]), MAX_LIST_RESULTS)
        self.assertEqual(team["_total_count"], MAX_LIST_RESULTS + 3)
        self.assertTrue(team["_truncated"])

    def test_details(self):
        data = self._execute(self.fixture.member)["data"]
        self.assertEqual(data["project"]["uuid"], str(self.project.uuid))
        self.assertFalse(data["project"]["is_terminated"])
        self.assertEqual(data["organization"]["uuid"], str(self.fixture.customer.uuid))
        self.assertEqual(data["matches"], [])

    def test_terminated_project_found_by_uuid_but_not_by_name(self):
        self.project.delete()
        self.assertEqual(self._execute(self.fixture.staff)["type"], "success")
        result = self.tool.execute(
            self.fixture.staff, {"project_name": self.project.name}
        )
        self.assertEqual(result["type"], "error")

    def test_exact_name_wins_over_partial_matches(self):
        structure_factories.ProjectFactory(
            customer=self.fixture.customer, name=self.project.name + " two"
        )
        result = self.tool.execute(
            self.fixture.owner, {"project_name": self.project.name.upper()}
        )
        self.assertEqual(result["data"]["project"]["uuid"], str(self.project.uuid))

    def test_several_name_matches_are_listed(self):
        self.project.name = "Lab Alpha"
        self.project.save()
        structure_factories.ProjectFactory(
            customer=self.fixture.customer, name="Lab Beta"
        )
        result = self.tool.execute(self.fixture.owner, {"project_name": "lab"})
        data = result["data"]
        self.assertIsNone(data["project"])
        self.assertEqual(len(data["matches"]), 2)
        self.assertEqual(data["_total_count"], 2)
