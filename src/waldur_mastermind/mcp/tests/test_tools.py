import asyncio
import json
import threading
import time
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import SimpleTestCase
from django.utils import timezone
from jsonschema import Draft202012Validator
from mcp import Client, types
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.shared.exceptions import MCPError
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core import middleware as core_middleware
from waldur_core.core.models import PersonalAccessToken, User
from waldur_core.logging import middleware as logging_middleware
from waldur_core.logging.models import Event, Feed
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.tools.base import BaseTool
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.executor import ToolExecutor
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import _ACCOUNT_TOOLS, _MARKETPLACE_TOOLS
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.mcp import auth, server, tools
from waldur_mastermind.mcp.tests.test_server import create_pat

CLIENT_IP = "203.0.113.5"
END_USER_TOOLS = {tool.value for tool in (*_ACCOUNT_TOOLS, *_MARKETPLACE_TOOLS)}
SUPPORT_TOOLS = {*END_USER_TOOLS, ToolName.GET_USER_OVERVIEW.value}


def access_token(user, scopes=None) -> AccessToken:
    """What the verifier hands the server for a fresh token of this user."""
    user.can_use_personal_access_tokens = True
    user.save(update_fields=["can_use_personal_access_tokens"])
    pat = create_pat(user, scopes)
    return auth.verify_personal_access_token(pat.plaintext, CLIENT_IP)


def as_caller(token, action):
    """Run ``action(client)`` on an in-memory client, which skips HTTP auth."""

    async def run():
        reset_token = auth_context_var.set(AuthenticatedUser(token))
        try:
            async with Client(server.build_mcp()) as client:
                return await action(client)
        finally:
            auth_context_var.reset(reset_token)

    return async_to_sync(run)()


def listed_names(token) -> list[str]:
    result = as_caller(token, lambda client: client.list_tools())
    return [tool.name for tool in result.tools]


def call(token, name, arguments=None) -> types.CallToolResult:
    return as_caller(token, lambda client: client.call_tool(name, arguments or {}))


def texts(result) -> list[str]:
    return [block.text for block in result.content]


class StubTool(BaseTool):
    """Stands in for a registered tool, recording what it ran with."""

    def __init__(self, name: ToolName, result=None, **definition_changes):
        self.wrapped = tool_registry.get(name)
        self.result = result or {"type": "success", "data": {}, "summary": "Done."}
        self.definition_changes = {"output_schema": {}, **definition_changes}
        self.calls = []

    @property
    def definition(self):
        return self.wrapped.definition.model_copy(update=self.definition_changes)

    def execute(self, user, arguments):
        self.calls.append(
            SimpleNamespace(
                user=user,
                arguments=arguments,
                event_context=dict(logging_middleware.get_event_context()),
                current_user=core_middleware.get_current_user(),
            )
        )
        return self.result

    def install(self, test_case):
        patcher = mock.patch.dict(tool_registry._tools, {self.name: self})
        patcher.start()
        test_case.addCleanup(patcher.stop)
        return self


