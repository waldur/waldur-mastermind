import unittest
from pathlib import Path

from waldur_mastermind.chat.validation.evaluators import PatternEvaluator
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


class ScenarioRoleTest(unittest.TestCase):
    """The role field routes a scenario to the authenticated or anonymous pipeline."""

    scenarios_dir = Path(__file__).parent.parent / "validation_scenarios"

    def test_role_defaults_to_authenticated(self):
        scenario = Scenario(
            name="x", category="c", description="", inputs=["hi"], evaluations=[]
        )
        self.assertEqual(scenario.role, "authenticated")

    def test_invalid_role_raises(self):
        with self.assertRaises(ValueError):
            Scenario(
                name="x",
                category="c",
                description="",
                inputs=["hi"],
                evaluations=[],
                role="staff",
            )

    def test_anonymous_discovery_scenarios_load_with_role(self):
        scenarios = load_scenarios_from_yaml(
            self.scenarios_dir / "anonymous_discovery.yaml"
        )
        self.assertGreater(len(scenarios), 0)
        for scenario in scenarios:
            self.assertEqual(scenario.role, "anonymous")
            self.assertEqual(scenario.category, "anonymous_discovery")

    def test_existing_scenarios_stay_authenticated(self):
        scenarios = load_scenarios_from_yaml(self.scenarios_dir / "tool_usage.yaml")
        for scenario in scenarios:
            self.assertEqual(scenario.role, "authenticated")


class AccessGuidanceScenarioTest(unittest.TestCase):
    """The access-guidance scenarios pin how the assistant hands users on:
    the provider grants access; the assistant never submits requests or
    invents approval details.

    The preset split is part of the contract: scenarios that name a
    concrete offering need the preset so the pinned first tool round
    becomes a search; the on-behalf refusal must stay catalog-agnostic
    so it scores on any deployment.
    """

    scenarios_dir = Path(__file__).parent.parent / "validation_scenarios"

    def test_access_guidance_scenarios_present_with_preset_split(self):
        scenarios = {
            s.name: s
            for s in load_scenarios_from_yaml(
                self.scenarios_dir / "anonymous_discovery.yaml"
            )
        }
        self.assertIsNone(scenarios["access_request_not_submitted"].preset)
        self.assertEqual(scenarios["provider_manages_access"].preset, "hpc_ai_platform")
        self.assertEqual(
            scenarios["access_timeline_not_invented"].preset, "hpc_ai_platform"
        )


class ScenarioPatternGuardTest(unittest.TestCase):
    """Forbidden patterns are matched with plain findall, so a pattern that
    names a fabricated claim also matches its negation ("not automatically
    granted") — and the persona primes exactly those phrasings. These tests
    pin that denials and hypotheticals stay clean while genuine fabricated
    claims still trip the pattern.
    """

    scenarios_dir = Path(__file__).parent.parent / "validation_scenarios"

    @classmethod
    def setUpClass(cls):
        cls.scenarios = {
            s.name: s
            for s in load_scenarios_from_yaml(
                cls.scenarios_dir / "anonymous_discovery.yaml"
            )
        }

    def _forbidden_violations(self, scenario_name, text):
        criteria = next(
            e for e in self.scenarios[scenario_name].evaluations if e.type == "pattern"
        )
        result = PatternEvaluator().evaluate(
            text, {"forbidden_patterns": criteria.config["forbidden_patterns"]}
        )
        return result.details["violations"]

    def assert_clean(self, scenario_name, text):
        violations = self._forbidden_violations(scenario_name, text)
        self.assertEqual(violations, [], f"{text!r} should not trip: {violations}")

    def assert_trips(self, scenario_name, text):
        self.assertNotEqual(
            self._forbidden_violations(scenario_name, text),
            [],
            f"{text!r} should trip a forbidden pattern",
        )

    def test_submission_denials_and_hypotheticals_stay_clean(self):
        self.assert_clean(
            "access_request_not_submitted",
            "Once your request has been submitted, the provider reviews it.",
        )
        self.assert_clean(
            "access_request_not_submitted",
            "Puhuri will notify you by email once the provider decides.",
        )

    def test_submission_claims_still_trip(self):
        self.assert_trips(
            "access_request_not_submitted", "Your request has been submitted."
        )
        self.assert_trips(
            "access_request_not_submitted", "I will notify you once it's approved."
        )
        self.assert_trips(
            "access_request_not_submitted", "I've submitted the request for you."
        )

    def test_approval_negations_stay_clean(self):
        self.assert_clean(
            "provider_manages_access",
            "Access is not automatically granted; the provider reviews each request.",
        )
        self.assert_clean(
            "provider_manages_access", "Requests aren't automatically approved."
        )

    def test_approval_claims_still_trip(self):
        self.assert_trips(
            "provider_manages_access", "Your request is automatically granted."
        )
        self.assert_trips(
            "provider_manages_access", "We approve requests within a day."
        )

    def test_eligibility_negation_stays_clean(self):
        self.assert_clean(
            "eligibility_not_fabricated",
            "Being listed doesn't mean it's open to everyone.",
        )

    def test_eligibility_claim_still_trips(self):
        self.assert_trips(
            "eligibility_not_fabricated",
            "The SLURM HPC Cluster is open to everyone.",
        )

    def test_timeline_negation_stays_clean(self):
        self.assert_clean(
            "access_timeline_not_invented",
            "The offering doesn't say whether it takes a few days.",
        )

    def test_timeline_claim_still_trips(self):
        self.assert_trips(
            "access_timeline_not_invented", "Access typically takes a few days."
        )
        self.assert_trips("access_timeline_not_invented", "Expect 5 business days.")

    def test_injection_scenario_forbids_access_guidance_header(self):
        criteria = next(
            e
            for e in self.scenarios["prompt_injection_refused"].evaluations
            if e.type == "pattern"
        )
        self.assertIn("=== ACCESS GUIDANCE ===", criteria.config["forbidden_patterns"])
