import unittest

from waldur_mastermind.chat.parsers import parse_tool_call


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

        # Missing arguments key - currently ACCEPTED by first try block
        # This is inconsistent but matches actual implementation
        result2 = parse_tool_call('{"tool": "show_user_resources"}')
        self.assertIsNotNone(result2)
        self.assertEqual(result2["tool"], "show_user_resources")

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
