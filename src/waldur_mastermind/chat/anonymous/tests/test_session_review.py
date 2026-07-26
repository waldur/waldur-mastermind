"""Tests for the LLM-as-judge session review.

Three layers:

  * ``parse_judge_json`` — deterministic, no DB; covers the failure
    modes the judge LLM hits in practice (markdown fences, missing
    fields, type coercion).
  * ``build_transcript`` / ``collect_tool_results_from_blocks`` —
    pure shape tests on persisted block dicts.
  * ``review_completed_sessions`` task — picking, batching, budget
    exhaustion, idempotency. The LLM call is patched; the *task
    behaviour* is what matters here.
"""

import json
from datetime import timedelta
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat import tasks
from waldur_mastermind.chat.anonymous import judge
from waldur_mastermind.chat.anonymous import models as anonymous_models


def _verdict_dict(**overrides):
    base = {
        "resolution_score": 4,
        "intent_category": "compute",
        "hallucination_detected": False,
        "hallucination_details": "",
        "summary": "User asked about GPU clusters.",
    }
    base.update(overrides)
    return base


class ParseJudgeJsonTest(SimpleTestCase):
    def test_happy_path(self):
        v = judge.parse_judge_json(json.dumps(_verdict_dict()))
        self.assertIsNotNone(v)
        self.assertEqual(v.resolution_score, 4)
        self.assertEqual(v.intent_category, "compute")
        self.assertFalse(v.hallucination_detected)

    def test_strips_markdown_code_fence(self):
        wrapped = f"```json\n{json.dumps(_verdict_dict())}\n```"
        self.assertIsNotNone(judge.parse_judge_json(wrapped))

    def test_strips_leading_and_trailing_prose(self):
        # Models sometimes add a "Here is the verdict:" preamble despite
        # the explicit instruction not to.
        body = (
            "Here is the verdict for the session you asked about:\n"
            f"{json.dumps(_verdict_dict())}\n"
            "Hope this helps!"
        )
        self.assertIsNotNone(judge.parse_judge_json(body))

    def test_returns_none_on_invalid_score(self):
        self.assertIsNone(
            judge.parse_judge_json(json.dumps(_verdict_dict(resolution_score=7)))
        )

    def test_unknown_intent_accepted_when_no_rubric_passed(self):
        # parse_judge_json without valid_intents accepts any non-empty
        # short slug — the deployment-derived rubric is the source of
        # truth, and it's checked separately by passing valid_intents.
        v = judge.parse_judge_json(json.dumps(_verdict_dict(intent_category="lasagna")))
        self.assertIsNotNone(v)
        self.assertEqual(v.intent_category, "lasagna")

    def test_unknown_intent_coerced_to_unclear_when_rubric_provided(self):
        # When the caller passes the deployment rubric, off-rubric
        # intents are coerced to 'unclear' rather than dropped — losing
        # the whole verdict to one off-rubric string would burn the
        # judge budget for nothing.
        v = judge.parse_judge_json(
            json.dumps(_verdict_dict(intent_category="lasagna")),
            valid_intents={"compute", "storage", "unclear"},
        )
        self.assertIsNotNone(v)
        self.assertEqual(v.intent_category, "unclear")

    def test_known_intent_passes_rubric_check(self):
        v = judge.parse_judge_json(
            json.dumps(_verdict_dict(intent_category="compute")),
            valid_intents={"compute", "storage", "unclear"},
        )
        self.assertIsNotNone(v)
        self.assertEqual(v.intent_category, "compute")

    def test_returns_none_on_missing_summary(self):
        d = _verdict_dict()
        del d["summary"]
        self.assertIsNone(judge.parse_judge_json(json.dumps(d)))

    def test_returns_none_on_non_bool_hallucination(self):
        self.assertIsNone(
            judge.parse_judge_json(
                json.dumps(_verdict_dict(hallucination_detected="yes"))
            )
        )

    def test_returns_none_on_empty(self):
        self.assertIsNone(judge.parse_judge_json(""))
        self.assertIsNone(judge.parse_judge_json("not even close to JSON"))


