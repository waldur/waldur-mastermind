from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import CustomerFixture, ProjectFixture
from waldur_mastermind.chat.tools.account.get_order import GetOrderTool
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    END_USER_TOOLS,
    STAFF_TOOLS,
    SUPPORT_TOOLS,
)
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

ROLES = ("owner", "member", "user", "global_support", "staff")


class GetOrderToolTest(APITestCase):
    def setUp(self):
        self.tool = GetOrderTool()
        self.fixture = ProjectFixture()
        self.provider = CustomerFixture()
        self.reviewer = structure_factories.UserFactory()
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            offering=marketplace_factories.OfferingFactory(
                customer=self.provider.customer
            ),
            state=OrderStates.ERRED,
            error_message="Backend unreachable",
            consumer_reviewed_by=self.reviewer,
            consumer_reviewed_at=timezone.now(),
        )

    def _execute(self, user):
        return self.tool.execute(user, {"order_uuid": str(self.order.uuid)})

    def test_registered_in_account_category_and_role_sets(self):
        self.assertEqual(
            tool_registry.get(ToolName.GET_ORDER).definition.category,
            ToolCategory.ACCOUNT,
        )
        for tool_set in (END_USER_TOOLS, SUPPORT_TOOLS, STAFF_TOOLS):
            self.assertIn(ToolName.GET_ORDER, tool_set)

    def test_visibility_matches_rest_retrieve(self):
        users = {role: getattr(self.fixture, role) for role in ROLES}
        users["offering owner"] = self.provider.owner
        for role, user in users.items():
            with self.subTest(role=role):
                self.client.force_authenticate(user)
                response = self.client.get(
                    marketplace_factories.OrderFactory.get_url(self.order)
                )
                rest_visible = response.status_code == status.HTTP_200_OK
                self.assertEqual(self._execute(user)["type"] == "success", rest_visible)

    def test_unrelated_user_cannot_see_order(self):
        result = self._execute(self.fixture.user)
        self.assertEqual(result["type"], "error")
        self.assertEqual(result["summary"], "Order not found or not visible to you.")

    def test_details(self):
        result = self._execute(self.fixture.member)
        self.assertEqual(result["type"], "success")
        data = result["data"]
        self.assertEqual(data["order"]["state"], "erred")
        self.assertEqual(data["order"]["error_message"], "Backend unreachable")
        self.assertIsNotNone(data["order"]["consumer_reviewed_at"])
        self.assertEqual(data["created_by"]["uuid"], str(self.order.created_by.uuid))
        self.assertEqual(data["consumer_reviewed_by"]["uuid"], str(self.reviewer.uuid))
        self.assertIsNone(data["provider_reviewed_by"])
        self.assertEqual(data["resource"]["uuid"], str(self.order.resource.uuid))
        self.assertIn("Backend unreachable", result["summary"])

    def test_cost_matches_rest_format(self):
        for cost in (Decimal("0"), Decimal("12.345")):
            with self.subTest(cost=cost):
                self.order.cost = cost
                self.order.save(update_fields=["cost"])
                self.client.force_authenticate(self.fixture.owner)
                rest = self.client.get(
                    marketplace_factories.OrderFactory.get_url(self.order)
                ).data
                result = self._execute(self.fixture.owner)
                self.assertEqual(result["data"]["order"]["cost"], rest["cost"])

    def test_requires_valid_uuid(self):
        for arguments in ({}, {"order_uuid": "nope"}):
            with self.subTest(arguments=arguments):
                result = self.tool.execute(self.fixture.staff, arguments)
                self.assertEqual(result["type"], "validation_error")
