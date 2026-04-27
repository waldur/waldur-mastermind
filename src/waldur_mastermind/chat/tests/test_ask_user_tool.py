"""Tests for the universal ask_user meta-tool."""

from django.test import TestCase

from waldur_mastermind.chat.tools.ask_user import AskUserTool
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.ui_registry import ui_registry


def _q(question="Pick one", options=None, **extra):
    """Build a question dict with sensible defaults; ``options=None`` keeps it
    out of the dict so absent-options paths can be exercised."""
    out = {"question": question}
    if options is not None:
        out["options"] = options
    out.update(extra)
    return out


def _opt(label, **extra):
    out = {"label": label}
    out.update(extra)
    return out


class AskUserToolDefinitionTest(TestCase):
    def test_registered_in_registry(self):
        self.assertIn(ToolName.ASK_USER, tool_registry)

    def test_resolved_via_string_lookup(self):
        self.assertIn("ask_user", tool_registry)

    def test_category_is_none(self):
        tool = tool_registry.get(ToolName.ASK_USER)
        self.assertIsNone(tool.definition.category)

    def test_description_mentions_question_range(self):
        tool = tool_registry.get(ToolName.ASK_USER)
        self.assertIn("1", tool.definition.description)
        self.assertIn("4", tool.definition.description)

    def test_usage_instructions_nonempty(self):
        tool = tool_registry.get(ToolName.ASK_USER)
        self.assertNotEqual(tool.definition.usage_instructions.strip(), "")

    def test_input_schema_top_level_requires_questions(self):
        tool = tool_registry.get(ToolName.ASK_USER)
        schema = tool.definition.inputSchema
        self.assertEqual(schema["type"], "object")
        self.assertIn("questions", schema["required"])

    def test_input_schema_caps_questions_between_one_and_four(self):
        tool = tool_registry.get(ToolName.ASK_USER)
        questions = tool.definition.inputSchema["properties"]["questions"]
        self.assertEqual(questions["minItems"], 1)
        self.assertEqual(questions["maxItems"], 4)

    def test_input_schema_caps_options_between_two_and_twenty(self):
        tool = tool_registry.get(ToolName.ASK_USER)
        options = tool.definition.inputSchema["properties"]["questions"]["items"][
            "properties"
        ]["options"]
        self.assertEqual(options["minItems"], 2)
        self.assertEqual(options["maxItems"], 20)


