from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase
from jsonschema import Draft202012Validator

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.tools.arguments import (
    MAX_ECHO_CHARS,
    argument_error,
    shorten,
)
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.executor import ToolExecutor
from waldur_mastermind.chat.tools.proposals_researcher.guide_proposal import (
    GuideProposalTool,
)
from waldur_mastermind.chat.tools.proposals_researcher.list_calls import (
    ListCallsTool,
)
from waldur_mastermind.chat.tools.proposals_researcher.list_proposals import (
    ListProposalsTool,
)
from waldur_mastermind.chat.tools.proposals_researcher.proposal_overview import (
    ProposalOverviewTool,
)
from waldur_mastermind.chat.tools.proposals_reviewer.call_insights import (
    CallInsightsTool,
)
from waldur_mastermind.chat.tools.proposals_reviewer.review_assistant import (
    ReviewAssistantTool,
)
from waldur_mastermind.chat.tools.registry import tool_registry

WRONG_TYPE = {
    "string": 12345,
    "integer": "1",
    "number": "1",
    "boolean": "true",
    "array": "x",
    "object": ["x"],
}
LONG = "x" * 10_000


def out_of_schema_values(schema: dict):
    """Values of the right type that the property's schema still refuses."""
    if "enum" in schema:
        yield "not-a-choice"
    if "items" in schema and "enum" in schema["items"]:
        yield ["not-a-choice"]
    if "minimum" in schema:
        yield schema["minimum"] - 1
    if "maximum" in schema:
        yield schema["maximum"] + 1
    if "maxLength" in schema:
        yield "a" * (schema["maxLength"] + 1)
    if schema.get("minItems"):
        yield []


class EveryToolTest(TestCase):
    """Every registered tool gets its arguments checked before it runs."""

    def setUp(self):
        self.executor = ToolExecutor(structure_factories.UserFactory(is_staff=True))
        # Which tools a role may use is not under test here.
        self.executor.permitted = frozenset(tool_registry.definitions)

    def run_refused(self, name: ToolName, arguments) -> dict:
        tool = tool_registry.get(name)
        with mock.patch.object(tool, "execute") as execute:
            result = self.executor.execute_tool(name.value, arguments)
        execute.assert_not_called()
        self.assertEqual(result["type"], "validation_error", result)
        return result

    def properties(self):
        for name, definition in tool_registry.definitions.items():
            for argument, schema in definition.inputSchema["properties"].items():
                yield name, argument, schema

    def test_registry_is_not_empty(self):
        self.assertGreater(len(tool_registry.definitions), 40)

    def test_schemas_are_valid(self):
        for name, definition in tool_registry.definitions.items():
            with self.subTest(tool=name.value):
                Draft202012Validator.check_schema(definition.inputSchema)

    def test_wrong_type_is_refused(self):
        for name, argument, schema in self.properties():
            with self.subTest(tool=name.value, argument=argument):
                self.run_refused(name, {argument: WRONG_TYPE[schema["type"]]})

    def test_out_of_schema_value_is_refused(self):
        for name, argument, schema in self.properties():
            for value in out_of_schema_values(schema):
                with self.subTest(tool=name.value, argument=argument, value=value):
                    self.run_refused(name, {argument: value})

    def test_nul_character_is_refused(self):
        for name, argument, schema in self.properties():
            if schema["type"] != "string":
                continue
            with self.subTest(tool=name.value, argument=argument):
                result = self.run_refused(name, {argument: "a\x00b"})
                self.assertIn("NUL", result["summary"])

    def test_unknown_argument_is_refused(self):
        for name in tool_registry.definitions:
            with self.subTest(tool=name.value):
                result = self.run_refused(name, {"no_such_argument": "x"})
                self.assertIn("Unknown argument: no_such_argument.", result["summary"])

    def test_missing_required_argument_is_refused(self):
        for name, definition in tool_registry.definitions.items():
            required = definition.inputSchema.get("required")
            if not required:
                continue
            with self.subTest(tool=name.value):
                result = self.run_refused(name, {})
                self.assertIn(repr(required[0]), result["summary"])

    def test_non_object_is_refused(self):
        for name in tool_registry.definitions:
            for arguments in (["x"], "x", 1):
                with self.subTest(tool=name.value, arguments=arguments):
                    result = self.run_refused(name, arguments)
                    self.assertIn("expected an object", result["summary"])

    def test_summary_echoes_a_bounded_length(self):
        for name, argument, schema in self.properties():
            # A list of text is valid for an array, so it gets one level more.
            wrong = [[LONG]] if schema["type"] == "array" else [LONG]
            cases = [{LONG: 1}, {argument: wrong}, {LONG + "a": 1, LONG + "b": 2}]
            if "enum" in schema:
                cases.append({argument: LONG})
            for arguments in cases:
                with self.subTest(tool=name.value, argument=argument):
                    result = self.run_refused(name, arguments)
                    self.assertNotIn("x" * (MAX_ECHO_CHARS + 1), result["summary"])

    def test_many_unknown_arguments_are_counted_not_listed(self):
        arguments = {f"bogus_{i}": 1 for i in range(1000)}

        result = self.run_refused(ToolName.LIST_PROJECTS, arguments)

        self.assertIn("and 995 more", result["summary"])
        self.assertLess(len(result["summary"]), 400)

    def test_null_counts_as_missing(self):
        tool = tool_registry.get(ToolName.LIST_PROJECTS)
        with mock.patch.object(tool, "execute", return_value={}) as execute:
            self.executor.execute_tool("list_projects", {"search": "a", "uuid": None})

        execute.assert_called_once_with(self.executor.user, {"search": "a"})

    def test_nested_null_counts_as_missing(self):
        result = self.executor.execute_tool(
            "ask_user",
            {
                "questions": [
                    {
                        "question": "Which workload?",
                        "header": None,
                        "options": [{"label": "A", "value": None}, {"label": "B"}],
                    }
                ]
            },
        )

        self.assertEqual(result["type"], "success", result)
        self.assertEqual(result["ui_component"], "ask_user_form")

    def test_null_in_a_list_is_refused(self):
        result = self.run_refused(ToolName.LIST_ORDERS, {"state": [None]})

        self.assertIn("$.state[0]", result["summary"])

    def test_deep_nesting_is_refused_without_crashing(self):
        value = "x"
        for _ in range(5000):
            value = [{"a": value}]

        result = self.run_refused(ToolName.LIST_PROJECTS, {"search": value})

        self.assertIn("nested too deeply", result["summary"])

    def test_non_object_never_reaches_the_input_guard(self):
        with mock.patch(
            "waldur_mastermind.chat.tools.executor.get_detection_service"
        ) as guard:
            self.run_refused(ToolName.LIST_PROJECTS, ["x"])

        guard.assert_not_called()


