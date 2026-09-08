"""Tests for ``waldur ai_assistant test_evaluation``.

The LLM is mocked. These tests pin down who the harness runs as (a real
user through the production tool path) and what it feeds the evaluators:
every tool call of the turn (not only the last round's), tool arguments,
and the text of an ``ask_user`` form.
"""

import json
import logging
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.context_assembler import resolve_prompt_role
from waldur_mastermind.chat.validation.presets import is_preset_loaded
from waldur_mastermind.chat.validation.scenarios import Scenario
from waldur_mastermind.chat.validation.turn_blocks import ask_user_text

_COMMAND_MODULE = "waldur_mastermind.chat.management.commands.ai_assistant"


class _FakeStreamer:
    """Streams nothing; exposes canned blocks and records init kwargs."""

    instances: list["_FakeStreamer"] = []
    blocks: list[dict] = []
    # What the real streamer leaves in ``tool_calls`` after a turn that
    # ends with a text round: nothing.
    last_round_tool_calls: dict = {}
    # What the real streamer sets when the worker thread died.
    error: str | None = None
    # Calls the lazy-load guard refused: no block, nothing executed.
    rejected_tool_calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.accumulated_blocks = list(_FakeStreamer.blocks)
        self.tool_calls = dict(_FakeStreamer.last_round_tool_calls)
        self.rejected_tool_calls = list(_FakeStreamer.rejected_tool_calls)
        self.error = _FakeStreamer.error
        # None until a usage chunk arrives, like the real streamer.
        self.input_tokens = None
        self.output_tokens = None
        _FakeStreamer.instances.append(self)

    def __iter__(self):
        return iter(())


def _tool_block(name, arguments=None, result=None):
    return {
        "id": "blk_1",
        "key": "tool",
        "status": "complete",
        "tool": {"call_id": "call_1", "name": name, "arguments": arguments or {}},
        "result": result or {"id": "blk_1_r", "key": "markdown", "content": ""},
    }


def _markdown(text):
    return {"id": "blk_2", "key": "markdown", "status": "complete", "content": text}


def _ask_user_form(*questions, context=None):
    block = {
        "id": "blk_1_r",
        "key": "ask_user_form",
        "status": "complete",
        "questions": [{"id": f"q{i}", "question": q} for i, q in enumerate(questions)],
    }
    if context:
        block["context"] = context
    return block


class AskUserTextTest(SimpleTestCase):
    def test_option_labels_are_part_of_what_the_user_saw(self):
        # "What can I order?" answered as a form whose options ARE the
        # answer; a data_match on the category name must see them.
        form = _ask_user_form("Which area?", context="We offer:")
        form["questions"][0]["options"] = [
            {"label": "Virtual machines", "value": "e30", "description": "VMs"},
        ]

        text = ask_user_text([form])

        self.assertIn("We offer:", text)
        self.assertIn("Which area?", text)
        self.assertIn("Virtual machines", text)
        self.assertIn("VMs", text)