class AskUserToolHappyPathTest(TestCase):
    def setUp(self):
        self.tool = AskUserTool()

    def test_single_question_two_options(self):
        result = self.tool.execute(
            None,
            {
                "questions": [
                    _q("What's your workload?", [_opt("Training"), _opt("Inference")])
                ]
            },
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["ui_component"], "ask_user_form")
        questions = result["ui_data"]["questions"]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["question"], "What's your workload?")
        self.assertEqual(len(questions[0]["options"]), 2)

    def test_four_questions_four_options_each(self):
        questions = [
            _q(
                f"Question {i}?",
                [_opt(f"Opt {i}-{j}") for j in range(4)],
            )
            for i in range(4)
        ]
        result = self.tool.execute(None, {"questions": questions})
        self.assertEqual(result["type"], "success")
        self.assertEqual(len(result["ui_data"]["questions"]), 4)
        for q in result["ui_data"]["questions"]:
            self.assertEqual(len(q["options"]), 4)

    def test_options_omitted_renders_as_freeform(self):
        # No options key at all → free-form text question.
        result = self.tool.execute(
            None, {"questions": [{"question": "What's your hostname?"}]}
        )
        self.assertEqual(result["type"], "success")
        rendered = result["ui_data"]["questions"][0]
        self.assertEqual(rendered["question"], "What's your hostname?")
        self.assertNotIn("options", rendered)

    def test_twenty_options_supported(self):
        # Picker-style use case: many projects/flavors. 20 is the cap.
        opts = [_opt(f"Project {i}") for i in range(20)]
        result = self.tool.execute(None, {"questions": [_q("Pick a project", opts)]})
        self.assertEqual(result["type"], "success")
        self.assertEqual(len(result["ui_data"]["questions"][0]["options"]), 20)

    def test_header_optional_passthrough(self):
        result = self.tool.execute(
            None,
            {
                "questions": [
                    _q(
                        "Pick one",
                        [_opt("A"), _opt("B")],
                        header="Workload",
                    )
                ]
            },
        )
        self.assertEqual(result["ui_data"]["questions"][0]["header"], "Workload")

    def test_description_optional_on_option(self):
        result = self.tool.execute(
            None,
            {
                "questions": [
                    _q(
                        "Pick one",
                        [
                            _opt("Training", description="Multi-GPU runs"),
                            _opt("Inference"),
                        ],
                    )
                ]
            },
        )
        opts = result["ui_data"]["questions"][0]["options"]
        self.assertEqual(opts[0]["description"], "Multi-GPU runs")
        self.assertNotIn("description", opts[1])

    def test_value_optional_on_option(self):
        # `value` lets the LLM carry a UUID alongside a human label
        # (e.g., for project/flavor pickers in the upcoming VM refactor).
        uuid = "a3000000000000000000000000000001"
        result = self.tool.execute(
            None,
            {
                "questions": [
                    _q(
                        "Pick a project",
                        [_opt("Acme / Prod", value=uuid), _opt("Other org")],
                    )
                ]
            },
        )
        opts = result["ui_data"]["questions"][0]["options"]
        self.assertEqual(opts[0]["value"], uuid)
        self.assertNotIn("value", opts[1])

    def test_multiselect_passthrough(self):
        result = self.tool.execute(
            None,
            {
                "questions": [
                    _q(
                        "Which regions?",
                        [_opt("EU"), _opt("US")],
                        multiSelect=True,
                    )
                ]
            },
        )
        self.assertTrue(result["ui_data"]["questions"][0]["multiSelect"])

    def test_multiselect_defaults_to_false(self):
        result = self.tool.execute(
            None,
            {"questions": [_q("Pick", [_opt("A"), _opt("B")])]},
        )
        self.assertFalse(result["ui_data"]["questions"][0]["multiSelect"])

    def test_context_passed_through(self):
        result = self.tool.execute(
            None,
            {
                "questions": [_q("Pick", [_opt("A"), _opt("B")])],
                "context": "To recommend a GPU offering, I need:",
            },
        )
        self.assertEqual(
            result["ui_data"]["context"],
            "To recommend a GPU offering, I need:",
        )

    def test_context_truncated_to_400_chars(self):
        long_context = "x" * 600
        result = self.tool.execute(
            None,
            {
                "questions": [_q("Pick", [_opt("A"), _opt("B")])],
                "context": long_context,
            },
        )
        self.assertLessEqual(len(result["ui_data"]["context"]), 400)

    def test_summary_mentions_question_count(self):
        result = self.tool.execute(
            None,
            {
                "questions": [
                    _q("Question one?", [_opt("A"), _opt("B")]),
                    _q("Question two?", [_opt("C"), _opt("D")]),
                ]
            },
        )
        self.assertIn("2", result["summary"])

    def test_backend_assigns_stable_ids(self):
        result = self.tool.execute(
            None,
            {
                "questions": [
                    _q("Question one?", [_opt("A"), _opt("B")]),
                    _q("Question two?", [_opt("C"), _opt("D"), _opt("E")]),
                ]
            },
        )
        questions = result["ui_data"]["questions"]
        self.assertEqual(questions[0]["id"], "q0")
        self.assertEqual(questions[1]["id"], "q1")
        self.assertEqual(questions[0]["options"][0]["id"], "q0o0")
        self.assertEqual(questions[0]["options"][1]["id"], "q0o1")
        self.assertEqual(questions[1]["options"][2]["id"], "q1o2")

    def test_allow_free_text_defaults_true_when_omitted(self):
        result = self.tool.execute(
            None,
            {"questions": [_q("Pick one", [_opt("A"), _opt("B")])]},
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["ui_data"]["questions"][0]["allowFreeText"], True)

    def test_allow_free_text_explicit_false_round_trips(self):
        result = self.tool.execute(
            None,
            {
                "questions": [
                    _q(
                        "Pick a flavor",
                        [_opt("small"), _opt("medium")],
                        allowFreeText=False,
                    )
                ]
            },
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["ui_data"]["questions"][0]["allowFreeText"], False)

    def test_allow_free_text_wrong_type_rejected(self):
        result = self.tool.execute(
            None,
            {
                "questions": [
                    _q(
                        "Pick a flavor",
                        [_opt("small"), _opt("medium")],
                        allowFreeText="yes",
                    )
                ]
            },
        )
        self.assertEqual(result["type"], "validation_error")
        self.assertIn("allowFreeText", result["summary"])


