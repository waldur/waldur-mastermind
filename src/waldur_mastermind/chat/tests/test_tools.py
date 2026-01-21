from django.test import TestCase

from waldur_mastermind.chat import prompts, tools


class ToolDefinitionTest(TestCase):
    def test_tool_definition_fields(self):
        definition = tools.ToolDefinition(
            name="test_tool",
            description="A test tool",
            inputSchema={"type": "object", "properties": {}},
        )

        self.assertEqual(definition.name, "test_tool")
        self.assertEqual(definition.description, "A test tool")
        self.assertEqual(definition.inputSchema, {"type": "object", "properties": {}})


class ToolRegistryTest(TestCase):
    def test_show_user_resources_is_registered(self):
        self.assertIn("show_user_resources", tools.TOOL_REGISTRY)

    def test_show_user_resources_has_correct_definition(self):
        tool = tools.TOOL_REGISTRY["show_user_resources"]

        self.assertEqual(tool.name, "show_user_resources")
        self.assertIn("cloud resources", tool.description.lower())
        self.assertEqual(tool.inputSchema["type"], "object")
        self.assertEqual(tool.inputSchema["properties"], {})
        self.assertEqual(tool.inputSchema["required"], [])


class GetToolsPromptTest(TestCase):
    def test_returns_string(self):
        prompt = tools.get_tools_prompt()
        self.assertIsInstance(prompt, str)

    def test_includes_tool_names(self):
        prompt = tools.get_tools_prompt()
        self.assertIn("show_user_resources", prompt)

    def test_includes_tool_descriptions(self):
        prompt = tools.get_tools_prompt()
        tool = tools.TOOL_REGISTRY["show_user_resources"]
        self.assertIn(tool.description, prompt)

    def test_tool_with_parameters_shows_parameter_info(self):
        # Add a temporary tool with parameters to test formatting
        original_registry = tools.TOOL_REGISTRY.copy()
        try:
            tools.TOOL_REGISTRY["test_tool_with_params"] = tools.ToolDefinition(
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

            prompt = tools.get_tools_prompt()

            self.assertIn("param1", prompt)
            self.assertIn("string", prompt)
            self.assertIn("required", prompt)
            self.assertIn("param2", prompt)
            self.assertIn("integer", prompt)
            self.assertIn("optional", prompt)
        finally:
            tools.TOOL_REGISTRY.clear()
            tools.TOOL_REGISTRY.update(original_registry)

    def test_tool_without_parameters_shows_no_parameters(self):
        prompt = tools.get_tools_prompt()
        # show_user_resources has no parameters
        self.assertIn("no parameters", prompt)


class UICapabilitiesTest(TestCase):
    def test_ui_capabilities_is_string(self):
        self.assertIsInstance(prompts.UI_CAPABILITIES, str)

    def test_includes_mermaid_capabilities(self):
        self.assertIn("mermaid", prompts.UI_CAPABILITIES.lower())
        self.assertIn("```mermaid", prompts.UI_CAPABILITIES)

    def test_includes_code_capabilities(self):
        self.assertIn("code", prompts.UI_CAPABILITIES.lower())
        self.assertIn("```python", prompts.UI_CAPABILITIES)

    def test_includes_table_capabilities(self):
        self.assertIn("table", prompts.UI_CAPABILITIES.lower())


class SystemPromptTest(TestCase):
    def test_system_prompt_is_string(self):
        self.assertIsInstance(prompts.SYSTEM_PROMPT, str)

    def test_system_prompt_includes_ui_capabilities(self):
        # UI_CAPABILITIES should be part of SYSTEM_PROMPT
        self.assertIn("UI RENDERING CAPABILITIES", prompts.SYSTEM_PROMPT)
        self.assertIn("mermaid", prompts.SYSTEM_PROMPT.lower())

    def test_system_prompt_includes_tool_instructions(self):
        # TOOL_INSTRUCTIONS content should be in SYSTEM_PROMPT
        self.assertIn("TOOLS ARE EXTREMELY RARE", prompts.SYSTEM_PROMPT)
        self.assertIn("{tools}", prompts.SYSTEM_PROMPT)
