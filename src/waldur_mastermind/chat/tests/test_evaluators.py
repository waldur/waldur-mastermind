import unittest

from waldur_mastermind.chat.validation.evaluators import (
    CodeBlockEvaluator,
    PatternEvaluator,
    ToolUsageEvaluator,
    get_evaluator,
)


class ToolUsageEvaluatorTest(unittest.TestCase):
    """Tests for ToolUsageEvaluator."""

    def setUp(self):
        """Set up evaluator for each test."""
        self.evaluator = ToolUsageEvaluator()

    def test_no_tool_expected_no_tool_called(self):
        """Test that absence of tool call is detected when expected."""
        response = "Hello! How can I help you today?"
        config = {
            "expected_tool": None,
            "rationale": "Greetings should not trigger tools",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertIn("avoided tool usage", result.message.lower())
        self.assertIsNone(result.details["expected_tool"])
        self.assertIsNone(result.details["actual_tool"])

    def test_no_tool_expected_but_tool_called(self):
        """Test that unexpected tool call is detected."""
        response = '{"tool": "show_user_resources", "arguments": {}}'
        config = {
            "expected_tool": None,
            "rationale": "Simple questions should not trigger tools",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertIn("unexpectedly called tool", result.message.lower())
        self.assertIsNone(result.details["expected_tool"])
        self.assertEqual(result.details["actual_tool"], "show_user_resources")

    def test_correct_tool_called(self):
        """Test that correct tool call is detected."""
        response = '{"tool": "show_user_resources", "arguments": {"user_id": 123}}'
        config = {
            "expected_tool": "show_user_resources",
            "rationale": "User requested their resources",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertIn("correctly called tool", result.message.lower())
        self.assertEqual(result.details["expected_tool"], "show_user_resources")
        self.assertEqual(result.details["actual_tool"], "show_user_resources")
        self.assertEqual(result.details["tool_call"]["tool"], "show_user_resources")

    def test_expected_tool_not_called(self):
        """Test that missing expected tool is detected."""
        response = "I don't have access to your resources."
        config = {
            "expected_tool": "show_user_resources",
            "rationale": "User explicitly requested data",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertIn("no tool was called", result.message.lower())
        self.assertEqual(result.details["expected_tool"], "show_user_resources")
        self.assertIsNone(result.details["actual_tool"])

    def test_wrong_tool_called(self):
        """Test that incorrect tool call is detected."""
        response = '{"tool": "create_resource", "arguments": {}}'
        config = {
            "expected_tool": "show_user_resources",
            "rationale": "User asked to view, not create",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertIn("but got", result.message.lower())
        self.assertEqual(result.details["expected_tool"], "show_user_resources")
        self.assertEqual(result.details["actual_tool"], "create_resource")

    def test_tool_call_with_markdown_wrapper(self):
        """Test that tool calls wrapped in markdown are parsed correctly."""
        response = '```json\n{"tool": "show_user_resources", "arguments": {}}\n```'
        config = {
            "expected_tool": "show_user_resources",
            "rationale": "User requested resources",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)


class PatternEvaluatorTest(unittest.TestCase):
    """Tests for PatternEvaluator."""

    def setUp(self):
        """Set up evaluator for each test."""
        self.evaluator = PatternEvaluator()

    def test_no_forbidden_patterns_found(self):
        """Test that response without forbidden patterns passes."""
        response = "Hello! I can help you with resources and virtual machines."
        config = {
            "forbidden_patterns": [r"\btool\b", r"\bfunction\b", r"\bAPI\b"],
            "rationale": "Should not mention implementation details",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertEqual(len(result.details["violations"]), 0)

    def test_forbidden_pattern_found(self):
        """Test that response with forbidden pattern fails."""
        response = "I'll use my tool to fetch your resources."
        config = {
            "forbidden_patterns": [r"\btool\b", r"\bfunction\b"],
            "rationale": "Should not mention tools",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertEqual(len(result.details["violations"]), 1)
        self.assertEqual(result.details["violations"][0]["pattern"], r"\btool\b")

    def test_required_pattern_present(self):
        """Test that response with required pattern passes."""
        response = "Here is your list of virtual machines."
        config = {
            "required_patterns": [r"virtual machine"],
            "rationale": "Should mention VMs",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertEqual(len(result.details["missing_required"]), 0)

    def test_required_pattern_missing(self):
        """Test that response without required pattern fails."""
        response = "Here is your list of resources."
        config = {
            "required_patterns": [r"virtual machine"],
            "rationale": "Should mention VMs",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertIn(r"virtual machine", result.details["missing_required"])

    def test_case_insensitive_matching(self):
        """Test that pattern matching is case insensitive."""
        response = "I'll use my TOOL to help you."
        config = {
            "forbidden_patterns": [r"\btool\b"],
            "rationale": "Should not mention tools",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.details["violations"]), 1)


class CodeBlockEvaluatorTest(unittest.TestCase):
    """Tests for CodeBlockEvaluator."""

    def setUp(self):
        """Set up evaluator for each test."""
        self.evaluator = CodeBlockEvaluator()

    def test_no_code_blocks(self):
        """Test that response without code blocks passes."""
        response = "Here is a simple explanation without code."
        config = {
            "require_language_identifier": True,
            "rationale": "Code should have language identifiers",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertEqual(result.details["code_blocks_found"], 0)

    def test_code_block_with_language_identifier(self):
        """Test that code block with language identifier passes."""
        response = """Here is an example:
```python
def hello():
    print("Hello")
```
"""
        config = {
            "require_language_identifier": True,
            "rationale": "Code should have language identifiers",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertEqual(result.details["code_blocks_found"], 1)
        self.assertEqual(len(result.details["blocks_without_language"]), 0)

    def test_code_block_without_language_identifier(self):
        """Test that code block without language identifier fails."""
        response = """Here is an example:
```
def hello():
    print("Hello")
```
"""
        config = {
            "require_language_identifier": True,
            "rationale": "Code should have language identifiers",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertEqual(len(result.details["blocks_without_language"]), 1)

    def test_allowed_languages_validation(self):
        """Test that only allowed languages pass validation."""
        response = """Here is an example:
```javascript
console.log("Hello");
```
"""
        config = {
            "require_language_identifier": True,
            "allowed_languages": ["python", "bash"],
            "rationale": "Only Python and Bash allowed",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertEqual(len(result.details["blocks_with_wrong_language"]), 1)

    def test_multiple_code_blocks(self):
        """Test evaluation with multiple code blocks."""
        response = """First example:
```python
print("Python")
```

Second example:
```bash
echo "Bash"
```
"""
        config = {
            "require_language_identifier": True,
            "allowed_languages": ["python", "bash"],
            "rationale": "Multiple blocks should all be valid",
        }

        result = self.evaluator.evaluate(response, config)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertEqual(result.details["code_blocks_found"], 2)


class EvaluatorRegistryTest(unittest.TestCase):
    """Tests for evaluator registry."""

    def test_get_tool_usage_evaluator(self):
        """Test that tool_usage evaluator can be retrieved."""
        evaluator = get_evaluator("tool_usage")
        self.assertIsInstance(evaluator, ToolUsageEvaluator)

    def test_get_pattern_evaluator(self):
        """Test that pattern evaluator can be retrieved."""
        evaluator = get_evaluator("pattern")
        self.assertIsInstance(evaluator, PatternEvaluator)

    def test_get_code_block_evaluator(self):
        """Test that code_block evaluator can be retrieved."""
        evaluator = get_evaluator("code_block")
        self.assertIsInstance(evaluator, CodeBlockEvaluator)

    def test_get_unknown_evaluator_raises_error(self):
        """Test that unknown evaluator type raises ValueError."""
        with self.assertRaisesRegex(ValueError, "Unknown evaluator type"):
            get_evaluator("nonexistent_type")
