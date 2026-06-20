"""Unit tests for the data_match and tool_arguments evaluators added for
the support-assistant validation harness."""

from django.test import SimpleTestCase

from waldur_mastermind.chat.validation.evaluators import (
    DataMatchEvaluator,
    ToolArgumentsEvaluator,
    ToolUsageEvaluator,
)


class DataMatchEvaluatorTest(SimpleTestCase):
    def setUp(self):
        self.ev = DataMatchEvaluator()

    def test_passes_when_all_values_present(self):
        result = self.ev.evaluate(
            "Project Alpha has a balance of 16,806.00",
            {"expected_values": ["Alpha", "16,806"], "rationale": "x"},
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertEqual(result.details["missing"], [])

    def test_fails_when_any_value_missing(self):
        result = self.ev.evaluate(
            "Project Beta",
            {"expected_values": ["Alpha", "Beta"], "rationale": "x"},
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.details["missing"], ["Alpha"])

    def test_case_insensitive(self):
        result = self.ev.evaluate(
            "PROJECT ALPHA", {"expected_values": ["alpha"], "rationale": "x"}
        )
        self.assertTrue(result.passed)

    def test_empty_expected_values_passes_trivially(self):
        result = self.ev.evaluate("anything", {"expected_values": []})
        self.assertTrue(result.passed)


class ToolArgumentsEvaluatorTest(SimpleTestCase):
    def setUp(self):
        self.ev = ToolArgumentsEvaluator()

    def test_passes_when_args_match(self):
        result = self.ev.evaluate(
            "",
            {
                "tool": "explain_project_credit_balance",
                "args_must_contain": {"project_name": "Alpha"},
                "tool_calls": [
                    {
                        "name": "explain_project_credit_balance",
                        "arguments": {"project_name": "Project Alpha"},
                    }
                ],
            },
        )
        self.assertTrue(result.passed)

    def test_fails_when_tool_not_called(self):
        result = self.ev.evaluate(
            "",
            {
                "tool": "explain_project_credit_balance",
                "args_must_contain": {"project_name": "Alpha"},
                "tool_calls": [],
            },
        )
        self.assertFalse(result.passed)

    def test_fails_when_args_dont_match(self):
        result = self.ev.evaluate(
            "",
            {
                "tool": "explain_project_credit_balance",
                "args_must_contain": {"project_name": "Alpha"},
                "tool_calls": [
                    {
                        "name": "explain_project_credit_balance",
                        "arguments": {"project_name": "Beta"},
                    }
                ],
            },
        )
        self.assertFalse(result.passed)

    def test_passes_if_any_call_matches(self):
        # Tool called twice — one wrong, one right; should pass.
        result = self.ev.evaluate(
            "",
            {
                "tool": "explain_project_credit_balance",
                "args_must_contain": {"project_name": "Alpha"},
                "tool_calls": [
                    {
                        "name": "explain_project_credit_balance",
                        "arguments": {"project_name": "Beta"},
                    },
                    {
                        "name": "explain_project_credit_balance",
                        "arguments": {"project_name": "Alpha"},
                    },
                ],
            },
        )
        self.assertTrue(result.passed)


class ToolUsageMultiCallEvaluatorTest(SimpleTestCase):
    """Verifies the multi-call match logic introduced for lazy-load."""

    def setUp(self):
        self.ev = ToolUsageEvaluator()

    def test_passes_when_target_anywhere_in_chain(self):
        result = self.ev.evaluate(
            "",
            {
                "expected_tool": "explain_project_credit_balance",
                "tool_calls": [
                    {"name": "search_tools"},
                    {"name": "explain_project_credit_balance"},
                ],
                "rationale": "lazy-load",
            },
        )
        self.assertTrue(result.passed)

    def test_fails_when_only_search_tools_called(self):
        result = self.ev.evaluate(
            "",
            {
                "expected_tool": "explain_project_credit_balance",
                "tool_calls": [{"name": "search_tools"}],
                "rationale": "x",
            },
        )
        self.assertFalse(result.passed)

    def test_fails_when_wrong_domain_tool_called(self):
        result = self.ev.evaluate(
            "",
            {
                "expected_tool": "explain_project_credit_balance",
                "tool_calls": [
                    {"name": "search_tools"},
                    {"name": "get_project_quota"},
                ],
                "rationale": "x",
            },
        )
        self.assertFalse(result.passed)

    def test_passes_when_no_tool_expected_and_none_called(self):
        result = self.ev.evaluate("", {"expected_tool": None, "tool_calls": []})
        self.assertTrue(result.passed)