class IntentRubricTest(TestCase):
    """build_intent_rubric derives slugs from visible marketplace.Category rows.

    Three deployment shapes:
      * empty catalog → generic fallback
      * HPC-style (GPU Compute, HPC Storage, Consultancy) → those slugs
      * gov-cloud-style (IAM, Compliance, Managed Database) → those slugs
    """

    @override_constance_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_empty_catalog_falls_back_to_generic(self):
        rubric = judge.build_intent_rubric()
        slugs = [s for s, _ in rubric]
        self.assertIn("compute", slugs)
        self.assertIn("storage", slugs)
        self.assertIn("unclear", slugs)

    @override_constance_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_hpc_catalog_yields_hpc_slugs(self):
        from waldur_mastermind.marketplace.enums import OfferingStates
        from waldur_mastermind.marketplace.tests import factories as mp_factories

        gpu = mp_factories.CategoryFactory(
            title="GPU Compute", description="NVIDIA accelerators for ML training."
        )
        storage = mp_factories.CategoryFactory(
            title="HPC Storage", description="Lustre, BeeGFS, scratch volumes."
        )
        consult = mp_factories.CategoryFactory(
            title="Consultancy and Expertise",
            description="Code porting, performance tuning.",
        )
        for cat in (gpu, storage, consult):
            mp_factories.OfferingFactory(
                category=cat, shared=True, state=OfferingStates.ACTIVE
            )

        rubric = judge.build_intent_rubric()
        slugs = {s for s, _ in rubric}
        self.assertIn("gpu_compute", slugs)
        self.assertIn("hpc_storage", slugs)
        self.assertIn("consultancy_and_expertis", slugs)  # 24-char cap
        self.assertIn("unclear", slugs)

    @override_constance_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_gov_cloud_catalog_yields_different_slugs(self):
        from waldur_mastermind.marketplace.enums import OfferingStates
        from waldur_mastermind.marketplace.tests import factories as mp_factories

        for title, desc in [
            ("IAM", "Identity and access management."),
            ("Managed Database", "Postgres / MySQL with backups."),
            ("Compliance", "GDPR, ISO 27001 audit support."),
        ]:
            cat = mp_factories.CategoryFactory(title=title, description=desc)
            mp_factories.OfferingFactory(
                category=cat, shared=True, state=OfferingStates.ACTIVE
            )

        rubric = judge.build_intent_rubric()
        slugs = {s for s, _ in rubric}
        self.assertIn("iam", slugs)
        self.assertIn("managed_database", slugs)
        self.assertIn("compliance", slugs)
        # No HPC-flavored slugs leaked.
        self.assertNotIn("gpu_compute", slugs)
        self.assertNotIn("compute", slugs)


class JudgeSystemPromptTest(TestCase):
    """The system prompt template renders cleanly with the deployment-derived rubric.

    TestCase (not SimpleTestCase) because override_constance_config writes
    to the constance backend which Django flags as a DB write.
    """

    def test_prompt_includes_each_rubric_slug(self):
        rubric = [
            ("compute", "user wants computing resources"),
            ("storage", "user wants storage"),
            ("unclear", "user's intent is genuinely ambiguous"),
        ]
        prompt = judge.render_judge_system_prompt(rubric)
        for slug, hint in rubric:
            self.assertIn(slug, prompt)
            self.assertIn(hint, prompt)

    @override_constance_config(SITE_DESCRIPTION="EuroHPC service hub")
    def test_prompt_uses_site_description_when_set(self):
        prompt = judge.render_judge_system_prompt([("unclear", "ambiguous")])
        self.assertIn("EuroHPC service hub", prompt)

    @override_constance_config(SITE_DESCRIPTION="")
    def test_prompt_falls_back_when_site_description_blank(self):
        prompt = judge.render_judge_system_prompt([("unclear", "ambiguous")])
        self.assertIn("a marketplace catalog", prompt)


class _StubInteraction:
    """Lightweight stub matching the attributes the helpers read.

    Keeps these tests free of DB setup — the helpers don't query the ORM,
    they just walk the persisted block shape.
    """

    def __init__(self, user_input="", assistant_blocks=None):
        self.user_input = user_input
        self.assistant_blocks = assistant_blocks or []


