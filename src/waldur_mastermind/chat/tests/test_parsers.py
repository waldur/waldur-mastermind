import unittest

from waldur_mastermind.chat.parsers import looks_like_tool_call, parse_tool_call


class ParseToolCallTest(unittest.TestCase):
    """
    Critical tests for parse_tool_call() - a security boundary handling untrusted LLM output.
    """

    def test_parses_clean_json_tool_call(self):
        """Happy path: well-formed JSON tool call."""
        content = '{"tool": "show_user_resources", "arguments": {}}'
        result = parse_tool_call(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "show_user_resources")
        self.assertEqual(result["arguments"], {})

    def test_parses_markdown_wrapped_json(self):
        """LLMs often wrap JSON in markdown code blocks."""
        content = '```json\n{"tool": "show_user_resources", "arguments": {}}\n```'
        result = parse_tool_call(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "show_user_resources")

    def test_parses_markdown_without_language_tag(self):
        """Some LLMs use bare ``` without language tag."""
        content = '```\n{"tool": "show_user_resources", "arguments": {}}\n```'
        result = parse_tool_call(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "show_user_resources")
        self.assertEqual(result["arguments"], {})

    def test_returns_none_for_malformed_json(self):
        """Security: malformed JSON must not crash, return None."""
        content = '{"tool": "show_user_resources", "arguments": {'
        result = parse_tool_call(content)

        self.assertIsNone(result)

    def test_returns_none_for_missing_required_fields(self):
        """Security: must validate 'tool' key exists."""
        # Missing tool key - should return None
        result1 = parse_tool_call('{"action": "do_something", "arguments": {}}')
        self.assertIsNone(result1)

        # Missing arguments key - valid for tools with no parameters (e.g. list_projects)
        result2 = parse_tool_call('{"tool": "list_projects"}')
        self.assertIsNotNone(result2)
        self.assertEqual(result2["tool"], "list_projects")

    def test_extracts_no_arguments_tool_call_from_mixed_text(self):
        """Tools with no arguments should be parsed even when LLM adds prefix text and code fences."""
        content = (
            "Okay, let's get started with creating a virtual machine. "
            "First, I need to know which project you'd like to create the VM in. "
            "Here's a list of projects you have access to:\n\n"
            '```json\n{"tool": "list_projects"}\n```\n\n'
            "Please review the list and let me know which project to use."
        )
        result = parse_tool_call(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "list_projects")

    def test_returns_none_for_empty_input(self):
        """Security: empty/whitespace input must not crash."""
        self.assertIsNone(parse_tool_call(""))
        self.assertIsNone(parse_tool_call("   \n\t  "))
        self.assertIsNone(parse_tool_call("Please show my resources"))

    def test_handles_nested_json_safely(self):
        """Security: nested JSON in arguments treated as data, not executed."""
        content = '{"tool": "safe_tool", "arguments": {"input": "{\\"tool\\": \\"malicious\\"}"}}'
        result = parse_tool_call(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "safe_tool")
        # Nested JSON is string data, not a separate tool call
        self.assertIsInstance(result["arguments"]["input"], str)

    def test_handles_unicode_content(self):
        """Common case: unicode in arguments."""
        content = '{"tool": "example", "arguments": {"text": "Hello 世界 🌍"}}'
        result = parse_tool_call(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["arguments"]["text"], "Hello 世界 🌍")

    def test_handles_large_content_safely(self):
        """Security: large content should not cause performance issues."""
        # Create tool call with moderately large argument
        large_data = "x" * 10000
        content = f'{{"tool": "example", "arguments": {{"data": "{large_data}"}}}}'
        result = parse_tool_call(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "example")

    def test_extracts_json_from_mixed_text_with_nested_arguments(self):
        """LLM sometimes outputs text before/after tool call JSON with nested args."""
        content = """Okay, you want to change the flavor to 'small'. Here's the updated configuration:

{"tool": "preview_vm", "arguments": {"project_uuid": "waldurtest", "name": "monday", "flavor": "small", "image": "Ubuntu 20.04"}}

Now, let's preview this configuration before creating the VM."""

        result = parse_tool_call(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "preview_vm")
        self.assertEqual(result["arguments"]["project_uuid"], "waldurtest")
        self.assertEqual(result["arguments"]["name"], "monday")
        self.assertEqual(result["arguments"]["flavor"], "small")
        self.assertEqual(result["arguments"]["image"], "Ubuntu 20.04")

    def test_extracts_first_valid_tool_call_from_multiple_json_objects(self):
        """If content has multiple JSON objects, extract the first valid tool call."""
        content = """Here's some data: {"data": "not a tool"}

And here's the tool call: {"tool": "show_user_resources", "arguments": {}}

And some trailing text with more JSON: {"random": "object"}"""

        result = parse_tool_call(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "show_user_resources")

    def test_handles_deeply_nested_arguments(self):
        """Tool calls can have deeply nested argument structures."""
        content = """{"tool": "create_vm", "arguments": {"name": "test", "ports": [{"subnet": "uuid", "security_groups": [{"name": "default"}]}]}}"""

        result = parse_tool_call(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "create_vm")
        self.assertEqual(result["arguments"]["name"], "test")
        self.assertIsInstance(result["arguments"]["ports"], list)
        self.assertEqual(len(result["arguments"]["ports"]), 1)


