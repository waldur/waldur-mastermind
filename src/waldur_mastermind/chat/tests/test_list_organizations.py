from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.list_organizations import (
    ListOrganizationsTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry


class ListOrganizationsToolTest(TestCase):
    def setUp(self):
        self.tool = ListOrganizationsTool()
        self.fixture = ProjectFixture()
        # Noise: a customer the user has no role on.
        self.unrelated = structure_factories.CustomerFactory(name="UNRELATED")

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.LIST_ORGANIZATIONS, tool_registry)
        self.assertEqual(
            tool_registry.get(ToolName.LIST_ORGANIZATIONS).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_owner_sees_their_customer(self):
        result = self.tool.execute(self.fixture.owner, {})
        names = [o["name"] for o in result["data"]["organizations"]]
        self.assertIn(self.fixture.customer.name, names)
        self.assertNotIn(self.unrelated.name, names)

    def test_stranger_sees_nothing(self):
        stranger = structure_factories.UserFactory()
        result = self.tool.execute(stranger, {})
        self.assertEqual(result["data"]["organizations"], [])
        self.assertEqual(result["data"]["total"], 0)

    def test_search_matches_name_icontains(self):
        self.fixture.customer.name = "LUMI UT"
        self.fixture.customer.save()
        result = self.tool.execute(self.fixture.owner, {"search": "lumi"})
        self.assertEqual(len(result["data"]["organizations"]), 1)
        self.assertEqual(result["data"]["organizations"][0]["name"], "LUMI UT")

    def test_uuid_matches_exact(self):
        result = self.tool.execute(
            self.fixture.owner, {"uuid": str(self.fixture.customer.uuid)}
        )
        self.assertEqual(len(result["data"]["organizations"]), 1)

    def test_invalid_uuid_returns_validation_error(self):
        result = self.tool.execute(self.fixture.owner, {"uuid": "not-a-uuid"})
        self.assertEqual(result["type"], "validation_error")

    def test_role_surfaced_on_direct_membership(self):
        result = self.tool.execute(self.fixture.owner, {})
        org = next(
            o
            for o in result["data"]["organizations"]
            if o["uuid"] == str(self.fixture.customer.uuid)
        )
        self.assertEqual(org["role"], "CUSTOMER.OWNER")

    def test_role_null_when_access_only_via_project(self):
        # fixture.member has PROJECT role only, no CUSTOMER role — but still
        # sees the customer through project membership.
        result = self.tool.execute(self.fixture.member, {})
        names = [o["name"] for o in result["data"]["organizations"]]
        self.assertIn(self.fixture.customer.name, names)
        org = next(
            o
            for o in result["data"]["organizations"]
            if o["name"] == self.fixture.customer.name
        )
        self.assertIsNone(org["role"])
