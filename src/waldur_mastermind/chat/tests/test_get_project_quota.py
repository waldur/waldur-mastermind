from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.get_project_quota import (
    GetProjectQuotaTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry


class GetProjectQuotaToolTest(TestCase):
    def setUp(self):
        self.tool = GetProjectQuotaTool()
        self.fixture = ProjectFixture()
        self.fixture.project.set_quota_limit("nc_resource_count", 100)
        self.fixture.project.add_quota_usage("nc_resource_count", 12)

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.GET_PROJECT_QUOTA, tool_registry)
        self.assertEqual(
            tool_registry.get(ToolName.GET_PROJECT_QUOTA).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_requires_uuid_or_name(self):
        result = self.tool.execute(self.fixture.member, {})
        self.assertEqual(result["type"], "validation_error")

    def test_returns_quota_rows_for_accessible_project(self):
        result = self.tool.execute(
            self.fixture.member,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        self.assertEqual(result["type"], "success")
        quotas = result["data"]["quotas"]
        names = [q["name"] for q in quotas]
        self.assertIn("nc_resource_count", names)
        row = next(q for q in quotas if q["name"] == "nc_resource_count")
        self.assertEqual(row["limit"], 100)
        self.assertEqual(row["usage"], 12)

    def test_name_fallback(self):
        self.fixture.project.name = "Regular Access"
        self.fixture.project.save()
        result = self.tool.execute(
            self.fixture.member,
            {"project_name": "regular"},
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["project"]["name"], "Regular Access")

    def test_unauthorized_project_returns_not_found(self):
        other = structure_factories.ProjectFactory()
        result = self.tool.execute(
            self.fixture.member, {"project_uuid": str(other.uuid)}
        )
        self.assertEqual(result["type"], "error")
        self.assertIn("not found", result["summary"].lower())

    def test_invalid_uuid_returns_validation_error(self):
        result = self.tool.execute(self.fixture.member, {"project_uuid": "not-a-uuid"})
        self.assertEqual(result["type"], "validation_error")