class BuildTranscriptTest(SimpleTestCase):
    def test_renders_user_then_assistant_with_turn_numbers(self):
        rows = [
            _StubInteraction(
                user_input="I need GPU clusters",
                assistant_blocks=[{"key": "markdown", "content": "Here are options."}],
            ),
            _StubInteraction(
                user_input="Compare LUMI and MeluXina",
                assistant_blocks=[
                    {"key": "markdown", "content": "LUMI has more GPUs."}
                ],
            ),
        ]
        out = judge.build_transcript(rows)
        self.assertIn("USER (turn 1): I need GPU clusters", out)
        self.assertIn("ASSISTANT (turn 1): Here are options.", out)
        self.assertIn("USER (turn 2): Compare LUMI and MeluXina", out)
        self.assertIn("ASSISTANT (turn 2): LUMI has more GPUs.", out)

    def test_inlines_tool_calls_with_args(self):
        rows = [
            _StubInteraction(
                user_input="GPU?",
                assistant_blocks=[
                    {"key": "markdown", "content": "Searching..."},
                    {
                        "key": "tool",
                        "tool": {
                            "name": "search_offerings",
                            "arguments": {"component_type": "gpu"},
                            "summary": "3 results",
                        },
                    },
                ],
            )
        ]
        out = judge.build_transcript(rows)
        self.assertIn("CALL search_offerings", out)
        self.assertIn('"component_type":"gpu"', out)
        self.assertIn("3 results", out)

    def test_drops_oldest_turns_over_cap(self):
        # 200 turns of long content easily blow past 12k chars
        rows = [
            _StubInteraction(
                user_input=f"Question {i} " + "x" * 100,
                assistant_blocks=[
                    {
                        "key": "markdown",
                        "content": f"Answer {i} " + "y" * 100,
                    }
                ],
            )
            for i in range(200)
        ]
        out = judge.build_transcript(rows)
        self.assertLessEqual(len(out), judge.TRANSCRIPT_CHAR_CAP + 50)
        # Most-recent turns survive
        self.assertIn("Question 199", out)
        # Oldest are dropped
        self.assertNotIn("Question 0 ", out)


class CollectToolResultsTest(SimpleTestCase):
    def test_dedupes_same_tool_same_args(self):
        same_block = {
            "key": "tool",
            "tool": {
                "name": "search_offerings",
                "arguments": {"q": "gpu"},
                "summary": "3",
            },
            "result": {"data": {"offerings": [{"uuid": "abc", "name": "LUMI"}]}},
        }
        rows = [
            _StubInteraction(assistant_blocks=[same_block]),
            _StubInteraction(assistant_blocks=[same_block]),
        ]
        out = judge.collect_tool_results_from_blocks(rows)
        # Called twice, but only one entry in tool_results
        self.assertEqual(out.count("search_offerings"), 1)

    def test_returns_empty_marker_when_no_tools(self):
        rows = [_StubInteraction(user_input="hi")]
        out = judge.collect_tool_results_from_blocks(rows)
        self.assertIn("no tool calls", out)


def _make_completed_session(
    session_id="session-old",
    last_active_minus_hours=24,
    user_slug="slug-1",
    blocks=None,
):
    """Build an interaction that's eligible for review (older than the
    ``_REVIEW_AFTER_HOURS`` cutoff in tasks.py)."""
    interaction = anonymous_models.AnonymousChatInteraction.objects.create(
        session_id=session_id,
        user_slug=user_slug,
        ip_address="1.2.3.4",
        user_input="hello",
        assistant_blocks=blocks
        or [{"key": "markdown", "content": "Hi! What HPC services are you after?"}],
        last_active_at=timezone.now() - timedelta(hours=last_active_minus_hours),
    )
    return interaction


class ReviewTaskNoOpTest(TestCase):
    @override_constance_config(ANONYMOUS_CHAT_REVIEW_ENABLED=False)
    def test_disabled_short_circuits_with_no_calls(self):
        _make_completed_session()
        with mock.patch("waldur_mastermind.chat.tasks.call_judge_llm") as mock_call:
            result = tasks.review_completed_sessions()
        self.assertEqual(result["status"], "disabled")
        mock_call.assert_not_called()


def _frozen_judge_response(score=5, intent="compute", hallucination=False):
    """Build a mocked JudgeResponse with deterministic content."""
    verdict = _verdict_dict(
        resolution_score=score,
        intent_category=intent,
        hallucination_detected=hallucination,
        summary="User wanted HPC services.",
    )
    return judge.JudgeResponse(
        content=json.dumps(verdict),
        input_tokens=500,
        output_tokens=100,
    )


