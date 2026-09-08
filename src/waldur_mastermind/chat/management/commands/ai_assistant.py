import argparse
import difflib
import json
import logging
import time
from pathlib import Path
from textwrap import dedent

from constance import config
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from waldur_core.core import utils as core_utils
from waldur_mastermind.chat.context_assembler import (
    build_context,
    resolve_prompt_role,
)
from waldur_mastermind.chat.health_checks import (
    LLMConfigurationHealthCheck,
    LLMConnectivityHealthCheck,
    LLMResponseHealthCheck,
)
from waldur_mastermind.chat.llm_streamer import LLMStreamer
from waldur_mastermind.chat.validation.evaluators import get_evaluator
from waldur_mastermind.chat.validation.presets import is_preset_loaded
from waldur_mastermind.chat.validation.scenarios import load_all_scenarios
from waldur_mastermind.chat.validation.turn_blocks import (
    response_text,
    tool_calls_from_blocks,
)


class Command(BaseCommand):
    help = dedent("""
        Check the AI Assistant's configuration and score it against the
        validation scenario packs. The subcommands are listed below; each
        takes --help of its own.

        Examples:
                waldur ai_assistant health
                waldur ai_assistant validate_scenarios
                waldur ai_assistant test_evaluation
                waldur ai_assistant test_evaluation --scenario greeting_no_tool
                waldur ai_assistant test_evaluation --user support --preload-tools
                waldur ai_assistant test_evaluation --preset credit_realistic
                waldur ai_assistant run_all
    """).strip()

    def create_parser(self, prog_name, subcommand, **kwargs):
        # Keep the subcommand list and examples on their own lines; the
        # default formatter reflows ``help`` into one paragraph.
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        return super().create_parser(prog_name, subcommand, **kwargs)

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(
            dest="subcommand",
            help="Available subcommands",
        )

        # Health subcommand
        subparsers.add_parser(
            "health",
            help="Check the LLM configuration, endpoint and a live request",
            description=(
                "Report the configured model, endpoint and (masked) token, "
                "then run three checks: the settings are complete, the "
                "endpoint lists its models, and a completion comes back."
            ),
        )

        # Validate scenarios subcommand
        subparsers.add_parser(
            "validate_scenarios",
            help="Parse the scenario packs and report what they cover",
            description=(
                "Load every YAML pack under chat/validation_scenarios and "
                "print each scenario with its inputs and evaluation types. "
                "Calls no LLM; use it to check a pack parses after editing."
            ),
        )

        # Test evaluation subcommand
        test_eval_parser = subparsers.add_parser(
            "test_evaluation",
            help="Put the scenario packs to the live LLM and score the answers",
            description=(
                "Drive the assistant the way the deployed chat endpoint does "
                "— as a real user (default: staff), tools fetched through "
                "search_tools and executed for real — then score tool "
                "choice, tool arguments and the answer text. Failures print "
                "the tool calls and the response. Scenarios that assert "
                "demo-preset figures are skipped when that preset is absent; "
                "--preset loads it, deleting what is in the database first, "
                "so point DJANGO_SETTINGS_MODULE at a database of its own."
            ),
        )
        test_eval_parser.add_argument(
            "--scenario",
            help="Run only this scenario, by name (e.g. greeting_no_tool)",
        )
        self._add_evaluation_arguments(test_eval_parser)

        # Run all subcommand
        run_all_parser = subparsers.add_parser(
            "run_all",
            help="health, then validate_scenarios, then test_evaluation",
            description=(
                "Run the three checks in order and stop at the first that "
                "fails. Takes the same options as test_evaluation."
            ),
        )
        self._add_evaluation_arguments(run_all_parser)

    @staticmethod
    def _add_evaluation_arguments(parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed output for all tests (default: only failures)",
        )
        parser.add_argument(
            "--user",
            default="staff",
            help=(
                "Username the scenarios run as; every demo preset ships a "
                "'staff' user (default: staff)"
            ),
        )
        parser.add_argument(
            "--preset",
            help=(
                "Demo preset to load before running (e.g. credit_realistic, "
                "which the support_* packs assert against). DESTRUCTIVE: "
                "wipes existing structure data first."
            ),
        )
        parser.add_argument(
            "--wipe-database",
            metavar="NAME",
            help=(
                "Name the database --preset may delete from, instead of "
                "being asked for it. The run stops if it is not the "
                "database DJANGO_SETTINGS_MODULE actually selected"
            ),
        )
        parser.add_argument(
            "--preload-tools",
            action="store_true",
            help=(
                "Expose every tool schema on turn 0 instead of the deployed "
                "search_tools lazy-load path"
            ),
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
        preload_tools = options.get("preload_tools", False)

        if options.get("preset"):
            self._confirm_preset_load(options["preset"], options.get("wipe_database"))
            call_command("demo_presets", "load", options["preset"], "--yes")

        run_user = self._resolve_user(options.get("user") or "staff")
        run_role = resolve_prompt_role(run_user)
        # Name the database: a run that silently landed on the wrong one
        # skips every data-backed pack and looks like a fixture problem.
        self.stdout.write(
            f"Running as {run_user.username} "
            f"(staff={run_user.is_staff}, support={run_user.is_support}, "
            f"scope tier={run_role}) on database "
            f"'{connection.settings_dict['NAME']}'"
        )
        if not (run_user.is_staff or run_user.is_support):
            # Account tools scope every queryset to what the user holds a
            # role on, so a plain user misses preset data it has no access
            # to — a failure about rights, not about the answer.
            self.stdout.write(
                self.style.WARNING(
                    f"'{run_user.username}' is neither staff nor support: account "
                    "tools will only return data this user has a role on, so "
                    "data-backed scenarios can fail for want of access."
                )
            )

        # Scenario loading, the streamer, httpx and the usage recorder all
        # narrate at INFO, which buries the results they are narrating.
        # Warnings and errors still come through.
        if not verbose:
            logging.disable(logging.INFO)

        try:
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
            # A --scenario that matches nothing must not read as a clean run.
            known_names: list[str] = []
            total_skipped = 0
            total_duration_ms = 0
            total_tokens_in = 0
            total_tokens_out = 0
            results_by_category = {}

            preset_states: dict[str, bool] = {}

            def preset_available(name):
                # Memoised after any --preset load, so a scenario whose data was
                # just imported is seen as available.
                if name not in preset_states:
                    preset_states[name] = is_preset_loaded(name)
                return preset_states[name]

            def category_stats(category):
                # Created on first use so a --scenario run does not report a
                # row of zeroes for every pack it never touched.
                if category not in results_by_category:
                    results_by_category[category] = {
                        "passed": 0,
                        "failed": 0,
                        "skipped": 0,
                        "total": 0,
                    }
                    self.stdout.write(self.style.SUCCESS(f"\n{category}"))
                return results_by_category[category]

            for category, scenarios in sorted(scenarios_by_category.items()):
                for scenario in scenarios:
                    known_names.append(scenario.name)
                    # Skip if filtering and doesn't match
                    if scenario_filter and scenario.name != scenario_filter:
                        continue

                    if scenario.scope_tier and scenario.scope_tier != run_role:
                        skipped = len(scenario.inputs)
                        total_skipped += skipped
                        category_stats(category)["skipped"] += skipped
                        self.stdout.write(
                            self.style.WARNING(
                                f"  ⊘ {scenario.name}: written for the "
                                f"{scenario.scope_tier} scope tier, but "
                                f"'{run_user.username}' is served the {run_role} "
                                f"one.{self._tier_hint(scenario.scope_tier)}"
                            )
                        )
                        continue

                    if (
                        scenario.preset
                        and scenario.scope_tier != "end_user"
                        and not (run_user.is_staff or run_user.is_support)
                    ):
                        # Account tools scope every queryset to the roles
                        # the user holds, so a preset's figures are invisible
                        # to anyone else and "not found" becomes the right
                        # answer to a question the pack scores as wrong. An
                        # explicit end_user tier means the author wanted the
                        # scoped view.
                        skipped = len(scenario.inputs)
                        total_skipped += skipped
                        category_stats(category)["skipped"] += skipped
                        self.stdout.write(
                            self.style.WARNING(
                                f"  ⊘ {scenario.name}: asserts preset data only "
                                f"staff or support see in full; "
                                f"'{run_user.username}' is neither."
                                + self._tier_hint("staff")
                            )
                        )
                        continue

                    if scenario.preset and not preset_available(scenario.preset):
                        skipped = len(scenario.inputs)
                        total_skipped += skipped
                        category_stats(category)["skipped"] += skipped
                        self.stdout.write(
                            self.style.WARNING(
                                f"  ⊘ {scenario.name}: needs preset "
                                f"'{scenario.preset}'. Rerun with --preset "
                                f"{scenario.preset} on a database you can wipe."
                            )
                        )
                        continue

                    # Test each input
                    for input_text in scenario.inputs:
                        total_tests += 1
                        category_stats(category)["total"] += 1

                        # Call LLM
                        try:
                            start_time = time.time()

                            messages = build_context(
                                user=run_user, user_input=input_text, thread=None
                            )

                            # Same construction as the authenticated view:
                            # tools execute for real as this user, and arrive
                            # via search_tools unless --preload-tools.
                            streamer = LLMStreamer(
                                messages,
                                config.AI_ASSISTANT_API_URL,
                                config.AI_ASSISTANT_API_TOKEN,
                                user=run_user,
                                preload_all_tools=preload_tools,
                                worker_timeout=config.AI_ASSISTANT_STREAM_TIMEOUT_SECONDS,
                            )

                            # Iterate through stream to complete the request
                            for _ in streamer:
                                pass

                            blocks = streamer.accumulated_blocks
                            llm_response = response_text(blocks)

                            # None until a usage chunk arrives; a turn that
                            # erred before one still has to print a line.
                            tokens_in = streamer.input_tokens or 0
                            tokens_out = streamer.output_tokens or 0
                            total_tokens_in += tokens_in
                            total_tokens_out += tokens_out

                            duration_ms = int((time.time() - start_time) * 1000)
                            total_duration_ms += duration_ms

                        except Exception as e:
                            self.stdout.write(
                                self.style.ERROR(
                                    f"  ✗ {scenario.name}: LLM call failed: {e}"
                                )
                            )
                            category_stats(category)["failed"] += 1
                            continue

                        # Evaluate response against all criteria
                        test_passed = True
                        failure_messages = []
                        passed_messages = []

                        api_tool_calls = tool_calls_from_blocks(blocks)
                        # A call the lazy-load guard refused leaves no block,
                        # so the tools the model reached for and the tools that
                        # ran are two different lists.
                        rejected_calls = list(streamer.rejected_tool_calls)

                        # A worker that died after the tool call still
                        # satisfies every tool assertion; the user saw an
                        # error, so the turn failed.
                        stream_error = streamer.error
                        if stream_error:
                            test_passed = False
                            failure_messages.append(
                                f"the stream ended in an error: {stream_error}"
                            )

                        for evaluation in scenario.evaluations:
                            evaluator = get_evaluator(evaluation.type)

                            eval_config = dict(evaluation.config)
                            if evaluation.type == "language":
                                eval_config["input_text"] = input_text
                            elif evaluation.type in ("tool_usage", "tool_arguments"):
                                eval_config["tool_calls"] = api_tool_calls
                                eval_config["attempted_tool_calls"] = (
                                    api_tool_calls + rejected_calls
                                )

                            result = evaluator.evaluate(llm_response, eval_config)

                            if not result.passed:
                                test_passed = False
                                failure_messages.append(result.message)
                            elif verbose:
                                passed_messages.append(result.message)

                        cost = self._cost(duration_ms, tokens_in, tokens_out)
                        if test_passed:
                            passed_tests += 1
                            category_stats(category)["passed"] += 1
                            self.stdout.write(
                                self.style.SUCCESS(f"  ✓ {scenario.name} {cost}")
                            )
                            if verbose:
                                for msg in passed_messages:
                                    self.stdout.write(
                                        self.style.SUCCESS(f"      passed:   {msg}")
                                    )
                                self._write_transcript(
                                    input_text,
                                    llm_response,
                                    api_tool_calls,
                                    rejected_calls,
                                )
                        else:
                            category_stats(category)["failed"] += 1
                            # A failure is only actionable with the prompt, the
                            # tools and the answer in front of you.
                            self.stdout.write(
                                self.style.ERROR(f"  ✗ {scenario.name} {cost}")
                            )
                            for msg in failure_messages:
                                self.stdout.write(
                                    self.style.ERROR(f"      failed:   {msg}")
                                )
                            self._write_transcript(
                                input_text,
                                llm_response,
                                api_tool_calls,
                                rejected_calls,
                            )
        finally:
            logging.disable(logging.NOTSET)

        # A --scenario nobody wrote scores nothing, and an empty run used to
        # compare 0 passed against 0 total and report success. Raised before
        # the summary: there is no table to read when no scenario ran at all.
        if scenario_filter and scenario_filter not in known_names:
            close = difflib.get_close_matches(scenario_filter, known_names, n=3)
            suggestion = (
                f" Did you mean: {', '.join(close)}?"
                if close
                else " Run 'validate_scenarios' to list them."
            )
            raise CommandError(
                f"No scenario named '{scenario_filter}' in any pack.{suggestion}"
            )

        # Summary Table
        self.stdout.write("")
        self.stdout.write("=" * 70)
        self.stdout.write(self.style.SUCCESS("Summary by Category"))
        self.stdout.write("=" * 70)
        self.stdout.write(
            f"{'Category':<20} {'Passed':>8} {'Failed':>8} {'Skipped':>8} "
            f"{'Total':>8} {'Rate':>8}"
        )
        self.stdout.write("-" * 70)

        def rate_column(passed, total):
            # A wholly skipped category has no rate to report; 0.0% would
            # read as "the assistant failed everything".
            if total == 0:
                return f"{'—':>8}"
            return f"{passed / total * 100:>7.1f}%"

        for category in sorted(results_by_category.keys()):
            stats = results_by_category[category]
            self.stdout.write(
                f"{category:<20} {stats['passed']:>8} {stats['failed']:>8} "
                f"{stats['skipped']:>8} {stats['total']:>8} "
                f"{rate_column(stats['passed'], stats['total'])}"
            )

        self.stdout.write("-" * 70)
        avg_duration = total_duration_ms // total_tests if total_tests > 0 else 0

        self.stdout.write(
            f"{'TOTAL':<20} {passed_tests:>8} {total_tests - passed_tests:>8} "
            f"{total_skipped:>8} {total_tests:>8} "
            f"{rate_column(passed_tests, total_tests)}"
        )
        self.stdout.write("=" * 70)
        if total_skipped:
            self.stdout.write(
                f"Skipped {total_skipped} test(s) this run cannot score; "
                f"see the ⊘ lines above."
            )
        self.stdout.write(f"Average response time: {avg_duration}ms")
        self.stdout.write(f"Total time: {total_duration_ms / 1000:.1f}s")
        if total_tokens_in or total_tokens_out:
            self.stdout.write(f"Tokens: {total_tokens_in} in, {total_tokens_out} out")
        self.stdout.write("=" * 70)

        # Everything the run reached was skipped: the assistant was never
        # measured, so the exit code must not say it passed. The table and
        # the ⊘ lines above say what was skipped and how to make it run.
        if total_tests == 0:
            raise CommandError(
                "Nothing was scored: every scenario this run reached was "
                "skipped (see the ⊘ lines above). Load the data with --preset, "
                "or run as a user of the tier the scenarios were written for "
                "with --user."
            )

        if passed_tests < total_tests:
            raise CommandError(
                f"Validation failed: {total_tests - passed_tests} test(s) failed"
            )

    def _confirm_preset_load(self, preset, wipe_database):
        """Loading a preset deletes structure data, so make it deliberate.

        The target is whichever database DJANGO_SETTINGS_MODULE selected,
        which is exactly the part of the command line easiest to lose when
        it is copied. So the name is stated twice and checked, rather than
        waved through by a bare --yes: a prefix that went missing makes the
        two disagree, and nothing is deleted.
        """
        database = connection.settings_dict["NAME"]
        self.stdout.write(
            self.style.WARNING(
                f"Loading preset '{preset}' DELETES the customers, projects "
                f"and resources in database '{database}'."
            )
        )
        if wipe_database is None:
            try:
                answer = input(f"Type '{database}' to continue: ")
            except EOFError:
                raise CommandError(
                    "No terminal to confirm on. Pass --wipe-database NAME to "
                    "run non-interactively. Nothing was loaded."
                )
            if answer.strip() != database:
                raise CommandError("Aborted, nothing was loaded.")
            return
        if wipe_database != database:
            raise CommandError(
                f"--wipe-database says '{wipe_database}' but this run is "
                f"connected to '{database}'. Nothing was loaded. Check "
                f"DJANGO_SETTINGS_MODULE selects the database you meant."
            )

    @staticmethod
    def _tier_hint(role):
        """Name a user of the wanted tier, so the rerun is copy-pasteable."""
        user_model = get_user_model()
        candidates = user_model.objects.filter(is_active=True)
        if role == "staff":
            candidates = candidates.filter(is_staff=True)
        elif role == "support":
            candidates = candidates.filter(is_support=True, is_staff=False)
        else:
            candidates = candidates.filter(is_staff=False, is_support=False)
        username = candidates.values_list("username", flat=True).first()
        if username:
            return f" Rerun with --user {username}."
        return f" No {role} account exists in this database."

    @staticmethod
    def _format_calls(calls):
        return ", ".join(
            f"{call['name']}({json.dumps(call.get('arguments') or {}, ensure_ascii=False)})"
            for call in calls
        )

    @staticmethod
    def _cost(duration_ms, tokens_in, tokens_out):
        if not (tokens_in or tokens_out):
            return f"[{duration_ms}ms]"
        return f"[{duration_ms}ms · {tokens_in}→{tokens_out} tok]"

    def _write_transcript(
        self, input_text, llm_response, api_tool_calls, rejected_calls=()
    ):
        """Show what the assistant was asked, reached for and answered."""
        calls = self._format_calls(api_tool_calls) or "(no tool calls)"
        self.stdout.write(f"      prompt:   {input_text}")
        self.stdout.write(f"      tools:    {calls}")
        # Refused calls ran nothing and are in no block, so a turn whose
        # only mistake was reaching for an unloaded tool otherwise reads
        # as "no tool calls".
        if rejected_calls:
            self.stdout.write(
                f"      rejected: {self._format_calls(rejected_calls)} "
                "(not loaded in that round)"
            )
        self.stdout.write("      response:")
        for line in (llm_response or "(empty)").splitlines() or ["(empty)"]:
            self.stdout.write(f"        | {line}")

    @staticmethod
    def _resolve_user(username):
        user_model = get_user_model()
        try:
            return user_model.objects.get(username=username)
        except user_model.DoesNotExist:
            # Every tier is a legitimate --user now that scenarios can ask
            # for an end user; only the robot accounts are not.
            candidates = sorted(
                user_model.objects.filter(is_active=True)
                .exclude(username__in=core_utils.ROBOT_USERNAMES)
                .values_list("username", flat=True)
            )
            raise CommandError(
                f"User '{username}' not found in database "
                f"'{connection.settings_dict['NAME']}'. Available: "
                f"{candidates or 'none'}"
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
