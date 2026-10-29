from uuid import UUID

from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure.tests.fixtures import CustomerFixture, ProjectFixture
from waldur_mastermind.chat.tools.account.list_orders import ListOrdersTool
from waldur_mastermind.chat.tools.base import MAX_LIST_RESULTS
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


class ListOrdersToolTest(APITestCase):
    def setUp(self):
        self.tool = ListOrdersTool()
        self.fixture = ProjectFixture()
        self.provider = CustomerFixture()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.provider.customer
        )
        self.erred = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            state=OrderStates.ERRED,
            error_message="Backend unreachable",
        )
        self.done = marketplace_factories.OrderFactory(
            project=self.fixture.project, state=OrderStates.DONE
        )
        self.other = marketplace_factories.OrderFactory(state=OrderStates.DONE)

    def _rest_uuids(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(
            marketplace_factories.OrderFactory.get_list_url(),
            {"page_size": 100, **params},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {UUID(str(o["uuid"])).hex for o in response.data}

    def _tool_uuids(self, user, **arguments):
        result = self.tool.execute(user, arguments)
        self.assertEqual(result["type"], "success")
        return {UUID(o["uuid"]).hex for o in result["data"]["orders"]}

    def test_registered_in_account_category_and_role_sets(self):
        self.assertEqual(
            tool_registry.get(ToolName.LIST_ORDERS).definition.category,
            ToolCategory.ACCOUNT,
        )
        for tool_set in (END_USER_TOOLS, SUPPORT_TOOLS, STAFF_TOOLS):
            self.assertIn(ToolName.LIST_ORDERS, tool_set)

    def test_visible_orders_match_rest_list(self):
        users = {role: getattr(self.fixture, role) for role in ROLES}
        users["offering owner"] = self.provider.owner
        for role, user in users.items():
            with self.subTest(role=role):
                self.assertEqual(self._tool_uuids(user), self._rest_uuids(user))

    def test_unrelated_user_sees_no_orders(self):
        self.assertEqual(self._tool_uuids(self.fixture.user), set())

    def test_empty_result_notes_role_limited_visibility(self):
        result = self.tool.execute(self.fixture.user, {})
        self.assertIn("your role lets you see", result["summary"])
        result = self.tool.execute(self.fixture.staff, {"state": ["canceled"]})
        self.assertEqual(result["summary"], "Found 0 orders.")

    def test_offering_owner_sees_orders_for_their_offering(self):
        self.assertEqual(self._tool_uuids(self.provider.owner), {self.erred.uuid.hex})

    def test_filters_match_rest(self):
        user = self.fixture.staff
        self.fixture.project.name = "Bluewave Ledger"
        self.fixture.project.save()
        cases = [
            (
                {"project_uuid": str(self.fixture.project.uuid)},
                {"project_uuid": self.fixture.project.uuid.hex},
            ),
            (
                {"resource_uuid": str(self.erred.resource.uuid)},
                {"resource_uuid": self.erred.resource.uuid.hex},
            ),
            ({"state": ["erred"]}, {"state": "erred"}),
            (
                {"project_name": "bluewave"},
                {"project_uuid": self.fixture.project.uuid.hex},
            ),
        ]
        for arguments, params in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    self._tool_uuids(user, **arguments),
                    self._rest_uuids(user, **params),
                )

    def test_offering_filters_match_rest(self):
        self.offering.name = "Bluewave Compute"
        self.offering.save()
        for user in (self.provider.owner, self.fixture.owner, self.fixture.staff):
            rest = self._rest_uuids(user, offering_uuid=self.offering.uuid.hex)
            with self.subTest(user=user.username, filter="offering_uuid"):
                self.assertEqual(
                    self._tool_uuids(user, offering_uuid=str(self.offering.uuid)), rest
                )
            with self.subTest(user=user.username, filter="offering_name"):
                self.assertEqual(self._tool_uuids(user, offering_name="bluewave"), rest)

    def test_provider_finds_orders_by_offering_name(self):
        self.offering.name = "Bluewave Compute"
        self.offering.save()
        self.assertEqual(
            self._tool_uuids(self.provider.owner, offering_name="Bluewave"),
            {self.erred.uuid.hex},
        )

    def test_customer_filters_match_rest(self):
        self.fixture.customer.name = "Bluewave Org"
        self.fixture.customer.save()
        user = self.fixture.staff
        rest = self._rest_uuids(user, customer_uuid=self.fixture.customer.uuid.hex)
        self.assertEqual(len(rest), 2)
        self.assertEqual(
            self._tool_uuids(user, customer_uuid=str(self.fixture.customer.uuid)),
            rest,
        )
        self.assertEqual(self._tool_uuids(user, customer_name="bluewave"), rest)

    def test_row_carries_organization_and_rejection_comment(self):
        rejected = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=OrderStates.REJECTED,
            consumer_rejection_comment="Budget not approved",
        )
        rows = self.tool.execute(self.fixture.owner, {"state": ["rejected"]})["data"][
            "orders"
        ]
        row = next(r for r in rows if r["uuid"] == str(rejected.uuid))
        self.assertEqual(row["consumer_rejection_comment"], "Budget not approved")
        self.assertEqual(row["organization_uuid"], str(self.fixture.customer.uuid))

    def test_row_shape(self):
        result = self.tool.execute(self.fixture.owner, {"state": ["erred"]})
        row = result["data"]["orders"][0]
        self.assertEqual(row["uuid"], str(self.erred.uuid))
        self.assertEqual(row["state"], "erred")
        self.assertEqual(row["type"], "Create")
        self.assertEqual(row["error_message"], "Backend unreachable")

    def test_pending_covers_every_pending_state(self):
        waiting = marketplace_factories.OrderFactory(
            project=self.fixture.project, state=OrderStates.PENDING_PROVIDER
        )
        pending = self._tool_uuids(self.fixture.owner, state=["pending"])
        self.assertIn(waiting.uuid.hex, pending)
        self.assertEqual(
            pending,
            self._tool_uuids(
                self.fixture.owner,
                state=[
                    "pending-consumer",
                    "pending-provider",
                    "pending-project",
                    "pending-start-date",
                ],
            ),
        )

    def test_unknown_state_is_rejected(self):
        result = self.tool.execute(self.fixture.staff, {"state": ["failed"]})
        self.assertEqual(result["type"], "validation_error")

    def test_non_string_state_is_rejected(self):
        for state in ([{"state": "erred"}], 4):
            with self.subTest(state=state):
                result = self.tool.execute(self.fixture.staff, {"state": state})
                self.assertEqual(result["type"], "validation_error")

    def test_single_state_string_is_accepted(self):
        self.assertEqual(
            self._tool_uuids(self.fixture.staff, state="erred"),
            {self.erred.uuid.hex},
        )

    def test_invalid_uuid_is_rejected(self):
        result = self.tool.execute(self.fixture.staff, {"project_uuid": "x"})
        self.assertEqual(result["type"], "validation_error")

    def test_results_are_capped(self):
        for _ in range(MAX_LIST_RESULTS):
            marketplace_factories.OrderFactory(project=self.fixture.project)
        data = self.tool.execute(
            self.fixture.owner, {"project_uuid": str(self.fixture.project.uuid)}
        )["data"]
        self.assertEqual(len(data["orders"]), MAX_LIST_RESULTS)
        self.assertEqual(data["total"], MAX_LIST_RESULTS + 2)
        self.assertEqual(data["_total_count"], MAX_LIST_RESULTS + 2)
        self.assertTrue(data["_truncated"])
