from django.test import TestCase

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.list_projects import ListProjectsTool
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry


class ListProjectsToolTest(TestCase):
    def setUp(self):
        self.tool = ListProjectsTool()
        self.fixture = ProjectFixture()
        self.other_customer = structure_factories.CustomerFactory(name="OTHER")
        self.other_project = structure_factories.ProjectFactory(
            customer=self.other_customer, name="other-project"
        )

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.LIST_PROJECTS, tool_registry)
        self.assertEqual(
            tool_registry.get(ToolName.LIST_PROJECTS).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_returns_only_user_accessible_projects(self):
        result = self.tool.execute(self.fixture.member, {})
        names = [p["name"] for p in result["data"]["projects"]]
        self.assertIn(self.fixture.project.name, names)
        self.assertNotIn(self.other_project.name, names)

    def test_filter_by_organization_uuid(self):
        # Give the member ownership of a second customer too, so we can
        # verify the organization_uuid filter actually scopes results.
        second_customer = structure_factories.CustomerFactory()
        second_customer.add_user(self.fixture.member, CustomerRole.OWNER)

        result = self.tool.execute(
            self.fixture.member,
            {"organization_uuid": str(self.fixture.customer.uuid)},
        )
        uuids = [p["organization_uuid"] for p in result["data"]["projects"]]
        self.assertTrue(uuids)
        self.assertTrue(all(u == str(self.fixture.customer.uuid) for u in uuids))

    def test_search_by_name(self):
        self.fixture.project.name = "Regular Access"
        self.fixture.project.save()
        result = self.tool.execute(self.fixture.owner, {"search": "regular"})
        self.assertEqual(len(result["data"]["projects"]), 1)

    def test_uuid_matches_exact(self):
        result = self.tool.execute(
            self.fixture.owner, {"uuid": str(self.fixture.project.uuid)}
        )
        self.assertEqual(len(result["data"]["projects"]), 1)

    def test_filter_by_organization_name(self):
        # Rename the fixture's customer so we can filter by partial name.
        self.fixture.customer.name = "LUMI Regular"
        self.fixture.customer.save()
        second_customer = structure_factories.CustomerFactory(name="OTHER ORG")
        second_customer.add_user(self.fixture.member, CustomerRole.OWNER)

        result = self.tool.execute(
            self.fixture.member,
            {"organization_name": "lumi"},
        )
        uuids = [p["organization_uuid"] for p in result["data"]["projects"]]
        self.assertTrue(uuids)
        self.assertTrue(all(u == str(self.fixture.customer.uuid) for u in uuids))

    def test_invalid_uuid_returns_validation_error(self):
        result = self.tool.execute(
            self.fixture.owner,
            {"organization_uuid": "not-a-uuid"},
        )
        self.assertEqual(result["type"], "validation_error")

        result = self.tool.execute(
            self.fixture.owner,
            {"uuid": "not-a-uuid"},
        )
        self.assertEqual(result["type"], "validation_error")