class AskUserToolValidationErrorTest(TestCase):
    """The tool runs belt-and-suspenders shape checks inside execute() so
    bad LLM-supplied args produce a friendly markdown rejection instead of
    crashing or silently passing through. The validation_error type makes
    the rejection visible to the LLM in the next round so it can self-correct.
    """

    def setUp(self):
        self.tool = AskUserTool()

    def _assert_rejection(self, arguments):
        result = self.tool.execute(None, arguments)
        self.assertEqual(result["type"], "validation_error")
        self.assertEqual(result["ui_component"], "markdown")
        return result

    def test_zero_questions_rejected(self):
        self._assert_rejection({"questions": []})

    def test_five_questions_rejected(self):
        questions = [_q(f"Q{i}", [_opt("A"), _opt("B")]) for i in range(5)]
        self._assert_rejection({"questions": questions})

    def test_questions_missing_rejected(self):
        self._assert_rejection({})

    def test_questions_as_dict_rejected(self):
        self._assert_rejection({"questions": {"q0": "Q1"}})

    def test_one_option_rejected(self):
        self._assert_rejection({"questions": [_q("Pick", [_opt("Only one")])]})

    def test_twenty_one_options_rejected(self):
        opts = [_opt(f"Opt {i}") for i in range(21)]
        self._assert_rejection({"questions": [_q("Pick", opts)]})

    def test_missing_question_field_rejected(self):
        self._assert_rejection({"questions": [{"options": [_opt("A"), _opt("B")]}]})

    def test_non_boolean_multiselect_rejected(self):
        self._assert_rejection(
            {"questions": [_q("Pick", [_opt("A"), _opt("B")], multiSelect="yes")]}
        )

    def test_options_as_plain_strings_rejected(self):
        # Some models try to send ["Opt A", "Opt B"] instead of
        # [{"label": "Opt A"}, ...].
        self._assert_rejection({"questions": [_q("Pick", ["Opt A", "Opt B"])]})

    def test_label_missing_on_option_rejected(self):
        self._assert_rejection(
            {"questions": [_q("Pick", [{"description": "no label"}, _opt("B")])]}
        )

    def test_duplicate_option_labels_deduped_case_insensitive(self):
        # Same label twice (case-different) should collapse to one. With
        # only one unique remaining, < 2 options => rejection.
        self._assert_rejection({"questions": [_q("Pick", [_opt("Yes"), _opt("YES")])]})

    def test_duplicate_label_with_third_unique_keeps_two(self):
        # Three options, two are dupes → dedupes to 2 → still valid.
        result = self.tool.execute(
            None,
            {
                "questions": [
                    _q(
                        "Pick",
                        [_opt("Yes"), _opt("YES"), _opt("No")],
                    )
                ]
            },
        )
        self.assertEqual(result["type"], "success")
        labels = [o["label"] for o in result["ui_data"]["questions"][0]["options"]]
        self.assertEqual(len(labels), 2)

    def test_question_too_short_rejected(self):
        # minLength=4
        self._assert_rejection({"questions": [_q("Hi", [_opt("A"), _opt("B")])]})

    def test_label_too_long_rejected(self):
        long_label = "x" * 81
        self._assert_rejection(
            {"questions": [_q("Pick", [_opt(long_label), _opt("B")])]}
        )

    def test_questions_as_string_rejected(self):
        self._assert_rejection({"questions": "What's your name?"})


class AskUserToolUiComponentRegistrationTest(TestCase):
    """The tool's emitted ui_component name must be a registered block
    kind, otherwise the parser falls back to markdown and the form never
    renders."""

    def test_ask_user_form_is_a_registered_ui_component(self):
        self.assertIsNotNone(ui_registry.get("ask_user_form"))
