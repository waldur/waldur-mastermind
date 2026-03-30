from django.test import TestCase

from waldur_mastermind.chat.prompts.assembly import SYSTEM_PROMPT_TEMPLATE
from waldur_mastermind.chat.prompts.persona import PERSONA_TEMPLATE
from waldur_mastermind.chat.prompts.ui_capabilities import UI_CAPABILITIES
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.registry import tool_registry


class ToolDefinitionTest(TestCase):
    def test_tool_definition_fields(self):
        definition = ToolDefinition(
            name="test_tool",
            description="A test tool",
            inputSchema={"type": "object", "properties": {}},
        )

        self.assertEqual(definition.name, "test_tool")
        self.assertEqual(definition.description, "A test tool")
        self.assertEqual(definition.inputSchema, {"type": "object", "properties": {}})

    def test_tool_definition_prompt_fields_default_empty(self):
        definition = ToolDefinition(
            name="test_tool",
            description="A test tool",
            inputSchema={"type": "object", "properties": {}},
        )

        self.assertEqual(definition.usage_instructions, "")
        self.assertEqual(definition.workflow_instructions, "")


class ToolRegistryTest(TestCase):
    def test_show_user_resources_is_registered(self):
        self.assertIn("show_user_resources", tool_registry)

    def test_show_user_resources_has_correct_definition(self):
        tool = tool_registry.get("show_user_resources")

        self.assertEqual(tool.name, "show_user_resources")
        self.assertIn("cloud resources", tool.definition.description.lower())
        self.assertEqual(tool.definition.inputSchema["type"], "object")
        self.assertEqual(tool.definition.inputSchema["properties"], {})
        self.assertEqual(tool.definition.inputSchema["required"], [])

    def test_show_user_resources_has_usage_instructions(self):
        tool = tool_registry.get("show_user_resources")
        self.assertNotEqual(tool.definition.usage_instructions, "")


class GetToolsPromptTest(TestCase):
    def test_returns_string(self):
        prompt = tool_registry.get_tools_prompt()
        self.assertIsInstance(prompt, str)

    def test_includes_tool_names(self):
        # Tool names are now passed via the API tools param (get_openai_tools()), not in the prompt
        openai_tools = tool_registry.get_openai_tools()
        tool_names = [t["function"]["name"] for t in openai_tools]
        self.assertIn("show_user_resources", tool_names)

    def test_includes_tool_descriptions(self):
        # Tool descriptions are passed via the API tools param (get_openai_tools()), not in the prompt
        openai_tools = tool_registry.get_openai_tools()
        show_user_resources_tool = next(
            t for t in openai_tools if t["function"]["name"] == "show_user_resources"
        )
        self.assertTrue(len(show_user_resources_tool["function"]["description"]) > 0)

    def test_includes_usage_instructions(self):
        prompt = tool_registry.get_tools_prompt()
        tool = tool_registry.get("show_user_resources")
        self.assertIn(tool.definition.usage_instructions, prompt)

    def test_tool_with_parameters_shows_parameter_info(self):
        # Parameters are now in get_openai_tools(), not in the prompt
        class _TempTool(BaseTool):
            @property
            def definition(self):
                return ToolDefinition(
                    name="test_tool_with_params",
                    description="Test tool with parameters",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "param1": {
                                "type": "string",
                                "description": "First parameter",
                            },
                            "param2": {
                                "type": "integer",
                                "description": "Second parameter",
                            },
                        },
                        "required": ["param1"],
                    },
                )

            def execute(self, user, arguments):
                return {}

        tool_registry.register(_TempTool())
        try:
            openai_tools = tool_registry.get_openai_tools()
            temp_tool = next(
                t
                for t in openai_tools
                if t["function"]["name"] == "test_tool_with_params"
            )
            params = temp_tool["function"]["parameters"]
            self.assertIn("param1", params["properties"])
            self.assertEqual(params["properties"]["param1"]["type"], "string")
            self.assertIn("param2", params["properties"])
            self.assertEqual(params["properties"]["param2"]["type"], "integer")
            self.assertIn("param1", params["required"])
        finally:
            tool_registry._tools.pop("test_tool_with_params", None)

    def test_tool_without_parameters_shows_no_parameters(self):
        # show_user_resources has no parameters — verify via get_openai_tools()
        openai_tools = tool_registry.get_openai_tools()
        show_user_resources_tool = next(
            t for t in openai_tools if t["function"]["name"] == "show_user_resources"
        )
        params = show_user_resources_tool["function"]["parameters"]
        self.assertEqual(params.get("properties", {}), {})

    def test_prompt_has_available_tools_section(self):
        # AVAILABLE TOOLS section was moved to API tools param; it should NOT be in the prompt
        prompt = tool_registry.get_tools_prompt()
        self.assertNotIn("AVAILABLE TOOLS", prompt)

    def test_prompt_has_usage_guidelines_section(self):
        prompt = tool_registry.get_tools_prompt()
        self.assertIn("TOOL USAGE GUIDELINES", prompt)


class UICapabilitiesTest(TestCase):
    def test_ui_capabilities_is_string(self):
        self.assertIsInstance(UI_CAPABILITIES, str)

    def test_includes_mermaid_capabilities(self):
        self.assertIn("mermaid", UI_CAPABILITIES.lower())
        self.assertIn("```mermaid", UI_CAPABILITIES)

    def test_includes_code_capabilities(self):
        self.assertIn("code", UI_CAPABILITIES.lower())
        self.assertIn("```python", UI_CAPABILITIES)

    def test_includes_table_capabilities(self):
        self.assertIn("table", UI_CAPABILITIES.lower())


class SystemPromptTest(TestCase):
    def test_system_prompt_template_is_string(self):
        self.assertIsInstance(SYSTEM_PROMPT_TEMPLATE, str)

    def test_system_prompt_template_has_persona(self):
        self.assertIn(PERSONA_TEMPLATE, SYSTEM_PROMPT_TEMPLATE)

    def test_system_prompt_template_has_placeholders(self):
        self.assertIn("{tools}", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("{assistant_name}", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("{organization}", SYSTEM_PROMPT_TEMPLATE)

    def test_system_prompt_template_formats_correctly(self):
        result = SYSTEM_PROMPT_TEMPLATE.format(
            tools="[tool prompt]",
            assistant_name="TestBot",
            organization="TestOrg",
        )
        self.assertIn("TestBot", result)
        self.assertIn("TestOrg", result)
        self.assertIn("[tool prompt]", result)
        self.assertNotIn("{assistant_name}", result)
        self.assertNotIn("{organization}", result)
        self.assertNotIn("{tools}", result)
