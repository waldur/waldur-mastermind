import time
from pathlib import Path

from constance import config
from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.chat.block_schemas import blocks_to_text
from waldur_mastermind.chat.context_assembler import build_context
from waldur_mastermind.chat.health_checks import (
    LLMConfigurationHealthCheck,
    LLMConnectivityHealthCheck,
    LLMResponseHealthCheck,
)
from waldur_mastermind.chat.llm_streamer import LLMStreamer
from waldur_mastermind.chat.validation.evaluators import get_evaluator
from waldur_mastermind.chat.validation.scenarios import load_all_scenarios


class Command(BaseCommand):
    help = """
    AI Assistant management commands.

    Available subcommands:
        health              - Check AI Assistant infrastructure health
        validate_scenarios  - Validate scenario YAML files
        test_evaluation     - Test evaluation with real AI Assistant responses
        run_all             - Run all checks (health, validate, test)

    Examples:
        waldur ai_assistant health
        waldur ai_assistant validate_scenarios
        waldur ai_assistant test_evaluation
        waldur ai_assistant test_evaluation --scenario greeting_no_tool
        waldur ai_assistant run_all
    """

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(
            dest="subcommand",
            help="Available subcommands",
        )

        # Health subcommand
        subparsers.add_parser(
            "health",
            help="Check AI Assistant infrastructure health",
        )

        # Validate scenarios subcommand
        subparsers.add_parser(
            "validate_scenarios",
            help="Validate scenario YAML files",
        )

        # Test evaluation subcommand
        test_eval_parser = subparsers.add_parser(
            "test_evaluation",
            help="Test evaluation with real AI Assistant responses",
        )
        test_eval_parser.add_argument(
            "--scenario",
            help="Scenario name to test (e.g., greeting_no_tool)",
        )
        test_eval_parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed output for all tests (default: only failures)",
        )

        # Run all subcommand
        run_all_parser = subparsers.add_parser(
            "run_all",
            help="Run all checks (health, validate, test)",
        )
        run_all_parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed output for all tests (default: only failures)",
        )

    def handle(self, **options):
        subcommand = options.get("subcommand")

        if not subcommand:
            self.print_help("manage.py", "ai_assistant")
            return

        handler = getattr(self, f"handle_{subcommand}", None)
        if handler:
            handler(**options)
        else:
            raise CommandError(f"Unknown subcommand: {subcommand}")

    def handle_health(self, **_options):
        """Run health checks on AI Assistant infrastructure."""
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS("AI Assistant Configuration & Health"))
        self.stdout.write("=" * 60)
        self.stdout.write("")

        # Display configuration
        self.stdout.write(self.style.SUCCESS("Configuration:"))
        self.stdout.write(f"  AI_ASSISTANT_ENABLED: {config.AI_ASSISTANT_ENABLED}")
        self.stdout.write(
            f"  AI_ASSISTANT_BACKEND_TYPE: {config.AI_ASSISTANT_BACKEND_TYPE}"
        )
        self.stdout.write(f"  AI_ASSISTANT_MODEL: {config.AI_ASSISTANT_MODEL}")

        # Mask the token for security
        if config.AI_ASSISTANT_API_URL:
            self.stdout.write(f"  AI_ASSISTANT_API_URL: {config.AI_ASSISTANT_API_URL}")
        else:
            self.stdout.write(self.style.WARNING("  AI_ASSISTANT_API_URL: [not set]"))

        if config.AI_ASSISTANT_API_TOKEN:
            # Show first 8 chars and mask the rest
            token_preview = (
                config.AI_ASSISTANT_API_TOKEN[:8]
                + "..."
                + config.AI_ASSISTANT_API_TOKEN[-4:]
            )
            self.stdout.write(f"  AI_ASSISTANT_API_TOKEN: {token_preview}")
        else:
            self.stdout.write(self.style.WARNING("  AI_ASSISTANT_API_TOKEN: [not set]"))

        self.stdout.write("")

        # Run health checks
        self.stdout.write(self.style.SUCCESS("Health Checks:"))
        self.stdout.write("")

        health_checks = [
            LLMConfigurationHealthCheck(),
            LLMConnectivityHealthCheck(),
            LLMResponseHealthCheck(),
        ]

        all_passed = True

        for i, health_check in enumerate(health_checks, 1):
            self.stdout.write(f"{i}. {health_check.identifier()}")

            try:
                health_check.run_check()

                if health_check.errors:
                    all_passed = False
                    for error in health_check.errors:
                        self.stdout.write(self.style.ERROR(f"   ✗ {error.message}"))
                else:
                    self.stdout.write(self.style.SUCCESS("   ✓ OK"))

            except Exception as e:
                all_passed = False
                self.stdout.write(self.style.ERROR(f"   ✗ Error: {e}"))

            self.stdout.write("")

        self.stdout.write("=" * 60)
        if all_passed:
            self.stdout.write(self.style.SUCCESS("Health Status: ALL CHECKS PASSED"))
        else:
            self.stdout.write(self.style.ERROR("Health Status: SOME CHECKS FAILED"))
            raise CommandError("Health checks failed")

    def handle_validate_scenarios(self, **_options):
        """Validate scenario YAML files."""
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS("Validation Scenarios Check"))
        self.stdout.write("=" * 60)
        self.stdout.write("")

        # Find scenarios directory
        chat_dir = Path(__file__).parent.parent.parent
        scenarios_dir = chat_dir / "validation_scenarios"

        self.stdout.write(f"Scenarios directory: {scenarios_dir}")
        self.stdout.write("")

        if not scenarios_dir.exists():
            self.stdout.write(
                self.style.ERROR(f"✗ Scenarios directory not found: {scenarios_dir}")
            )
            raise CommandError("Scenarios directory does not exist")

        # Load all scenarios
        try:
            scenarios_by_category = load_all_scenarios(scenarios_dir)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"✗ Failed to load scenarios: {e}"))
            raise CommandError(f"Failed to load scenarios: {e}")

        if not scenarios_by_category:
            self.stdout.write(
                self.style.WARNING("⚠ No scenario files found in directory")
            )
            return

        # Display results
        total_scenarios = 0
        total_inputs = 0

        for category, scenarios in sorted(scenarios_by_category.items()):
            self.stdout.write(self.style.SUCCESS(f"Category: {category}"))

            for scenario in scenarios:
                total_scenarios += 1
                total_inputs += len(scenario.inputs)

                # Show scenario details
                self.stdout.write(f"  ✓ {scenario.name}")
                self.stdout.write(f"    Description: {scenario.description}")
                self.stdout.write(f"    Inputs: {len(scenario.inputs)}")
                self.stdout.write(f"    Evaluations: {len(scenario.evaluations)}")

                # Show evaluation types
                eval_types = [ev.type for ev in scenario.evaluations]
                self.stdout.write(f"    Types: {', '.join(eval_types)}")

            self.stdout.write("")

        # Summary
        self.stdout.write("=" * 60)
        self.stdout.write(
            self.style.SUCCESS(
                f"Validation Status: {total_scenarios} scenarios loaded successfully"
            )
        )
        self.stdout.write(f"  Categories: {len(scenarios_by_category)}")
        self.stdout.write(f"  Total scenarios: {total_scenarios}")
        self.stdout.write(f"  Total test inputs: {total_inputs}")
        self.stdout.write("=" * 60)

    def handle_test_evaluation(self, **options):
        """Test evaluation with real LLM responses."""
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS("LLM Validation Testing"))
        self.stdout.write("=" * 60)
        self.stdout.write("")

        # Get options
        scenario_filter = options.get("scenario")
        verbose = options.get("verbose", False)

        # Find scenarios directory
        chat_dir = Path(__file__).parent.parent.parent
        scenarios_dir = chat_dir / "validation_scenarios"

        # Load scenarios
        try:
            scenarios_by_category = load_all_scenarios(scenarios_dir)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"✗ Failed to load scenarios: {e}"))
            raise CommandError(f"Failed to load scenarios: {e}")

        # Run evaluations
        total_tests = 0
        passed_tests = 0
        total_duration_ms = 0
        results_by_category = {}

        for category, scenarios in sorted(scenarios_by_category.items()):
            if category not in results_by_category:
                results_by_category[category] = {"passed": 0, "failed": 0, "total": 0}

            for scenario in scenarios:
                # Skip if filtering and doesn't match
                if scenario_filter and scenario.name != scenario_filter:
                    continue

                if verbose:
                    self.stdout.write(
                        self.style.SUCCESS(f"\nScenario: {scenario.name} ({category})")
                    )
                    self.stdout.write(f"Description: {scenario.description}")
                    self.stdout.write("")

                # Test each input
                for input_text in scenario.inputs:
                    total_tests += 1
                    results_by_category[category]["total"] += 1

                    if verbose:
                        self.stdout.write(f"  Test: '{input_text}'")

                    # Call LLM
                    try:
                        start_time = time.time()

                        messages = build_context(
                            user=None, user_input=input_text, thread=None
                        )

                        # Use LLMStreamer to get response
                        streamer = LLMStreamer(
                            messages,
                            config.AI_ASSISTANT_API_URL,
                            config.AI_ASSISTANT_API_TOKEN,
                            user=None,  # No tool execution for validation
                            preload_all_tools=True,  # Validation tests all tools
                        )

                        # Iterate through stream to complete the request
                        for _ in streamer:
                            pass

                        # Get the accumulated response
                        llm_response = blocks_to_text(
                            streamer.accumulated_blocks
                        ).strip()

                        duration_ms = int((time.time() - start_time) * 1000)
                        total_duration_ms += duration_ms

                    except Exception as e:
                        self.stdout.write(
                            self.style.ERROR(
                                f"  ✗ {scenario.name}: LLM call failed: {e}"
                            )
                        )
                        results_by_category[category]["failed"] += 1
                        continue

                    # Evaluate response against all criteria
                    test_passed = True
                    failure_messages = []

                    # Build tool_calls list from streamer for tool_usage evaluators
                    api_tool_calls = [
                        {"name": entry["name"]}
                        for entry in streamer.tool_calls.values()
                        if entry.get("name")
                    ]

                    for evaluation in scenario.evaluations:
                        evaluator = get_evaluator(evaluation.type)

                        eval_config = dict(evaluation.config)
                        if evaluation.type == "language":
                            eval_config["input_text"] = input_text
                        elif evaluation.type == "tool_usage":
                            eval_config["tool_calls"] = api_tool_calls

                        result = evaluator.evaluate(llm_response, eval_config)

                        if not result.passed:
                            test_passed = False
                            failure_messages.append(result.message)

                        if verbose and result.passed:
                            self.stdout.write(
                                self.style.SUCCESS(f"    ✓ {result.message}")
                            )

                    if test_passed:
                        passed_tests += 1
                        results_by_category[category]["passed"] += 1
                        if verbose:
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"  ✓ {scenario.name} [{duration_ms}ms]"
                                )
                            )
                    else:
                        results_by_category[category]["failed"] += 1
                        # Always show failures
                        self.stdout.write(
                            self.style.ERROR(f"  ✗ {scenario.name}: {input_text}")
                        )
                        for msg in failure_messages:
                            self.stdout.write(self.style.ERROR(f"     - {msg}"))

                    if verbose:
                        self.stdout.write("")

        # Summary Table
        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS("Summary by Category"))
        self.stdout.write("=" * 60)
        self.stdout.write(
            f"{'Category':<20} {'Passed':>8} {'Failed':>8} {'Total':>8} {'Rate':>8}"
        )
        self.stdout.write("-" * 60)

        for category in sorted(results_by_category.keys()):
            stats = results_by_category[category]
            rate = (stats["passed"] / stats["total"] * 100) if stats["total"] > 0 else 0
            self.stdout.write(
                f"{category:<20} {stats['passed']:>8} {stats['failed']:>8} "
                f"{stats['total']:>8} {rate:>7.1f}%"
            )

        self.stdout.write("-" * 60)
        pass_rate = (passed_tests / total_tests * 100) if total_tests > 0 else 0
        avg_duration = total_duration_ms // total_tests if total_tests > 0 else 0

        self.stdout.write(
            f"{'TOTAL':<20} {passed_tests:>8} {total_tests - passed_tests:>8} "
            f"{total_tests:>8} {pass_rate:>7.1f}%"
        )
        self.stdout.write("=" * 60)
        self.stdout.write(f"Average response time: {avg_duration}ms")
        self.stdout.write(f"Total time: {total_duration_ms / 1000:.1f}s")
        self.stdout.write("=" * 60)

        if passed_tests < total_tests:
            raise CommandError(
                f"Validation failed: {total_tests - passed_tests} test(s) failed"
            )

    def handle_run_all(self, **options):
        """Run all checks in sequence."""
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS("Running All AI Assistant Checks"))
        self.stdout.write("=" * 60)
        self.stdout.write("")

        # Step 1: Health checks
        self.stdout.write(self.style.SUCCESS("Step 1/3: Health Checks"))
        self.stdout.write("")
        try:
            self.handle_health(**options)
        except CommandError as e:
            self.stdout.write(self.style.ERROR(f"Health checks failed: {e}"))
            raise

        self.stdout.write("")
        self.stdout.write("")

        # Step 2: Validate scenarios
        self.stdout.write(self.style.SUCCESS("Step 2/3: Validate Scenarios"))
        self.stdout.write("")
        try:
            self.handle_validate_scenarios(**options)
        except CommandError as e:
            self.stdout.write(self.style.ERROR(f"Scenario validation failed: {e}"))
            raise

        self.stdout.write("")
        self.stdout.write("")

        # Step 3: Test evaluation
        self.stdout.write(self.style.SUCCESS("Step 3/3: Test Evaluation"))
        self.stdout.write("")
        try:
            self.handle_test_evaluation(**options)
        except CommandError as e:
            self.stdout.write(self.style.ERROR(f"Evaluation tests failed: {e}"))
            raise

        # Final summary
        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS("All Checks Passed Successfully! ✓"))
        self.stdout.write("=" * 60)