class LooksLikeToolCallTest(unittest.TestCase):
    """
    Tests for looks_like_tool_call() — used during streaming to decide whether
    to buffer output while waiting for a complete tool call JSON.
    """

    def test_returns_true_for_clean_json_start(self):
        """Fast path: content starts with { — definitely a tool call."""
        self.assertTrue(
            looks_like_tool_call('{"tool": "show_user_resources", "arguments": {}}')
        )

    def test_returns_true_for_short_preamble(self):
        """Short preamble before the JSON — {"tool" is still present."""
        content = 'Here is the call: {"tool": "show_user_resources", "arguments": {}}'
        self.assertTrue(looks_like_tool_call(content))

    def test_returns_true_for_long_preamble_with_tool_pattern(self):
        """LLM adds >200 chars of text before tool call JSON — {"tool" catches it."""
        long_preamble = "a" * 300
        content = f'{long_preamble}{{"tool": "preview_vm", "arguments": {{}}}}'
        self.assertTrue(looks_like_tool_call(content))

    def test_returns_true_for_markdown_wrapped_tool_call(self):
        """
        LLM wraps tool call in ```json code block despite instructions not to.
        The stripped content after fence removal contains '{"tool"'.
        """
        content = '```json\n{"tool": "preview_vm", "arguments": {}}\n```'
        self.assertTrue(looks_like_tool_call(content))

    def test_returns_true_for_markdown_with_long_preamble(self):
        """Markdown code block with long explanatory text before it."""
        long_preamble = (
            "Okay, excellent! You've chosen the 'm1.small' flavor and Ubuntu 22.04 image. "
            * 4
        )
        content = (
            f'{long_preamble}```json\n{{"tool": "create_vm", "arguments": {{}}}}\n```'
        )
        self.assertTrue(looks_like_tool_call(content))

    def test_returns_false_for_plain_text(self):
        """No JSON at all — regular conversational reply."""
        self.assertFalse(looks_like_tool_call("Hello! How can I help you today?"))

    def test_returns_false_for_empty_input(self):
        """Empty and whitespace-only inputs must return False, not crash."""
        self.assertFalse(looks_like_tool_call(""))
        self.assertFalse(looks_like_tool_call("   \n\t  "))

    def test_returns_false_for_text_mentioning_tool_conceptually(self):
        """
        A response that *mentions* 'tool' without containing a JSON tool call.
        The string '{"tool"' must NOT appear in regular prose.
        """
        content = (
            "I can use a tool to help you. Let me explain how tools work in Waldur."
        )
        self.assertFalse(looks_like_tool_call(content))

    def test_returns_false_for_brace_beyond_200_chars_without_tool_pattern(self):
        """Content has a { beyond 200 chars but it's not a tool call."""
        long_text = "This is a long explanation " * 10  # >200 chars
        content = f"{long_text}{{some: data}}"  # brace but no '{"tool"' key
        self.assertFalse(looks_like_tool_call(content))

    def test_returns_false_for_curly_brace_in_normal_response(self):
        """
        Root cause of the unrendered-JSON bug: LLM response containing { within
        first 200 chars (e.g. template variables, dict examples) was incorrectly
        triggering tool call buffering. With the stricter '{"tool"'-only check
        these responses must return False and be streamed normally.
        """
        self.assertFalse(looks_like_tool_call("The {project} VM is ready."))
        self.assertFalse(
            looks_like_tool_call("Use {flavor} and {image} for your configuration.")
        )
        self.assertFalse(looks_like_tool_call('{"key": "value"}'))  # JSON, but no tool
        self.assertFalse(
            looks_like_tool_call(
                "Here is an example: {'name': 'vm1', 'flavor': 'm1.small'}"
            )
        )