class TestEvaluationScoringTest(TestCase):
    def setUp(self):
        _FakeStreamer.instances = []
        _FakeStreamer.blocks = []
        _FakeStreamer.last_round_tool_calls = {}
        _FakeStreamer.rejected_tool_calls = []
        _FakeStreamer.error = None
        self.staff = structure_factories.UserFactory(username="staff", is_staff=True)

    def _run(self, scenario, blocks, *args, presets_loaded=(), scenarios_error=None):
        _FakeStreamer.blocks = blocks
        # (call_command args, streamers built so far) per preset load.
        self.preset_loads = []
        self.presets_loaded = set(presets_loaded)
        config_attrs = {
            "AI_ASSISTANT_API_URL": "https://example.com/v1",
            "AI_ASSISTANT_API_TOKEN": "tok",
            "AI_ASSISTANT_STREAM_TIMEOUT_SECONDS": 123,
        }
        with (
            mock.patch(f"{_COMMAND_MODULE}.LLMStreamer", _FakeStreamer),
            mock.patch(
                f"{_COMMAND_MODULE}.build_context", return_value=[]
            ) as self.build_context,
            mock.patch(
                f"{_COMMAND_MODULE}.call_command", side_effect=self._load_preset
            ),
            mock.patch(
                f"{_COMMAND_MODULE}.is_preset_loaded",
                side_effect=lambda name: name in self.presets_loaded,
            ),
            mock.patch(
                f"{_COMMAND_MODULE}.load_all_scenarios",
                return_value={scenario.category: [scenario]},
                side_effect=scenarios_error,
            ),
            mock.patch(f"{_COMMAND_MODULE}.config", **config_attrs),
        ):
            out = mock.MagicMock()
            try:
                call_command("ai_assistant", "test_evaluation", *args, stdout=out)
            finally:
                # Kept on self so tests that expect a CommandError can still
                # read what the run printed.
                self.output = "".join(
                    str(call.args[0]) for call in out.write.call_args_list
                )
        return self.output

    def _run_all_skipped(self, *args, **kwargs):
        """Run something that skips every scenario it reaches.

        Skipping is still not a failure — no scenario is scored against the
        assistant — but a run that scored nothing is not a pass either, so
        the command exits non-zero. Tests keep asserting the skip itself.
        """
        with self.assertRaisesMessage(CommandError, "Nothing was scored"):
            self._run(*args, **kwargs)
        return self.output

    @staticmethod
    def _no_tool_scenario(preset=None, scope_tier=None):
        return TestEvaluationScoringTest._scenario(
            {"type": "tool_usage", "config": {"expected_tool": None}},
            preset=preset,
            scope_tier=scope_tier,
        )

    def _load_preset(self, *args, **kwargs):
        self.preset_loads.append((args, len(_FakeStreamer.instances)))
        self.presets_loaded.add(args[2])

    @staticmethod
    def _scenario(evaluation, preset=None, scope_tier=None):
        return Scenario(
            name="probe",
            category="harness",
            description="",
            inputs=["hi"],
            evaluations=[evaluation],
            preset=preset,
            scope_tier=scope_tier,
        )

    def test_tool_called_in_earlier_round_satisfies_expected_tool(self):
        # search_offerings ran in round 0; round 1 was plain text, so the
        # streamer's per-round ``tool_calls`` is empty when the turn ends.
        scenario = self._scenario(
            {"type": "tool_usage", "config": {"expected_tool": "search_offerings"}}
        )
        self._run(scenario, [_tool_block("search_offerings"), _markdown("Here.")])

    def test_tool_arguments_evaluator_receives_arguments(self):
        scenario = self._scenario(
            {
                "type": "tool_arguments",
                "config": {
                    "tool": "get_offering",
                    "args_must_contain": {"name": "HPC"},
                },
            }
        )
        self._run(
            scenario,
            [_tool_block("get_offering", {"name": "HPC cluster"}), _markdown("Here.")],
        )

    def test_ask_user_question_text_is_scored(self):
        scenario = self._scenario(
            {"type": "pattern", "config": {"required_patterns": ["(?i)gpu"]}}
        )
        self._run(
            scenario,
            [
                _tool_block(
                    "ask_user",
                    {"questions": [{"question": "Do you need a GPU?"}]},
                    result=_ask_user_form("Do you need a GPU?", context="Quick check"),
                )
            ],
        )

    def test_top_level_ask_user_form_block_is_scored(self):
        # A provider that omits call_id leaves the form as a top-level
        # block with no tool metadata; its text must still count.
        scenario = self._scenario(
            {"type": "pattern", "config": {"required_patterns": ["(?i)gpu"]}}
        )
        self._run(scenario, [_ask_user_form("Do you need a GPU?")])

    def test_empty_response_still_fails_required_pattern(self):
        scenario = self._scenario(
            {"type": "pattern", "config": {"required_patterns": ["(?i)gpu"]}}
        )
        with self.assertRaises(CommandError):
            self._run(scenario, [])

    def test_runs_as_staff_user_through_production_tool_path(self):
        # Default identity is the ``staff`` user every demo preset ships;
        # the context and the streamer both see the real User, and tools
        # are lazy-loaded via search_tools exactly like the deployed view.
        self._run(self._no_tool_scenario(), [_markdown("Hello")])
        self.assertEqual(self.build_context.call_args.kwargs["user"], self.staff)
        kwargs = _FakeStreamer.instances[0].init_kwargs
        self.assertEqual(kwargs.get("user"), self.staff)
        self.assertFalse(kwargs.get("preload_all_tools", False))
        self.assertEqual(kwargs.get("worker_timeout"), 123)

    def test_user_option_picks_that_user(self):
        support = structure_factories.UserFactory(username="support", is_support=True)
        self._run(self._no_tool_scenario(), [_markdown("Hello")], "--user", "support")
        self.assertEqual(_FakeStreamer.instances[0].init_kwargs.get("user"), support)

    def test_unknown_user_names_the_candidates(self):
        # A tier skip recommends an end user; the error for a bad --user
        # must offer them too, and not the system account.
        structure_factories.UserFactory(username="acme_member")
        structure_factories.UserFactory(username="system_robot")
        with self.assertRaisesMessage(CommandError, "staff") as ctx:
            self._run(
                self._no_tool_scenario(), [_markdown("Hello")], "--user", "nobody"
            )
        self.assertIn("acme_member", str(ctx.exception))
        self.assertNotIn("system_robot", str(ctx.exception))
        self.assertFalse(_FakeStreamer.instances)

    def test_preload_tools_is_opt_in(self):
        self._run(self._no_tool_scenario(), [_markdown("Hello")], "--preload-tools")
        self.assertTrue(_FakeStreamer.instances[0].init_kwargs.get("preload_all_tools"))

    def test_preset_is_loaded_before_the_first_scenario(self):
        self._run(
            self._no_tool_scenario(),
            [_markdown("Hello")],
            "--preset",
            "credit_realistic",
            "--wipe-database",
            connection.settings_dict["NAME"],
        )
        self.assertEqual(
            self.preset_loads,
            [(("demo_presets", "load", "credit_realistic", "--yes"), 0)],
        )

    def test_no_preset_loads_nothing(self):
        self._run(self._no_tool_scenario(), [_markdown("Hello")])
        self.assertEqual(self.preset_loads, [])

    def test_scenario_whose_preset_is_absent_is_skipped_not_failed(self):
        # Missing fixture data is a fact about the database, not about the
        # assistant; it must not be reported as a model failure.
        output = self._run_all_skipped(
            self._no_tool_scenario(preset="credit_realistic"), [_markdown("Hello")]
        )
        self.assertFalse(_FakeStreamer.instances)
        self.assertIn("credit_realistic", output)
        self.assertIn("Skipped", output)

    def test_scenario_runs_when_its_preset_is_loaded(self):
        self._run(
            self._no_tool_scenario(preset="credit_realistic"),
            [_markdown("Hello")],
            presets_loaded=("credit_realistic",),
        )
        self.assertTrue(_FakeStreamer.instances)

    def test_preset_option_loads_the_data_the_scenario_needs(self):
        self._run(
            self._no_tool_scenario(preset="credit_realistic"),
            [_markdown("Hello")],
            "--preset",
            "credit_realistic",
            "--wipe-database",
            connection.settings_dict["NAME"],
        )
        self.assertEqual(
            self.preset_loads,
            [(("demo_presets", "load", "credit_realistic", "--yes"), 0)],
        )
        self.assertTrue(_FakeStreamer.instances)

    def test_scenario_needing_another_preset_is_still_skipped(self):
        self._run_all_skipped(
            self._no_tool_scenario(preset="credit_realistic"),
            [_markdown("Hello")],
            "--preset",
            "minimal_quickstart",
            "--wipe-database",
            connection.settings_dict["NAME"],
        )
        self.assertFalse(_FakeStreamer.instances)

    def test_wipe_database_must_name_the_database_in_use(self):
        # The guard that matters: DJANGO_SETTINGS_MODULE decides which
        # database --preset deletes from. If it is missing or wrong, the
        # names disagree and nothing is loaded.
        with self.assertRaisesMessage(CommandError, "waldur_support_validation"):
            self._run(
                self._no_tool_scenario(preset="credit_realistic"),
                [_markdown("Hello")],
                "--preset",
                "credit_realistic",
                "--wipe-database",
                "waldur_support_validation",
            )
        self.assertEqual(self.preset_loads, [])
        self.assertFalse(_FakeStreamer.instances)

    def test_wipe_database_naming_the_current_database_proceeds(self):
        self._run(
            self._no_tool_scenario(preset="credit_realistic"),
            [_markdown("Hello")],
            "--preset",
            "credit_realistic",
            "--wipe-database",
            connection.settings_dict["NAME"],
        )
        self.assertTrue(self.preset_loads)

    def test_preset_scenario_skips_for_a_user_who_cannot_see_the_data(self):
        # Account tools scope every queryset to what the user holds a role
        # on, so a preset's Bluewave figures are invisible to an Acme
        # member and every assertion on them fails for want of access.
        structure_factories.UserFactory(username="acme_member")
        output = self._run_all_skipped(
            self._no_tool_scenario(preset="credit_realistic"),
            [_markdown("Hello")],
            "--user",
            "acme_member",
            presets_loaded=("credit_realistic",),
        )
        self.assertFalse(_FakeStreamer.instances)
        self.assertIn("--user staff", output)
        self.assertIn("Skipped", output)

    def test_preset_scenario_written_for_an_end_user_still_runs(self):
        # An explicit end_user tier says the author meant a scoped view.
        structure_factories.UserFactory(username="acme_member")
        self._run(
            self._no_tool_scenario(preset="credit_realistic", scope_tier="end_user"),
            [_markdown("Hello")],
            "--user",
            "acme_member",
            presets_loaded=("credit_realistic",),
        )
        self.assertTrue(_FakeStreamer.instances)

    def test_support_user_sees_preset_data_and_runs(self):
        structure_factories.UserFactory(username="helper", is_support=True)
        self._run(
            self._no_tool_scenario(preset="credit_realistic"),
            [_markdown("Hello")],
            "--user",
            "helper",
            presets_loaded=("credit_realistic",),
        )
        self.assertTrue(_FakeStreamer.instances)

    def test_a_stream_that_died_fails_even_when_the_tool_was_right(self):
        # The right tool was called, then the worker crashed serialising
        # its result. Tool-only assertions all pass; the user got nothing.
        _FakeStreamer.error = "Object of type Decimal is not JSON serializable"
        scenario = self._scenario(
            {"type": "tool_usage", "config": {"expected_tool": "get_resource_usage"}}
        )

        with self.assertRaises(CommandError):
            self._run(scenario, [_tool_block("get_resource_usage")])

        self.assertNotIn("✓", self.output)
        self.assertIn("✗ probe", self.output)
        self.assertIn("Decimal is not JSON serializable", self.output)

    def test_failure_shows_the_response_and_the_tool_calls(self):
        # A failing scenario is only actionable if you can see what the
        # assistant actually said and which tools it reached for.
        scenario = self._scenario(
            {"type": "pattern", "config": {"required_patterns": ["(?i)gpu"]}}
        )
        with self.assertRaises(CommandError):
            self._run(
                scenario,
                [
                    _tool_block("search_offerings", {"query": "hpc"}),
                    _markdown("Nothing matches."),
                ],
            )
        self.assertIn("search_offerings", self.output)
        self.assertIn("hpc", self.output)
        self.assertIn("Nothing matches.", self.output)

    def test_failure_says_so_when_nothing_was_produced(self):
        scenario = self._scenario(
            {"type": "pattern", "config": {"required_patterns": ["(?i)gpu"]}}
        )
        with self.assertRaises(CommandError):
            self._run(scenario, [])
        self.assertIn("(no tool calls)", self.output)
        self.assertIn("(empty)", self.output)

    def test_preset_load_is_refused_without_confirmation(self):
        with mock.patch("builtins.input", return_value=""):
            with self.assertRaisesMessage(CommandError, "Aborted"):
                self._run(
                    self._no_tool_scenario(preset="credit_realistic"),
                    [_markdown("Hello")],
                    "--preset",
                    "credit_realistic",
                )
        self.assertEqual(self.preset_loads, [])

    def test_a_scenario_loading_failure_does_not_leave_logging_off(self):
        # INFO is silenced for the run; a pack that fails to parse raised
        # before the block that switched it back on. A regression must not
        # take every later test in the process down with it.
        self.addCleanup(logging.disable, logging.NOTSET)
        with self.assertRaisesMessage(CommandError, "Failed to load scenarios"):
            self._run(
                self._no_tool_scenario(),
                [_markdown("Hello")],
                scenarios_error=RuntimeError("bad yaml"),
            )
        self.assertEqual(logging.root.manager.disable, logging.NOTSET)

    def test_preset_load_without_a_terminal_names_the_flag(self):
        # No stdin to answer from (CI, a pipe): the run must stop with the
        # non-interactive way in, not a traceback.
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaisesMessage(CommandError, "--wipe-database"):
                self._run(
                    self._no_tool_scenario(preset="credit_realistic"),
                    [_markdown("Hello")],
                    "--preset",
                    "credit_realistic",
                )
        self.assertEqual(self.preset_loads, [])

    def test_preset_load_proceeds_when_the_database_name_is_typed(self):
        with mock.patch(
            "builtins.input", return_value=connection.settings_dict["NAME"]
        ):
            self._run(
                self._no_tool_scenario(preset="credit_realistic"),
                [_markdown("Hello")],
                "--preset",
                "credit_realistic",
            )
        self.assertEqual(len(self.preset_loads), 1)

    def test_warns_when_the_run_user_is_neither_staff_nor_support(self):
        # Account tools scope every queryset to what the user has a role
        # on, so a plain user fails data assertions for want of access.
        structure_factories.UserFactory(username="plain")
        output = self._run(
            self._no_tool_scenario(), [_markdown("Hello")], "--user", "plain"
        )
        self.assertIn("neither staff nor support", output)

    def test_no_rights_warning_for_a_support_user(self):
        structure_factories.UserFactory(username="helper", is_support=True)
        output = self._run(
            self._no_tool_scenario(), [_markdown("Hello")], "--user", "helper"
        )
        self.assertNotIn("neither staff nor support", output)

    def test_scenario_for_another_scope_tier_is_skipped_not_failed(self):
        # off_topic_programming asserts the end_user scope tier, which
        # forbids programming help. The staff tier grants it, so scoring a
        # staff run against that scenario measures the tier, not the model.
        output = self._run_all_skipped(
            self._no_tool_scenario(scope_tier="end_user"), [_markdown("Hello")]
        )
        self.assertFalse(_FakeStreamer.instances)
        self.assertIn("end_user", output)
        self.assertIn("Skipped", output)

    def test_scenario_runs_when_the_user_matches_its_scope_tier(self):
        structure_factories.UserFactory(username="member")
        self._run(
            self._no_tool_scenario(scope_tier="end_user"),
            [_markdown("Hello")],
            "--user",
            "member",
        )
        self.assertTrue(_FakeStreamer.instances)

    def test_scope_tier_skip_names_a_user_of_that_tier(self):
        structure_factories.UserFactory(username="member")
        output = self._run_all_skipped(
            self._no_tool_scenario(scope_tier="end_user"), [_markdown("Hello")]
        )
        self.assertIn("--user member", output)

    def test_scenario_without_a_scope_tier_runs_as_any_user(self):
        self._run(self._no_tool_scenario(), [_markdown("Hello")])
        self.assertTrue(_FakeStreamer.instances)

    def test_a_forbidden_tool_the_guard_refused_still_fails_the_turn(self):
        # The model reached for create_vm before search_tools had loaded it.
        # Nothing ran and no block exists, so the executed-call list looks
        # clean — but reaching for it is the failure the scenario asserts.
        _FakeStreamer.rejected_tool_calls = [
            {"name": "create_vm", "arguments": {"project": "Acme"}}
        ]
        scenario = self._scenario(
            {
                "type": "tool_usage",
                "config": {
                    "expected_tool": "plan_vm",
                    "forbidden_tools": ["create_vm"],
                },
            }
        )

        with self.assertRaises(CommandError):
            self._run(scenario, [_tool_block("plan_vm"), _markdown("Here.")])

        self.assertIn("create_vm", self.output)
        # ...and the transcript says it never ran, so the failure is readable.
        self.assertIn("rejected:", self.output)

    def test_an_unmatched_scenario_filter_is_an_error(self):
        # An empty run used to compare 0 passed against 0 total and report
        # success, so a typo in --scenario looked like a clean pass.
        with self.assertRaisesMessage(CommandError, "prob") as ctx:
            self._run(
                self._no_tool_scenario(), [_markdown("Hello")], "--scenario", "prob"
            )

        self.assertIn("probe", str(ctx.exception))
        self.assertFalse(_FakeStreamer.instances)

    def test_a_run_that_scored_nothing_names_the_way_out(self):
        # Everything skipped means the assistant was never measured; the
        # error has to say what would make the run scorable.
        with self.assertRaisesMessage(CommandError, "Nothing was scored") as ctx:
            self._run(
                self._no_tool_scenario(preset="credit_realistic"), [_markdown("Hello")]
            )

        self.assertIn("--preset", str(ctx.exception))
        self.assertIn("--user", str(ctx.exception))


