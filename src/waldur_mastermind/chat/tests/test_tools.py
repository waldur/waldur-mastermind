from django.test import TestCase
from pydantic import ValidationError

from waldur_mastermind.chat.prompts.assembly import SYSTEM_PROMPT_TEMPLATE
from waldur_mastermind.chat.prompts.persona import PERSONA_TEMPLATE
from waldur_mastermind.chat.prompts.ui_capabilities import UI_CAPABILITIES
from waldur_mastermind.chat.tools.base import ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    END_USER_TOOLS,
    STAFF_TOOLS,
    SUPPORT_TOOLS,
)


class ToolDefinitionTest(TestCase):
    def test_tool_definition_fields(self):
        definition = ToolDefinition(
            name=ToolName.SHOW_USER_RESOURCES,
            description="A test tool",
            inputSchema={"type": "object", "properties": {}},
        )

        self.assertEqual(definition.name, ToolName.SHOW_USER_RESOURCES)
        self.assertEqual(definition.description, "A test tool")
        self.assertEqual(definition.inputSchema, {"type": "object", "properties": {}})

    def test_tool_definition_prompt_fields_default_empty(self):
        definition = ToolDefinition(
            name=ToolName.SHOW_USER_RESOURCES,
            description="A test tool",
            inputSchema={"type": "object", "properties": {}},
        )

        self.assertEqual(definition.usage_instructions, "")
        self.assertEqual(definition.workflow_instructions, "")

    def test_tool_definition_rejects_unknown_name(self):
        with self.assertRaises(ValidationError):
            ToolDefinition(
                name="bogus",
                description="x",
                inputSchema={"type": "object", "properties": {}},
            )


class ToolNameEnumTest(TestCase):
    def test_invalid_value_raises(self):
        with self.assertRaises(ValueError):
            ToolName("bogus")


class ToolRegistryTest(TestCase):
    def test_show_user_resources_is_registered(self):
        self.assertIn(ToolName.SHOW_USER_RESOURCES, tool_registry)

    def test_registry_accepts_raw_string_lookup(self):
        # The LLM API returns plain strings — get/__contains__ normalise them.
        self.assertIn("show_user_resources", tool_registry)
        self.assertIsNotNone(tool_registry.get("show_user_resources"))

    def test_registry_returns_none_for_unknown_string(self):
        self.assertIsNone(tool_registry.get("bogus"))
        self.assertNotIn("bogus", tool_registry)

    def test_show_user_resources_has_correct_definition(self):
        tool = tool_registry.get(ToolName.SHOW_USER_RESOURCES)

        self.assertEqual(tool.name, ToolName.SHOW_USER_RESOURCES)
        self.assertIn("cloud resources", tool.definition.description.lower())
        self.assertEqual(tool.definition.inputSchema["type"], "object")
        self.assertEqual(tool.definition.inputSchema["properties"], {})
        self.assertEqual(tool.definition.inputSchema["required"], [])

    def test_show_user_resources_has_usage_instructions(self):
        tool = tool_registry.get(ToolName.SHOW_USER_RESOURCES)
        self.assertNotEqual(tool.definition.usage_instructions, "")

    def test_get_openai_tools_no_filter_returns_all(self):
        all_tools = tool_registry.get_openai_tools()
        filtered = tool_registry.get_openai_tools(tool_names=None)
        self.assertEqual(len(all_tools), len(filtered))

    def test_get_openai_tools_with_tool_names_filter(self):
        tools = tool_registry.get_openai_tools(
            tool_names=[ToolName.SHOW_USER_RESOURCES]
        )
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["function"]["name"], "show_user_resources")

    def test_get_tools_prompt_with_tool_names(self):
        prompt = tool_registry.get_tools_prompt(
            tool_names=[ToolName.SHOW_USER_RESOURCES]
        )
        self.assertIn("TOOL USAGE GUIDELINES", prompt)


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
        tool = tool_registry.get(ToolName.SHOW_USER_RESOURCES)
        self.assertIn(tool.definition.usage_instructions, prompt)

    def test_tool_with_parameters_shows_parameter_info(self):
        # create_vm has a rich inputSchema — verify get_openai_tools() emits it.
        openai_tools = tool_registry.get_openai_tools()
        create_vm_tool = next(
            t for t in openai_tools if t["function"]["name"] == "create_vm"
        )
        params = create_vm_tool["function"]["parameters"]
        self.assertIn("project_uuid", params["properties"])
        self.assertEqual(params["properties"]["project_uuid"]["type"], "string")
        self.assertIn("name", params["properties"])
        self.assertIn("project_uuid", params["required"])

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


class ToolSetsTest(TestCase):
    def test_tool_sets_hierarchy(self):
        self.assertTrue(set(END_USER_TOOLS) <= set(SUPPORT_TOOLS))
        self.assertTrue(set(SUPPORT_TOOLS) <= set(STAFF_TOOLS))

    def test_tool_sets_all_exist_in_registry(self):
        all_names = set(STAFF_TOOLS) | set(SUPPORT_TOOLS) | set(END_USER_TOOLS)
        for name in all_names:
            self.assertIn(name, tool_registry, f"Tool {name!r} not found in registry")

    def test_tool_sets_contain_enum_members(self):
        for tool_set in (STAFF_TOOLS, SUPPORT_TOOLS, END_USER_TOOLS):
            self.assertTrue(all(isinstance(t, ToolName) for t in tool_set))
