import unittest
from pathlib import Path

from waldur_mastermind.chat.validation.scenarios import (
    EvaluationCriteria,
    Scenario,
    load_all_scenarios,
    load_scenarios_from_yaml,
)


class ScenarioLoadingTest(unittest.TestCase):
    """Tests for loading validation scenarios from YAML files."""

    @classmethod
    def setUpClass(cls):
        """Set up paths used by all tests."""
        chat_dir = Path(__file__).parent.parent
        cls.scenarios_dir = chat_dir / "validation_scenarios"
        cls.tool_usage_yaml = cls.scenarios_dir / "tool_usage.yaml"

    def test_load_tool_usage_scenarios(self):
        """Test loading scenarios from tool_usage.yaml."""
        scenarios = load_scenarios_from_yaml(self.tool_usage_yaml)

        self.assertGreater(len(scenarios), 0)
        self.assertTrue(all(isinstance(s, Scenario) for s in scenarios))

    def test_greeting_no_tool_scenario(self):
        """Test that greeting_no_tool scenario is loaded correctly."""
        scenarios = load_scenarios_from_yaml(self.tool_usage_yaml)
        greeting_scenario = next(
            (s for s in scenarios if s.name == "greeting_no_tool"), None
        )

        self.assertIsNotNone(greeting_scenario)
        self.assertEqual(greeting_scenario.category, "tool_usage")
        self.assertIn("hello", greeting_scenario.inputs)
        self.assertGreater(len(greeting_scenario.evaluations), 0)
        self.assertTrue(
            all(
                isinstance(ev, EvaluationCriteria)
                for ev in greeting_scenario.evaluations
            )
        )

    def test_evaluation_criteria_conversion(self):
        """Test that evaluation dicts are converted to EvaluationCriteria objects."""
        scenarios = load_scenarios_from_yaml(self.tool_usage_yaml)

        for scenario in scenarios:
            for evaluation in scenario.evaluations:
                self.assertIsInstance(evaluation, EvaluationCriteria)
                self.assertIn(
                    evaluation.type,
                    {"tool_usage", "pattern", "length", "code_block", "language"},
                )
                self.assertIsInstance(evaluation.config, dict)

    def test_load_prompt_following_scenarios(self):
        """Test loading scenarios from prompt_following.yaml."""
        prompt_following_yaml = self.scenarios_dir / "prompt_following.yaml"
        scenarios = load_scenarios_from_yaml(prompt_following_yaml)

        self.assertGreater(len(scenarios), 0)
        self.assertTrue(all(isinstance(s, Scenario) for s in scenarios))

        # Check for specific scenarios
        scenario_names = {s.name for s in scenarios}
        self.assertIn("no_mention_tools_greeting", scenario_names)
        self.assertIn("json_only_for_tools", scenario_names)
        self.assertIn("uses_code_blocks_with_language", scenario_names)

    def test_load_all_scenarios(self):
        """Test loading all scenarios from directory."""
        scenarios_by_category = load_all_scenarios(self.scenarios_dir)

        self.assertIn("tool_usage", scenarios_by_category)
        self.assertGreater(len(scenarios_by_category["tool_usage"]), 0)

        self.assertIn("prompt_following", scenarios_by_category)
        self.assertGreater(len(scenarios_by_category["prompt_following"]), 0)

    def test_missing_file_raises_error(self):
        """Test that loading non-existent file raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            load_scenarios_from_yaml(Path("/nonexistent/file.yaml"))