class ScopeTierTest(TestCase):
    """The harness must read the prompt tier the way build_context does."""

    def test_tier_matches_the_prompt_the_user_would_get(self):
        staff = structure_factories.UserFactory(is_staff=True)
        support = structure_factories.UserFactory(is_support=True)
        member = structure_factories.UserFactory()
        self.assertEqual(resolve_prompt_role(staff), "staff")
        self.assertEqual(resolve_prompt_role(support), "support")
        self.assertEqual(resolve_prompt_role(member), "end_user")

    def test_staff_wins_over_support(self):
        both = structure_factories.UserFactory(is_staff=True, is_support=True)
        self.assertEqual(resolve_prompt_role(both), "staff")

    def test_no_user_is_an_end_user(self):
        self.assertEqual(resolve_prompt_role(None), "end_user")


class PresetDetectionTest(TestCase):
    """``is_preset_loaded`` decides whether a data-backed pack can run."""

    def setUp(self):
        self.preset = {
            "customers": [
                {"uuid": uuid.uuid4().hex, "name": "First"},
                {"uuid": uuid.uuid4().hex, "name": "Second"},
            ]
        }
        path = Path(tempfile.mkdtemp()) / "probe.json"
        path.write_text(json.dumps(self.preset))
        patcher = mock.patch(
            "waldur_mastermind.chat.validation.presets.DemoPresetManager"
            ".get_preset_path",
            return_value=path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _create(self, index):
        structure_factories.CustomerFactory(
            uuid=self.preset["customers"][index]["uuid"]
        )

    def test_absent_when_no_customer_is_present(self):
        self.assertFalse(is_preset_loaded("probe"))

    def test_absent_when_only_part_of_the_preset_is_present(self):
        self._create(0)
        self.assertFalse(is_preset_loaded("probe"))

    def test_present_when_every_customer_is_present(self):
        self._create(0)
        self._create(1)
        self.assertTrue(is_preset_loaded("probe"))

    def test_unknown_preset_is_not_loaded(self):
        with mock.patch(
            "waldur_mastermind.chat.validation.presets.DemoPresetManager"
            ".get_preset_path",
            return_value=None,
        ):
            self.assertFalse(is_preset_loaded("nope"))