class ToolSummaryEchoTest(TestCase):
    """A tool's own validation summary repeats only part of a refused value."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)

    def assert_cut(self, tool, arguments):
        result = tool.execute(self.staff, arguments)
        self.assertEqual(result["type"], "validation_error", result)
        self.assertIn("x...", result["summary"])
        self.assertNotIn("x" * (MAX_ECHO_CHARS + 1), result["summary"])

    def test_invalid_uuid_is_cut(self):
        for tool, argument in (
            (ReviewAssistantTool(), "uuid"),
            (CallInsightsTool(), "uuid"),
            (GuideProposalTool(), "uuid"),
            (ProposalOverviewTool(), "uuid"),
            (ListProposalsTool(), "call_uuid"),
            (ListCallsTool(), "manager_uuid"),
        ):
            with self.subTest(tool=tool.name.value):
                self.assert_cut(tool, {argument: LONG})

    def test_invalid_choice_is_cut(self):
        for tool, arguments in (
            (ListCallsTool(), {"state": LONG}),
            (ListCallsTool(), {"round_status": LONG}),
            (ListProposalsTool(), {"state": [LONG]}),
        ):
            with self.subTest(tool=tool.name.value, arguments=list(arguments)):
                self.assert_cut(tool, arguments)


class SchemaMessageEchoTest(SimpleTestCase):
    """A schema message that names refused keys repeats only part of each."""

    def test_long_nested_key_is_cut(self):
        definition = SimpleNamespace(
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "additionalProperties": False,
                    }
                },
            }
        )

        error = argument_error(definition, {"filter": {LONG: 1, "y": 2}})

        self.assertIn("Additional properties are not allowed", error)
        self.assertIn(shorten(repr(LONG)), error)
        self.assertNotIn("x" * (MAX_ECHO_CHARS + 1), error)
        self.assertIn("'y'", error)
