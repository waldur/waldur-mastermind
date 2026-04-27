from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.get_project_resources import (
    GetProjectResourcesTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as mp_factories


class GetProjectResourcesToolTest(TestCase):
    def setUp(self):
        self.tool = GetProjectResourcesTool()
        self.fixture = ProjectFixture()
        self.offering = mp_factories.OfferingFactory(
            customer=self.fixture.customer, name="Test Offering"
        )
        self.resource_ok = mp_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            name="test-server-1",
            state=ResourceStates.OK,
            backend_id="vm-1234",
        )
        self.resource_terminated = mp_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            name="test-server-old",
            state=ResourceStates.TERMINATED,
        )
        # Resource in an unrelated project — user cannot see it.
        other_project = structure_factories.ProjectFactory()
        self.resource_other = mp_factories.ResourceFactory(
            project=other_project,
            offering=mp_factories.OfferingFactory(),
            name="other-resource",
            state=ResourceStates.OK,
        )

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.GET_PROJECT_RESOURCES, tool_registry)
        self.assertEqual(
            tool_registry.get(ToolName.GET_PROJECT_RESOURCES).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_requires_at_least_one_arg(self):
        result = self.tool.execute(self.fixture.member, {})
        self.assertEqual(result["type"], "validation_error")

    def test_project_scoped_listing_excludes_terminated(self):
        result = self.tool.execute(
            self.fixture.member,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        names = [r["name"] for r in result["data"]["resources"]]
        self.assertIn("test-server-1", names)
        self.assertNotIn("test-server-old", names)

    def test_filter_by_project_name(self):
        self.fixture.project.name = "Regular Access"
        self.fixture.project.save()
        result = self.tool.execute(
            self.fixture.member,
            {"project_name": "regular"},
        )
        names = [r["name"] for r in result["data"]["resources"]]
        self.assertIn("test-server-1", names)
        self.assertNotIn("test-server-old", names)

    def test_filter_by_project_name_not_found(self):
        result = self.tool.execute(
            self.fixture.member,
            {"project_name": "nonexistent-project"},
        )
        self.assertEqual(result["type"], "error")

    def test_cross_project_search_by_name(self):
        result = self.tool.execute(self.fixture.member, {"search": "test-server-1"})
        self.assertEqual(len(result["data"]["resources"]), 1)
        self.assertEqual(result["data"]["resources"][0]["name"], "test-server-1")

    def test_search_by_backend_id(self):
        result = self.tool.execute(self.fixture.member, {"search": "vm-1234"})
        self.assertEqual(len(result["data"]["resources"]), 1)

    def test_uuid_matches_exact(self):
        result = self.tool.execute(
            self.fixture.member, {"uuid": str(self.resource_ok.uuid)}
        )
        self.assertEqual(len(result["data"]["resources"]), 1)

    def test_other_users_resource_is_invisible(self):
        result = self.tool.execute(self.fixture.member, {"search": "other-resource"})
        self.assertEqual(result["data"]["resources"], [])

    def test_non_accessible_project_returns_not_found(self):
        other_project = structure_factories.ProjectFactory()
        result = self.tool.execute(
            self.fixture.member,
            {"project_uuid": str(other_project.uuid)},
        )
        self.assertEqual(result["type"], "error")
        self.assertIn("not found", result["summary"].lower())