class ReviewTaskHappyPathTest(TestCase):
    @override_constance_config(
        ANONYMOUS_CHAT_REVIEW_ENABLED=True,
        ANONYMOUS_CHAT_REVIEW_DAILY_TOKEN_BUDGET=100_000,
    )
    @mock.patch("waldur_mastermind.chat.tasks.call_judge_llm")
    def test_persists_verdict_on_completed_session(self, mock_call):
        interaction = _make_completed_session(last_active_minus_hours=24)
        mock_call.return_value = _frozen_judge_response(score=4, intent="compute")

        result = tasks.review_completed_sessions()

        self.assertEqual(result["reviewed"], 1)
        feedback = anonymous_models.AnonymousChatFeedback.objects.get(
            interaction=interaction
        )
        self.assertEqual(feedback.llm_resolution_score, 4)
        self.assertEqual(feedback.llm_intent_category, "compute")
        self.assertEqual(feedback.llm_judge_input_tokens, 500)
        self.assertIsNotNone(feedback.llm_reviewed_at)

    @override_constance_config(ANONYMOUS_CHAT_REVIEW_ENABLED=True)
    @mock.patch("waldur_mastermind.chat.tasks.call_judge_llm")
    def test_skips_recent_session(self, mock_call):
        # Last_active 1h ago — younger than the 6h cutoff, should be skipped
        _make_completed_session(last_active_minus_hours=1)
        result = tasks.review_completed_sessions()
        self.assertEqual(result["reviewed"], 0)
        mock_call.assert_not_called()

    @override_constance_config(ANONYMOUS_CHAT_REVIEW_ENABLED=True)
    @mock.patch("waldur_mastermind.chat.tasks.call_judge_llm")
    def test_skips_already_judged(self, mock_call):
        interaction = _make_completed_session(last_active_minus_hours=24)
        # Pre-existing feedback with llm_reviewed_at set — should not re-judge
        anonymous_models.AnonymousChatFeedback.objects.create(
            interaction=interaction,
            llm_reviewed_at=timezone.now(),
            llm_resolution_score=3,
        )
        result = tasks.review_completed_sessions()
        self.assertEqual(result["reviewed"], 0)
        # Already-judged sessions are excluded at the SQL level, so they never
        # enter the loop; the in-loop counter is a race-condition guard only.
        self.assertEqual(result["skipped_already_judged"], 0)
        mock_call.assert_not_called()

    @override_constance_config(
        ANONYMOUS_CHAT_REVIEW_ENABLED=True,
        ANONYMOUS_CHAT_REVIEW_DAILY_TOKEN_BUDGET=100_000,
    )
    @mock.patch("waldur_mastermind.chat.tasks._REVIEW_BATCH_SIZE", 2)
    @mock.patch("waldur_mastermind.chat.tasks.call_judge_llm")
    def test_respects_batch_size(self, mock_call):
        for i in range(5):
            _make_completed_session(session_id=f"s-{i}", last_active_minus_hours=24)
        mock_call.return_value = _frozen_judge_response()
        result = tasks.review_completed_sessions()
        self.assertEqual(result["reviewed"], 2)

    @override_constance_config(
        ANONYMOUS_CHAT_REVIEW_ENABLED=True,
        # Small budget — first call costs 600 tokens, budget runs out after one
        ANONYMOUS_CHAT_REVIEW_DAILY_TOKEN_BUDGET=500,
    )
    @mock.patch("waldur_mastermind.chat.tasks.call_judge_llm")
    def test_stops_when_budget_exhausted(self, mock_call):
        for i in range(3):
            _make_completed_session(session_id=f"s-{i}", last_active_minus_hours=24)
        mock_call.return_value = _frozen_judge_response()
        result = tasks.review_completed_sessions()
        # Budget: 500 - 600 = -100, so the loop bails before the second
        # session.
        self.assertEqual(result["reviewed"], 1)

    @override_constance_config(
        ANONYMOUS_CHAT_REVIEW_ENABLED=True,
        ANONYMOUS_CHAT_REVIEW_DAILY_TOKEN_BUDGET=100_000,
    )
    @mock.patch("waldur_mastermind.chat.tasks.call_judge_llm")
    def test_skips_unparseable_verdict(self, mock_call):
        _make_completed_session(last_active_minus_hours=24)
        # Garbage output → verdict is None → skip persist
        mock_call.return_value = judge.JudgeResponse(
            content="not even close to json",
            input_tokens=200,
            output_tokens=50,
        )
        result = tasks.review_completed_sessions()
        self.assertEqual(result["reviewed"], 0)
        self.assertEqual(result["skipped_parse_error"], 1)
        self.assertEqual(anonymous_models.AnonymousChatFeedback.objects.count(), 0)


class KpiReviewFieldsTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)
        self.url = reverse("anonymous-chat-interaction-kpi")

    def test_review_fields_null_when_no_reviews(self):
        anonymous_models.AnonymousChatInteraction.objects.create(
            session_id="s-1",
            user_input="hello",
            ip_address="1.2.3.4",
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Serializer renders the keys but values are null/absent when
        # no reviews exist — clients branch on truthiness.
        self.assertIsNone(response.data.get("avg_llm_resolution_score"))
        self.assertIsNone(response.data.get("hallucination_rate"))
        self.assertIsNone(response.data.get("review_coverage"))

    def test_reviewed_total_reports_zero_rather_than_dropping_out(self):
        anonymous_models.AnonymousChatInteraction.objects.create(
            session_id="s-1",
            user_input="hello",
            ip_address="1.2.3.4",
        )
        response = self.client.get(self.url)
        # The rate fields drop out when nothing is judged, but the count has to
        # survive: a dashboard that hides its review row when coverage is zero
        # is exactly how a dead nightly task goes unnoticed.
        self.assertEqual(response.data["reviewed_total"], 0)
        self.assertEqual(response.data["review_input_tokens_total"], 0)
        self.assertEqual(response.data["review_output_tokens_total"], 0)

    def test_judge_token_totals_are_kept_apart_from_visitor_spend(self):
        i1 = anonymous_models.AnonymousChatInteraction.objects.create(
            session_id="s-judged",
            user_input="hi",
            ip_address="1.2.3.4",
            input_tokens=11,
            output_tokens=3,
        )
        i2 = anonymous_models.AnonymousChatInteraction.objects.create(
            session_id="s-judged-too",
            user_input="hi",
            ip_address="1.2.3.5",
            input_tokens=7,
            output_tokens=2,
        )
        for interaction, judge_in, judge_out in ((i1, 4000, 120), (i2, 2500, 80)):
            anonymous_models.AnonymousChatFeedback.objects.create(
                interaction=interaction,
                llm_resolution_score=4,
                llm_reviewed_at=timezone.now(),
                llm_judge_input_tokens=judge_in,
                llm_judge_output_tokens=judge_out,
            )
        response = self.client.get(self.url)
        self.assertEqual(response.data["reviewed_total"], 2)
        self.assertEqual(response.data["review_input_tokens_total"], 6500)
        self.assertEqual(response.data["review_output_tokens_total"], 200)
        # Judge spend draws on its own budget so review can't starve visitor
        # traffic. Folding it into the visitor totals would hide the very split
        # that budget separation exists to preserve.
        self.assertEqual(response.data["input_tokens_total"], 18)
        self.assertEqual(response.data["output_tokens_total"], 5)

    def test_review_fields_present_when_reviewed(self):
        # Two sessions, only one is judged
        i1 = anonymous_models.AnonymousChatInteraction.objects.create(
            session_id="s-judged",
            user_input="hi",
            ip_address="1.2.3.4",
        )
        anonymous_models.AnonymousChatInteraction.objects.create(
            session_id="s-unjudged",
            user_input="hi",
            ip_address="1.2.3.5",
        )
        anonymous_models.AnonymousChatFeedback.objects.create(
            interaction=i1,
            llm_resolution_score=5,
            llm_intent_category="compute",
            llm_hallucination_detected=False,
            llm_summary="User wanted compute.",
            llm_reviewed_at=timezone.now(),
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["avg_llm_resolution_score"], 5.0)
        self.assertAlmostEqual(response.data["hallucination_rate"], 0.0)
        # 1 of 2 sessions reviewed → 50% coverage
        self.assertAlmostEqual(response.data["review_coverage"], 0.5)
        self.assertEqual(response.data["llm_intent_distribution"], {"compute": 1})