class MCPToolsTestCase(test.APITestCase):
    def setUp(self):
        # PAT usage and audit debounces live in the cache.
        cache.clear()
        # The test transaction is only visible on this thread.
        patcher = mock.patch.object(auth, "ORM_THREAD_SENSITIVE", True)
        patcher.start()
        self.addCleanup(patcher.stop)


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class ToolRegistrationTest(MCPToolsTestCase):
    def setUp(self):
        super().setUp()
        self.user = structure_factories.UserFactory()

    def test_exposed_tools_are_listed_with_their_metadata(self):
        result = as_caller(access_token(self.user), lambda c: c.list_tools())

        self.assertLessEqual(len(result.tools), 25)
        for tool in result.tools:
            with self.subTest(tool=tool.name):
                definition = tool_registry.get(tool.name).definition
                self.assertEqual(tool.description, definition.description)
                self.assertEqual(tool.input_schema, definition.inputSchema)
                self.assertEqual(tool.output_schema, definition.output_schema)
                self.assertIsNotNone(tool.output_schema)
                self.assertTrue(tool.annotations.read_only_hint)
                Draft202012Validator.check_schema(tool.input_schema)


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class RoleTest(MCPToolsTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.other = marketplace_fixtures.MarketplaceFixture()
        self.sibling_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        self.invoice = invoice_factories.InvoiceFactory(customer=self.fixture.customer)
        self.other_invoice = invoice_factories.InvoiceFactory(
            customer=self.other.customer
        )
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)

    def callers(self):
        return {
            "organization member": (self.fixture.owner, None),
            "project member": (self.fixture.manager, None),
            "support": (self.support, [PermissionEnum.SUPPORT_ACCESS.value]),
            "staff": (self.staff, [PermissionEnum.STAFF_ACCESS.value]),
        }

    def rest_uuids(self, user, url, **params):
        self.client.force_authenticate(user)
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def assert_matches_rest(self, token, user, tool, key, url, **arguments):
        result = call(token, tool, arguments)
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(
            {item["uuid"] for item in result.structured_content[key]},
            self.rest_uuids(user, url, **arguments),
        )

    def test_tools_list_matches_the_role(self):
        expected = {
            "organization member": END_USER_TOOLS,
            "project member": END_USER_TOOLS,
            "support": SUPPORT_TOOLS,
            "staff": SUPPORT_TOOLS,
        }
        for role, (user, scopes) in self.callers().items():
            with self.subTest(role=role):
                names = listed_names(access_token(user, scopes))
                self.assertEqual(set(names), expected[role])

    def test_listing_tools_return_what_rest_returns(self):
        listings = [
            (
                "list_organizations",
                "organizations",
                structure_factories.CustomerFactory.get_list_url(),
            ),
            (
                "list_projects",
                "projects",
                structure_factories.ProjectFactory.get_list_url(),
            ),
            (
                "list_orders",
                "orders",
                marketplace_factories.OrderFactory.get_list_url(),
            ),
            (
                "list_invoices",
                "invoices",
                invoice_factories.InvoiceFactory.get_list_url(),
            ),
        ]
        for role, (user, scopes) in self.callers().items():
            token = access_token(user, scopes)
            for tool, key, url in listings:
                with self.subTest(role=role, tool=tool):
                    self.assert_matches_rest(token, user, tool, key, url)

    def uuids(self, token, tool, key):
        data = call(token, tool).structured_content
        return {item["uuid"] for item in data[key]}

    def test_organization_member_sees_only_their_organization(self):
        token = access_token(self.fixture.owner)

        self.assertEqual(
            self.uuids(token, "list_organizations", "organizations"),
            {self.fixture.customer.uuid.hex},
        )
        self.assertEqual(
            self.uuids(token, "list_projects", "projects"),
            {self.fixture.project.uuid.hex, self.sibling_project.uuid.hex},
        )
        self.assertEqual(
            self.uuids(token, "list_invoices", "invoices"), {self.invoice.uuid.hex}
        )

    def test_project_member_sees_only_their_project(self):
        token = access_token(self.fixture.manager)

        self.assertEqual(
            self.uuids(token, "list_projects", "projects"),
            {self.fixture.project.uuid.hex},
        )

    def test_staff_and_support_see_across_organizations(self):
        for role in ("support", "staff"):
            user, scopes = self.callers()[role]
            with self.subTest(role=role):
                seen = self.uuids(
                    access_token(user, scopes), "list_projects", "projects"
                )
                self.assertLessEqual(
                    {self.fixture.project.uuid.hex, self.other.project.uuid.hex}, seen
                )

    def test_objects_rest_hides_are_not_returned(self):
        lookups = [
            (
                "get_project",
                {"project_uuid": self.other.project.uuid.hex},
                structure_factories.ProjectFactory.get_url(self.other.project),
            ),
            (
                "get_resource",
                {"resource_uuid": self.other.resource.uuid.hex},
                marketplace_factories.ResourceFactory.get_url(self.other.resource),
            ),
            (
                "get_order",
                {"order_uuid": self.other.order.uuid.hex},
                marketplace_factories.OrderFactory.get_url(self.other.order),
            ),
        ]
        user = self.fixture.owner
        token = access_token(user)
        self.client.force_authenticate(user)
        for tool, arguments, url in lookups:
            with self.subTest(tool=tool):
                self.assertEqual(
                    self.client.get(url).status_code, status.HTTP_404_NOT_FOUND
                )
                result = call(token, tool, arguments)
                self.assertTrue(result.is_error)
                self.assertIsNone(result.structured_content)

    def test_every_tool_answers_within_its_output_schema(self):
        # The client checks each result against the tool's outputSchema.
        project = {"project_uuid": self.fixture.project.uuid.hex}
        resource = {"resource_uuid": self.fixture.resource.uuid.hex}
        offering = self.fixture.offering.uuid.hex
        arguments = {
            "explain_invoice_compensations": project,
            "explain_project_credit_balance": project,
            "explain_resource_paused_reason": resource,
            "get_customer_credit_overview": {
                "customer_uuid": self.fixture.customer.uuid.hex
            },
            "get_order": {"order_uuid": self.fixture.order.uuid.hex},
            "get_project": project,
            "get_project_quota": project,
            "get_project_resources": project,
            "get_resource": resource,
            "get_resource_usage": resource,
            "get_user_overview": {"user_email": self.fixture.owner.email},
            "compare_offerings": {"uuids": [offering, offering]},
            "get_offering": {"uuid": offering},
            "search_offerings": {"keyword": self.fixture.offering.name},
        }
        token = access_token(self.staff, [PermissionEnum.STAFF_ACCESS.value])

        for name in listed_names(token):
            with self.subTest(tool=name):
                result = call(token, name, arguments.get(name))
                self.assertFalse(result.is_error, result.content)


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class VisibilityTest(MCPToolsTestCase):
    def setUp(self):
        super().setUp()
        self.user = structure_factories.UserFactory()

    def assert_rejected_as_unknown(self, token, name):
        result = call(token, name)
        self.assertTrue(result.is_error)
        self.assertEqual(texts(result), [f"Unknown tool: {name!r}"])

    def test_tool_needing_a_permission_the_pat_lacks_is_hidden_and_rejected(self):
        tool = StubTool(
            ToolName.LIST_PROJECTS,
            required_pat_permissions=[PermissionEnum.LIST_ORDERS],
        ).install(self)
        token = access_token(self.user, [PermissionEnum.SUPPORT_ACCESS.value])

        self.assertNotIn("list_projects", listed_names(token))
        self.assert_rejected_as_unknown(token, "list_projects")
        self.assertEqual(tool.calls, [])

    def test_tool_is_served_to_a_pat_with_its_permission(self):
        tool = StubTool(
            ToolName.LIST_PROJECTS,
            required_pat_permissions=[PermissionEnum.LIST_ORDERS],
        ).install(self)
        token = access_token(self.user, [PermissionEnum.LIST_ORDERS.value])

        self.assertIn("list_projects", listed_names(token))
        self.assertFalse(call(token, "list_projects").is_error)
        self.assertEqual(len(tool.calls), 1)

    def test_tool_of_another_role_is_rejected(self):
        tool = StubTool(ToolName.GET_USER_OVERVIEW).install(self)

        self.assert_rejected_as_unknown(access_token(self.user), "get_user_overview")
        self.assertEqual(tool.calls, [])

    def test_tools_not_exposed_are_rejected_although_the_role_has_them(self):
        token = access_token(self.user)
        for name in (ToolName.CREATE_VM, ToolName.SEARCH_TOOLS, ToolName.ASK_USER):
            with self.subTest(tool=name.value):
                tool = StubTool(name).install(self)
                self.assert_rejected_as_unknown(token, name.value)
                self.assertEqual(tool.calls, [])

    def test_unknown_tool_is_rejected(self):
        self.assert_rejected_as_unknown(access_token(self.user), "no_such_tool")

    def test_tools_using_the_own_sign_in_are_never_served(self):
        StubTool(ToolName.LIST_HPC_JOBS, mcp_exposed=True).install(self)
        token = access_token(self.user)

        with mock.patch(
            "waldur_mastermind.chat.tools.tool_sets.workspace.is_configured",
            return_value=True,
        ):
            self.assertNotIn("list_hpc_jobs", listed_names(token))
            self.assert_rejected_as_unknown(token, "list_hpc_jobs")

    def test_rejected_call_is_not_audited(self):
        self.assert_rejected_as_unknown(access_token(self.user), "get_user_overview")

        self.assertFalse(Event.objects.filter(event_type="mcp_tool_called").exists())


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class ResultMappingTest(MCPToolsTestCase):
    def setUp(self):
        super().setUp()
        self.token = access_token(structure_factories.UserFactory())

    def call_with_result(self, result):
        StubTool(ToolName.LIST_PROJECTS, result=result).install(self)
        return call(self.token, "list_projects")

    def test_success_maps_data_and_summary(self):
        data = {"count": 2, "projects": ["a", "b"]}

        result = self.call_with_result(
            {
                "type": "success",
                "data": data,
                "summary": "Found 2 projects.",
                "ui_component": "project_table",
                "ui_data": {"secret": "for the chat only"},
            }
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, data)
        summary, encoded = texts(result)
        self.assertEqual(summary, "Found 2 projects.")
        self.assertEqual(json.loads(encoded), data)
        self.assertNotIn("for the chat only", result.model_dump_json())

    def test_data_is_json_encoded_as_the_output_schema_describes(self):
        result = self.call_with_result(
            {
                "type": "success",
                "data": {"cost": Decimal("1.50"), "day": date(2026, 11, 6)},
                "summary": "Done.",
            }
        )

        self.assertEqual(
            result.structured_content, {"cost": "1.50", "day": "2026-11-06"}
        )

    def test_data_outside_the_output_schema_is_an_error(self):
        StubTool(
            ToolName.LIST_PROJECTS,
            result={
                "type": "success",
                "data": {"owner": {"email": "private@example.org"}},
                "summary": "Found private@example.org.",
            },
            output_schema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "object",
                        "properties": {"email": {"type": "integer"}},
                    }
                },
            },
        ).install(self)

        with self.assertLogs("waldur_mastermind.mcp.tools", "ERROR") as logs:
            result = call(self.token, "list_projects")

        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        self.assertEqual(
            texts(result), ["The tool returned data in an unexpected format."]
        )
        self.assertIn("list_projects", logs.output[0])
        self.assertIn("$.owner.email", logs.output[0])
        self.assertNotIn("private@example.org", "".join(logs.output))

    def test_output_schema_is_checked_after_json_encoding(self):
        StubTool(
            ToolName.LIST_PROJECTS,
            result={
                "type": "success",
                "data": {"cost": Decimal("1.50")},
                "summary": "Done.",
            },
            output_schema={
                "type": "object",
                "properties": {"cost": {"type": "string"}},
            },
        ).install(self)

        result = call(self.token, "list_projects")

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {"cost": "1.50"})

    def test_real_tool_with_unexpected_data_gives_a_clean_error(self):
        fixture = marketplace_fixtures.MarketplaceFixture()
        resource = fixture.resource
        resource.limits = {"cpu": None}
        resource.save(update_fields=["limits"])
        token = access_token(fixture.owner)

        with self.assertLogs("waldur_mastermind.mcp.tools", "ERROR"):
            result = call(token, "get_resource", {"resource_uuid": resource.uuid.hex})

        self.assertTrue(result.is_error)
        self.assertEqual(
            texts(result), ["The tool returned data in an unexpected format."]
        )

    def test_numbers_json_cant_hold_are_an_error(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                result = self.call_with_result(
                    {"type": "success", "data": {"x": value}, "summary": "Done."}
                )

                self.assertTrue(result.is_error)
                self.assertIsNone(result.structured_content)

    def test_failures_map_to_is_error_with_the_summary(self):
        for result_type in ("error", "validation_error", "something_new"):
            with self.subTest(type=result_type):
                result = self.call_with_result(
                    {
                        "type": result_type,
                        "summary": "No such project.",
                        "data": {"ignored": True},
                    }
                )

                self.assertTrue(result.is_error)
                self.assertEqual(texts(result), ["No such project."])
                self.assertIsNone(result.structured_content)

    def test_tool_crash_is_a_generic_error(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)
        tool.execute = mock.Mock(side_effect=RuntimeError("password=hunter2"))

        result = call(self.token, "list_projects")

        self.assertTrue(result.is_error)
        self.assertNotIn("hunter2", result.model_dump_json())

    def test_arguments_reach_the_tool(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)

        call(self.token, "list_projects", {"search": "alpha"})

        self.assertEqual(tool.calls[0].arguments, {"search": "alpha"})

    def test_tool_runs_as_a_delegated_caller(self):
        with mock.patch.object(
            tools, "ToolExecutor", wraps=ToolExecutor
        ) as tool_executor:
            call(self.token, "list_categories")

        self.assertTrue(tool_executor.call_args.kwargs["delegated"])


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class ArgumentValidationTest(MCPToolsTestCase):
    def setUp(self):
        super().setUp()
        self.token = access_token(structure_factories.UserFactory())

    def assert_refused(self, tool_name, arguments, expected_text):
        tool = StubTool(tool_name).install(self)

        result = call(self.token, tool_name.value, arguments)

        self.assertTrue(result.is_error)
        self.assertIn(expected_text, texts(result)[0])
        self.assertEqual(tool.calls, [])

    def test_wrong_type_names_the_argument(self):
        self.assert_refused(ToolName.LIST_PROJECTS, {"uuid": 123}, "$.uuid")

    def test_missing_required_argument_is_named(self):
        self.assert_refused(ToolName.GET_ORDER, {}, "order_uuid")

    def test_value_out_of_range_is_refused(self):
        self.assert_refused(
            ToolName.SEARCH_OFFERINGS, {"keyword": "vm", "limit": -1}, "$.limit"
        )

    def test_nul_character_is_refused_at_any_depth(self):
        for arguments in (
            {"search": "a\x00b"},
            {"unknown": ["a\x00b"]},
            {"unknown": {"a\x00b": 1}},
        ):
            with self.subTest(arguments=arguments):
                self.assert_refused(ToolName.LIST_PROJECTS, arguments, "NUL")

    def test_unpaired_surrogate_is_refused(self):
        self.assert_refused(ToolName.LIST_PROJECTS, {"search": "a\ud800"}, "surrogate")

    def test_emoji_is_accepted(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)

        result = call(self.token, "list_projects", {"search": "café 🚀"})

        self.assertFalse(result.is_error)
        self.assertEqual(tool.calls[0].arguments, {"search": "café 🚀"})

    def direct_call(self, arguments):
        # The in-memory client can't send what these tests need to.
        return tools.call_tool(self.token, "list_projects", arguments, CLIENT_IP)

    def nested(self, depth):
        value = "x"
        for _ in range(depth):
            value = [value]
        return value

    def test_deep_nesting_is_refused_without_crashing(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)

        for key in ("search", "unknown"):
            with self.subTest(key=key):
                result = self.direct_call({key: self.nested(5000)})

                self.assertTrue(result.is_error)
                self.assertIn("nested too deeply", texts(result)[0])
        self.assertEqual(tool.calls, [])

    def test_arguments_over_the_size_limit_are_refused(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)
        half = "a" * (tools.MAX_ARGUMENT_CHARS // 2)

        for arguments in (
            {"search": "a" * (tools.MAX_ARGUMENT_CHARS + 1)},
            {"search": half, "uuid": half, "organization_name": "a"},
            {"unknown": [1] * (tools.MAX_ARGUMENT_CHARS + 1)},
            {"a" * (tools.MAX_ARGUMENT_CHARS + 1): 1},
        ):
            with self.subTest(size=len(json.dumps(arguments))):
                result = self.direct_call(arguments)

                self.assertTrue(result.is_error)
                self.assertIn("characters in total", texts(result)[0])
        self.assertEqual(tool.calls, [])

    def test_arguments_within_the_size_limit_are_accepted(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)

        result = self.direct_call({"search": "a" * (tools.MAX_ARGUMENT_CHARS - 10)})

        self.assertFalse(result.is_error)
        self.assertEqual(len(tool.calls), 1)

    def test_nesting_up_to_the_limit_is_left_to_the_schema(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)

        result = self.direct_call(
            {"unknown": self.nested(tools.MAX_ARGUMENT_DEPTH - 1)}
        )

        self.assertFalse(result.is_error)
        self.assertEqual(len(tool.calls), 1)

    def test_refused_value_is_not_echoed_in_full(self):
        result = call(self.token, "list_projects", {"uuid": ["a" * 3000]})

        self.assertLess(len(texts(result)[0]), 200)
        self.assertIn("$.uuid", texts(result)[0])
        self.assertIn("is not of type 'string'", texts(result)[0])

    def test_long_tool_name_is_not_echoed_in_full(self):
        result = call(self.token, "x" * 100_000)

        self.assertTrue(result.is_error)
        self.assertLess(len(texts(result)[0]), 200)

    def test_long_unknown_argument_name_is_not_echoed_in_full(self):
        # No exposed tool forbids extra keys yet, so this one is made to.
        schema = tool_registry.get(ToolName.LIST_PROJECTS).definition.inputSchema
        tool = StubTool(
            ToolName.LIST_PROJECTS,
            inputSchema={**schema, "additionalProperties": False},
        ).install(self)

        result = call(self.token, "list_projects", {"x" * 300: 1, "y": 2})

        self.assertTrue(result.is_error)
        text = texts(result)[0]
        self.assertIn("Additional properties are not allowed", text)
        self.assertIn(tools.shorten(repr("x" * 300)), text)
        self.assertNotIn("x" * tools.MAX_ECHO_CHARS, text)
        self.assertIn("'y'", text)
        self.assertEqual(tool.calls, [])

    def test_null_argument_counts_as_missing(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)

        result = call(self.token, "list_projects", {"search": "a", "uuid": None})

        self.assertFalse(result.is_error)
        self.assertEqual(tool.calls[0].arguments, {"search": "a"})

    def test_null_for_a_required_argument_is_refused(self):
        self.assert_refused(ToolName.GET_ORDER, {"order_uuid": None}, "order_uuid")

    def test_real_tool_gets_no_chance_to_crash(self):
        with self.assertNoLogs("waldur_mastermind.chat.tools.executor", "ERROR"):
            result = call(self.token, "list_projects", {"uuid": 123})

        self.assertIn("is not of type 'string'", texts(result)[0])

    def test_uuid_without_dashes_is_accepted(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)

        result = call(self.token, "list_projects", {"uuid": "a" * 32})

        self.assertFalse(result.is_error)
        self.assertEqual(len(tool.calls), 1)

    def test_refused_call_is_not_audited(self):
        call(self.token, "list_projects", {"uuid": 123})

        self.assertFalse(Event.objects.filter(event_type="mcp_tool_called").exists())


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class StaleTokenTest(MCPToolsTestCase):
    """The token stops being valid after the verifier has accepted it."""

    def setUp(self):
        super().setUp()
        self.user = structure_factories.UserFactory()
        self.token = access_token(self.user)
        self.pat = PersonalAccessToken.objects.get(uuid=self.token.claims["pat_uuid"])

    def assert_refused(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)

        result = call(self.token, "list_projects")

        self.assertTrue(result.is_error)
        self.assertEqual(texts(result), [server.INVALID_TOKEN])
        self.assertEqual(tool.calls, [])
        self.assertFalse(Event.objects.filter(event_type="mcp_tool_called").exists())

        async def list_tools():
            reset_token = auth_context_var.set(AuthenticatedUser(self.token))
            try:
                await server.list_tools(None, None)
            finally:
                auth_context_var.reset(reset_token)

        with self.assertRaises(MCPError) as caught:
            async_to_sync(list_tools)()
        self.assertEqual(caught.exception.code, types.INVALID_REQUEST)
        self.assertIn(server.INVALID_TOKEN, str(caught.exception))

    def test_revoked_token(self):
        self.pat.is_active = False
        self.pat.save(update_fields=["is_active"])

        self.assert_refused()

    def test_expired_token(self):
        self.pat.expires_at = timezone.now()
        self.pat.save(update_fields=["expires_at"])

        self.assert_refused()

    def test_deleted_token(self):
        self.pat.delete()

        self.assert_refused()

    def test_deactivated_user(self):
        # Saving would also revoke the token, leaving this check unexercised.
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        self.assert_refused()

    def test_user_no_longer_allowed_tokens(self):
        self.user.can_use_personal_access_tokens = False
        self.user.save(update_fields=["can_use_personal_access_tokens"])

        self.assert_refused()


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class CallerContextTest(MCPToolsTestCase):
    def setUp(self):
        super().setUp()
        self.tool = StubTool(ToolName.LIST_PROJECTS).install(self)
        self.first = structure_factories.UserFactory()
        self.second = structure_factories.UserFactory()

    def assert_context_is_clear(self):
        self.assertIsNone(logging_middleware.get_event_context())
        self.assertIsNone(core_middleware.get_current_user())

    def run_on_this_thread(self, token, client_ip=CLIENT_IP):
        # Under ORM_THREAD_SENSITIVE every call shares this thread, as calls
        # share a pool thread in production.
        return tools.call_tool(token, "list_projects", {}, client_ip)

    def test_tool_sees_the_caller_in_both_contexts(self):
        token = access_token(self.first)

        self.run_on_this_thread(token)

        (seen,) = self.tool.calls
        self.assertEqual(seen.current_user, self.first)
        self.assertEqual(seen.event_context["user_uuid"], self.first.uuid.hex)
        self.assertEqual(seen.event_context["auth_method"], "pat")
        self.assertEqual(seen.event_context["pat_uuid"], token.claims["pat_uuid"])
        self.assertEqual(seen.event_context["pat_name"], "mcp-pat")
        self.assertEqual(seen.event_context["ip_address"], CLIENT_IP)

    def test_consecutive_calls_on_one_thread_share_nothing(self):
        first_token = access_token(self.first)
        second_token = access_token(self.second)

        self.run_on_this_thread(first_token)
        self.assert_context_is_clear()
        self.run_on_this_thread(second_token, client_ip=None)
        self.assert_context_is_clear()

        first, second = self.tool.calls
        self.assertEqual(first.current_user, self.first)
        self.assertEqual(second.current_user, self.second)
        self.assertEqual(second.event_context["user_uuid"], self.second.uuid.hex)
        self.assertEqual(
            second.event_context["pat_uuid"], second_token.claims["pat_uuid"]
        )
        self.assertNotIn("ip_address", second.event_context)

    def test_context_left_on_the_thread_is_not_inherited(self):
        token = access_token(self.first)
        logging_middleware.set_event_context({"pat_name": "stale", "stale": True})
        self.addCleanup(logging_middleware.reset_event_context)

        self.run_on_this_thread(token)

        (seen,) = self.tool.calls
        self.assertEqual(seen.event_context["pat_name"], "mcp-pat")
        self.assertNotIn("stale", seen.event_context)

    def test_context_is_reset_when_the_call_fails(self):
        token = access_token(self.first)

        with mock.patch.object(tools, "emit_tool_called", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.run_on_this_thread(token)

        self.assert_context_is_clear()
        self.assertEqual(self.tool.calls, [])

    def test_unparseable_address_is_left_out(self):
        self.run_on_this_thread(access_token(self.first), client_ip="not an address")

        self.assertNotIn("ip_address", self.tool.calls[0].event_context)


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class AuditTest(MCPToolsTestCase):
    def setUp(self):
        super().setUp()
        self.user = structure_factories.UserFactory()

    def tool_events(self):
        return Event.objects.filter(event_type="mcp_tool_called")

    def test_tool_call_emits_audit_event(self):
        token = access_token(self.user)

        call(token, "list_projects", {"search": "private words"})

        event = self.tool_events().get()
        self.assertEqual(event.context["user_uuid"], self.user.uuid.hex)
        self.assertEqual(event.context["client"], "mcp-pat")
        self.assertEqual(event.context["tool_name"], "list_projects")
        self.assertEqual(event.context["server"], "/mcp")
        self.assertEqual(event.context["auth_method"], "pat")
        self.assertEqual(event.context["pat_uuid"], token.claims["pat_uuid"])
        self.assertIn(self.user.username, event.message)
        self.assertNotIn("private words", json.dumps(event.context))
        self.assertTrue(
            Feed.objects.filter(event=event, object_id=self.user.id).exists()
        )

    def test_failed_call_is_audited(self):
        StubTool(
            ToolName.LIST_PROJECTS,
            result={"type": "validation_error", "summary": "Nope."},
        ).install(self)

        call(access_token(self.user), "list_projects")

        self.assertEqual(self.tool_events().count(), 1)

    def test_audit_comes_before_the_tool(self):
        tool = StubTool(ToolName.LIST_PROJECTS).install(self)

        with mock.patch.object(tools, "emit_tool_called", side_effect=RuntimeError):
            result = call(access_token(self.user), "list_projects")

        self.assertTrue(result.is_error)
        self.assertEqual(tool.calls, [])

    def test_braces_in_the_token_name_are_harmless(self):
        token = access_token(self.user)
        PersonalAccessToken.objects.filter(uuid=token.claims["pat_uuid"]).update(
            name="{user_uuid} {unknown}"
        )

        result = call(token, "list_categories")

        self.assertFalse(result.is_error, result.content)
        self.assertIn("{user_uuid} {unknown}", self.tool_events().get().message)


TOKEN = AccessToken(
    token="w_test",
    client_id="pat:test",
    scopes=[],
    claims={"pat_uuid": "0" * 32},
)
CALL_PARAMS = SimpleNamespace(name="list_projects", arguments={})


async def with_token(awaitable_factory):
    reset_token = auth_context_var.set(AuthenticatedUser(TOKEN))
    try:
        return await awaitable_factory()
    finally:
        auth_context_var.reset(reset_token)


class HandlerTest(SimpleTestCase):
    def test_call_without_a_token_is_refused(self):
        with mock.patch.object(tools, "call_tool") as call_tool:
            with self.assertRaises(MCPError):
                async_to_sync(server.call_tool)(None, CALL_PARAMS)

        call_tool.assert_not_called()

    def test_listing_without_a_token_is_refused(self):
        with mock.patch.object(tools, "list_tools") as list_tools:
            with self.assertRaises(MCPError):
                async_to_sync(server.list_tools)(None, None)

        list_tools.assert_not_called()

    def test_call_failure_does_not_reach_the_client(self):
        with mock.patch.object(
            tools, "call_tool", side_effect=RuntimeError("password=hunter2")
        ):
            result = async_to_sync(with_token)(
                lambda: server.call_tool(None, CALL_PARAMS)
            )

        self.assertTrue(result.is_error)
        self.assertNotIn("hunter2", result.model_dump_json())

    def test_listing_failure_does_not_reach_the_client(self):
        with mock.patch.object(
            tools, "list_tools", side_effect=RuntimeError("password=hunter2")
        ):
            with self.assertRaises(MCPError) as caught:
                async_to_sync(with_token)(lambda: server.list_tools(None, None))

        self.assertNotIn("hunter2", str(caught.exception))
        self.assertEqual(caught.exception.code, types.INTERNAL_ERROR)

    def test_concurrent_calls_stay_within_the_orm_pool(self):
        lock = threading.Lock()
        running = peak = 0
        threads = set()

        def slow_call(*args):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
                threads.add(threading.current_thread())
            time.sleep(0.05)
            with lock:
                running -= 1
            return tools.error_result("Done.")

        async def call_many():
            await asyncio.gather(
                *[server.call_tool(None, CALL_PARAMS) for _ in range(8)]
            )

        auth.configure_orm_threads(2)
        self.addCleanup(auth.configure_orm_threads, auth.DEFAULT_ORM_THREADS)

        with mock.patch.object(tools, "call_tool", slow_call):
            async_to_sync(with_token)(call_many)

        self.assertEqual(peak, 2)
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(t.name.startswith("mcp-orm") for t in threads))
