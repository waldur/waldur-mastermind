from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.get_user_overview import (
    GetUserOverviewTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    END_USER_TOOLS,
    STAFF_TOOLS,
    SUPPORT_TOOLS,
)
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories as mp_factories


class GetUserOverviewToolTest(TestCase):
    def setUp(self):
        self.tool = GetUserOverviewTool()
        self.fixture = ProjectFixture()
        self.target = self.fixture.member
        self.staff = structure_factories.UserFactory(is_staff=True)

        self.offering = mp_factories.OfferingFactory(customer=self.fixture.customer)
        self.ok_resource = mp_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            state=ResourceStates.OK,
        )
        self.erred_resource = mp_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            state=ResourceStates.ERRED,
            error_message="boom",
        )
        self.pending_order = mp_factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            created_by=self.target,
            state=OrderStates.PENDING_CONSUMER,
            resource=self.ok_resource,
        )

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.GET_USER_OVERVIEW, tool_registry)
        self.assertEqual(
            tool_registry.get(ToolName.GET_USER_OVERVIEW).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_role_scoping(self):
        self.assertIn(ToolName.GET_USER_OVERVIEW, STAFF_TOOLS)
        self.assertIn(ToolName.GET_USER_OVERVIEW, SUPPORT_TOOLS)
        self.assertNotIn(ToolName.GET_USER_OVERVIEW, END_USER_TOOLS)

    def test_requires_user_email(self):
        result = self.tool.execute(self.staff, {})
        self.assertEqual(result["type"], "validation_error")

    def test_bundle_payload_shape(self):
        result = self.tool.execute(self.staff, {"user_email": self.target.email})
        self.assertEqual(result["type"], "success")
        data = result["data"]
        self.assertEqual(data["user"]["email"], self.target.email)
        self.assertGreaterEqual(len(data["projects"]), 1)
        self.assertIn(
            self.fixture.customer.name,
            [o["name"] for o in data["organizations"]],
        )
        self.assertEqual(data["resources"]["total"], 2)
        self.assertEqual(data["resources"]["by_state"].get("Erred"), 1)
        self.assertTrue(
            any(
                e["name"] == self.erred_resource.name
                for e in data["resources"]["erred"]
            )
        )
        self.assertGreaterEqual(len(data["pending_orders"]), 1)

    def test_unknown_email_returns_not_found(self):
        result = self.tool.execute(
            self.staff, {"user_email": "no-such-user@example.com"}
        )
        self.assertEqual(result["type"], "error")
        self.assertIn("not found", result["summary"].lower())

    def test_capped_erred_resources(self):
        for _ in range(12):
            mp_factories.ResourceFactory(
                project=self.fixture.project,
                offering=self.offering,
                state=ResourceStates.ERRED,
                error_message="boom",
            )
        result = self.tool.execute(self.staff, {"user_email": self.target.email})
        self.assertLessEqual(len(result["data"]["resources"]["erred"]), 10)
