"""Integration tests for scenario validation."""

import unittest
from pathlib import Path

from waldur_mastermind.chat.validation.evaluators import get_evaluator
from waldur_mastermind.chat.validation.scenarios import load_scenarios_from_yaml


class ScenarioEvaluationTest(unittest.TestCase):
    """Test full scenario loading and evaluation."""

    @classmethod
    def setUpClass(cls):
        """Set up paths and load scenarios once for all tests."""
        chat_dir = Path(__file__).parent.parent
        scenarios_file = chat_dir / "validation_scenarios" / "tool_usage.yaml"
        cls.scenarios = load_scenarios_from_yaml(scenarios_file)

    def test_evaluate_greeting_scenario(self):
        """Test evaluating a greeting with the tool_usage evaluator."""
        # Find the greeting scenario
        greeting_scenario = next(
            s for s in self.scenarios if s.name == "greeting_no_tool"
        )

        # Mock AI Assistant response to a greeting (should NOT include a tool call)
        llm_response = "Hello! How can I help you today?"

        # Evaluate the response
        evaluation_criteria = greeting_scenario.evaluations[0]
        evaluator = get_evaluator(evaluation_criteria.type)
        result = evaluator.evaluate(llm_response, evaluation_criteria.config)

        # Verify evaluation passed
        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertIn("avoided tool usage", result.message.lower())

    def test_evaluate_greeting_scenario_failure(self):
        """Test that greeting with unexpected native tool call fails evaluation."""
        # Find the greeting scenario
        greeting_scenario = next(
            s for s in self.scenarios if s.name == "greeting_no_tool"
        )

        # Simulate: AI Assistant response text + a native function call in config
        llm_response = "Here are your resources."
        config = dict(greeting_scenario.evaluations[0].config)
        config["tool_calls"] = [{"name": "display_user_resources"}]

        # Evaluate the response
        evaluation_criteria = greeting_scenario.evaluations[0]
        evaluator = get_evaluator(evaluation_criteria.type)
        result = evaluator.evaluate(llm_response, config)

        # Verify evaluation failed
        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertIn("unexpectedly called tool", result.message.lower())

    def test_evaluate_show_resources_scenario(self):
        """Test evaluating a resource request with the tool_usage evaluator."""
        # Find the show_resources scenario
        show_resources_scenario = next(
            s for s in self.scenarios if s.name == "show_resources_uses_tool"
        )

        # Simulate: AI Assistant response text + native function call in config
        llm_response = "Here are your resources."
        config = dict(show_resources_scenario.evaluations[0].config)
        config["tool_calls"] = [{"name": "display_user_resources"}]

        # Evaluate the response
        evaluation_criteria = show_resources_scenario.evaluations[0]
        evaluator = get_evaluator(evaluation_criteria.type)
        result = evaluator.evaluate(llm_response, config)

        # Verify evaluation passed
        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertIn("correctly called tool", result.message.lower())

    def test_evaluate_all_inputs_for_scenario(self):
        """Test that all inputs for a scenario can be evaluated."""
        # Find the greeting scenario
        greeting_scenario = next(
            s for s in self.scenarios if s.name == "greeting_no_tool"
        )

        # Mock AI Assistant response
        llm_response = "Hi there! What would you like to know?"

        # Get evaluator
        evaluation_criteria = greeting_scenario.evaluations[0]
        evaluator = get_evaluator(evaluation_criteria.type)

        # Test that each input could be evaluated
        # (In real usage, each input would be sent to AI Assistant separately)
        for input_text in greeting_scenario.inputs:
            # Just verify the scenario structure is valid
            self.assertIsInstance(input_text, str)
            self.assertGreater(len(input_text), 0)

        # Evaluate the mock response
        result = evaluator.evaluate(llm_response, evaluation_criteria.config)
        self.assertTrue(result.passed)
