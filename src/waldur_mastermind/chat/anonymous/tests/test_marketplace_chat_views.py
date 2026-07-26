"""Tests for the public anonymous chat endpoint (MarketplaceChatViewSet).

Three actions under the /api/marketplace-chat/ prefix:

  * stream    — full streaming flow with budget + binding + detection guards
  * feedback  — HMAC-bound thumbs feedback
  * click     — HMAC-bound click capture, validated against the interaction's
                recommended set

LLM streaming itself is tested elsewhere (test_chat.py + test_streamer.py);
here we only exercise the anon-specific guards and persistence.
"""

from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework import status, test

from waldur_mastermind.chat.anonymous import models as anonymous_models
from waldur_mastermind.chat.anonymous.helpers import compute_feedback_token
from waldur_mastermind.chat.anonymous.views import _extract_offering_uuids
from waldur_mastermind.chat.models import GlobalAssistantBudget
from waldur_mastermind.chat.tests.utils import (
    SYNC_THREAD_PATCH,
    _make_content_chunk,
    _make_usage_chunk,
    _mock_openai_client,
    _SynchronousThread,
)
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories as mp_factories

_ANONYMOUS_LIVE = dict(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="anonymous",
    AI_ASSISTANT_API_URL="https://example.com/llm",
    AI_ASSISTANT_API_TOKEN="dummy-token",
    AI_ASSISTANT_TOKEN_LIMIT_DAILY=100000,
    ANONYMOUS_CHAT_USER_SLUG_SALT="test-salt",
    ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET="test-secret",
    ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
)


def _seed_offering(name="Test Offering"):
    """At least one shared+ACTIVE offering so is_public_marketplace_enabled
    short-circuits don't flip the catalog summary builder into the empty
    branch — and so the catalog isn't empty when we exercise streaming."""
    return mp_factories.OfferingFactory(
        shared=True,
        state=OfferingStates.ACTIVE,
        name=name,
    )


class StreamGatingTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("marketplace-chat-stream")

    @override_constance_config(AI_ASSISTANT_ENABLED=False)
    def test_424_when_master_switch_off(self):
        # Mirrors the auth-path convention (`LLMConfigurationMixin`
        # raises `ExtensionDisabled` — 424). Picking a different code
        # for the same condition would diverge from the rest of the
        # codebase.
        response = self.client.post(
            self.url, data={"input": "hi", "session_id": "session-abc"}
        )
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="anonymous",
        AI_ASSISTANT_API_URL="",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
    )
    def test_409_when_api_url_unset(self):
        # Mirrors `ChatViewSet`'s 409 when AI Assistant API URL/token
        # is missing — same condition, same code.
        response = self.client.post(
            self.url, data={"input": "hi", "session_id": "session-abc"}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @override_constance_config(
        **{**_ANONYMOUS_LIVE, "ANONYMOUS_USER_CAN_VIEW_OFFERINGS": False}
    )
    def test_424_when_public_marketplace_disabled(self):
        # No public offerings = effectively the same as the master
        # switch being off; same status.
        response = self.client.post(
            self.url, data={"input": "hi", "session_id": "session-abc"}
        )
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_400_when_input_missing(self):
        response = self.client.post(self.url, data={"session_id": "session-abc"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_400_when_session_id_too_short(self):
        response = self.client.post(self.url, data={"input": "hi", "session_id": "x"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@mock.patch(SYNC_THREAD_PATCH, _SynchronousThread)
class StreamHappyPathTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("marketplace-chat-stream")
        _seed_offering()

    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_returns_ndjson_stream_with_interaction_uuid(self, mock_openai_cls):
        mock_openai_cls.return_value = _mock_openai_client(
            [_make_content_chunk("Hello!"), _make_usage_chunk(10, 20)]
        )

        response = self.client.post(
            self.url,
            data={
                "input": "what HPC services do you have?",
                "session_id": "session-abc",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/x-ndjson")

        body = b"".join(response.streaming_content).decode("utf-8")
        lines = [line for line in body.splitlines() if line.strip()]
        # First line should be the m frame with interaction_uuid + token
        import json as _json

        first = _json.loads(lines[0])
        self.assertIn("m", first)
        self.assertIn("interaction_uuid", first["m"])
        self.assertIn("feedback_token", first["m"])
        self.assertTrue(first["m"]["feedback_token"])

        # The interaction row was persisted
        self.assertEqual(anonymous_models.AnonymousChatInteraction.objects.count(), 1)
        # SessionBinding row was created on first use
        self.assertEqual(anonymous_models.SessionBinding.objects.count(), 1)

    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_persists_the_token_split_on_the_interaction(self, mock_openai_cls):
        mock_openai_cls.return_value = _mock_openai_client(
            [_make_content_chunk("Hello!"), _make_usage_chunk(10, 20)]
        )

        response = self.client.post(
            self.url,
            data={
                "input": "what HPC services do you have?",
                "session_id": "session-abc",
            },
        )
        b"".join(response.streaming_content)

        # The budget counters collapse these to a single total, so the row is
        # the only place the input/output split survives.
        interaction = anonymous_models.AnonymousChatInteraction.objects.get()
        self.assertEqual(interaction.input_tokens, 10)
        self.assertEqual(interaction.output_tokens, 20)

    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_session_id_reused_from_different_ip_returns_403(self, mock_openai_cls):
        mock_openai_cls.return_value = _mock_openai_client(
            [_make_content_chunk("Hi"), _make_usage_chunk(1, 1)]
        )
        # First request — IP A binds the session
        self.client.post(
            self.url,
            data={"input": "first", "session_id": "session-abc"},
            REMOTE_ADDR="1.2.3.4",
        )
        # Second request — IP B reuses the same session_id → 403
        response = self.client.post(
            self.url,
            data={"input": "second", "session_id": "session-abc"},
            REMOTE_ADDR="9.9.9.9",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_429_when_global_minute_burst_exhausted(self):
        # Pre-fill the singleton at exactly the cap so the next request trips it.
        # Use atomic + select_for_update like the production path does.
        from django.db import transaction

        with transaction.atomic():
            budget = GlobalAssistantBudget.get(lock=True)
            budget.minute_request_usage = 10_000  # well past any sane minute cap
            budget.save(update_fields=["minute_request_usage"])

        with override_constance_config(
            **_ANONYMOUS_LIVE,
            AI_ASSISTANT_GLOBAL_REQUESTS_PER_MINUTE=1,
        ):
            response = self.client.post(
                self.url,
                data={"input": "hi", "session_id": "session-abc"},
            )
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

        # Retry-After header (HTTP convention) + structured body
        # (Homeport renders the right banner from `code`).
        self.assertIn("Retry-After", response.headers)
        self.assertGreaterEqual(int(response.headers["Retry-After"]), 1)
        self.assertEqual(response.data["error"], "rate_limited")
        self.assertEqual(response.data["code"], "global_minute_burst")
        self.assertIn("reset_at", response.data)
        self.assertIn("retry_after_seconds", response.data)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_429_when_global_daily_token_budget_exhausted(self):
        # Site-wide token budget exhaustion → 429 with Retry-After.
        # Distinct from per-IP daily token cap (409) — different audience,
        # different reset cadence.
        from django.db import transaction

        with transaction.atomic():
            budget = GlobalAssistantBudget.get(lock=True)
            budget.daily_token_usage = 10_000_000
            budget.save(update_fields=["daily_token_usage"])

        with override_constance_config(
            **_ANONYMOUS_LIVE,
            AI_ASSISTANT_GLOBAL_DAILY_TOKEN_BUDGET=1,
        ):
            response = self.client.post(
                self.url,
                data={"input": "hi", "session_id": "session-abc"},
            )
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("Retry-After", response.headers)
        self.assertEqual(response.data["error"], "rate_limited")
        self.assertEqual(response.data["code"], "global_daily_token")


class PerIpBudgetGatingTest(test.APITestCase):
    """View-level tests for weekly / monthly per-IP caps and the -1 == unlimited daily case."""

    def setUp(self):
        self.url = reverse("marketplace-chat-stream")
        _seed_offering()

    @override_constance_config(
        **{**_ANONYMOUS_LIVE, "AI_ASSISTANT_TOKEN_LIMIT_WEEKLY": 500}
    )
    def test_409_when_per_ip_weekly_token_exhausted(self):
        from django.db import transaction

        with transaction.atomic():
            budget = anonymous_models.AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            anonymous_models.AnonymousChatBudget.objects.filter(pk=budget.pk).update(
                weekly_token_usage=500
            )

        response = self.client.post(
            self.url,
            data={"input": "hi", "session_id": "session-abc"},
            REMOTE_ADDR="1.2.3.4",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "per_ip_weekly_token")
        self.assertIn("Retry-After", response.headers)

    @override_constance_config(
        **{**_ANONYMOUS_LIVE, "AI_ASSISTANT_TOKEN_LIMIT_MONTHLY": 1000}
    )
    def test_409_when_per_ip_monthly_token_exhausted(self):
        from django.db import transaction

        with transaction.atomic():
            budget = anonymous_models.AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            anonymous_models.AnonymousChatBudget.objects.filter(pk=budget.pk).update(
                monthly_token_usage=1000
            )

        response = self.client.post(
            self.url,
            data={"input": "hi", "session_id": "session-abc"},
            REMOTE_ADDR="1.2.3.4",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "per_ip_monthly_token")
        self.assertIn("Retry-After", response.headers)

    @override_constance_config(
        **{**_ANONYMOUS_LIVE, "AI_ASSISTANT_TOKEN_LIMIT_DAILY": -1}
    )
    @mock.patch(SYNC_THREAD_PATCH, _SynchronousThread)
    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_anon_request_succeeds_when_token_limit_daily_is_minus_one(
        self, mock_openai_cls
    ):
        # -1 == unlimited; the old misconfig gate 409'd here. Should succeed now.
        mock_openai_cls.return_value = _mock_openai_client(
            [_make_content_chunk("Hello!"), _make_usage_chunk(5, 10)]
        )
        response = self.client.post(
            self.url,
            data={"input": "what services do you have?", "session_id": "session-abc"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class FeedbackTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("marketplace-chat-feedback")
        self.interaction = anonymous_models.AnonymousChatInteraction.objects.create(
            user_input="test",
            ip_address="1.2.3.4",
            session_id="session-abc",
            offering_uuids=[],
        )

    def _valid_token(self):
        with override_constance_config(
            ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET="test-secret"
        ):
            return compute_feedback_token(
                interaction_uuid=str(self.interaction.uuid),
                session_id="session-abc",
                ip_address="1.2.3.4",
            )

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_happy_path_thumbs_up(self):
        token = self._valid_token()
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": str(self.interaction.uuid),
                "feedback_token": token,
                "score": 1,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            anonymous_models.AnonymousChatFeedback.objects.filter(
                interaction=self.interaction, score=1
            ).exists()
        )

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_happy_path_thumbs_down_with_category(self):
        token = self._valid_token()
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": str(self.interaction.uuid),
                "feedback_token": token,
                "score": -1,
                "category": "inaccurate",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_idempotent_update(self):
        token = self._valid_token()
        # First — thumbs down
        self.client.post(
            self.url,
            data={
                "interaction_uuid": str(self.interaction.uuid),
                "feedback_token": token,
                "score": -1,
                "category": "inaccurate",
            },
        )
        # Second — same caller flips to thumbs up
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": str(self.interaction.uuid),
                "feedback_token": token,
                "score": 1,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Exactly one row, latest write wins
        rows = anonymous_models.AnonymousChatFeedback.objects.filter(
            interaction=self.interaction
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().score, 1)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_400_when_score_minus_one_without_category(self):
        token = self._valid_token()
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": str(self.interaction.uuid),
                "feedback_token": token,
                "score": -1,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_404_on_unknown_interaction(self):
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": "00000000-0000-0000-0000-000000000000",
                "feedback_token": "anything",
                "score": 1,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_403_on_forged_token(self):
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": str(self.interaction.uuid),
                "feedback_token": "deadbeef" * 8,
                "score": 1,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_403_even_when_caller_ip_matches_interaction_ip(self):
        # The token gate is the binding constraint, not the IP.
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": str(self.interaction.uuid),
                "feedback_token": "deadbeef" * 8,
                "score": 1,
            },
            REMOTE_ADDR="1.2.3.4",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ClickTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("marketplace-chat-click")
        self.recommended = "11111111-1111-1111-1111-111111111111"
        self.interaction = anonymous_models.AnonymousChatInteraction.objects.create(
            user_input="test",
            ip_address="1.2.3.4",
            session_id="session-abc",
            offering_uuids=[self.recommended],
        )

    def _valid_token(self):
        with override_constance_config(
            ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET="test-secret"
        ):
            return compute_feedback_token(
                interaction_uuid=str(self.interaction.uuid),
                session_id="session-abc",
                ip_address="1.2.3.4",
            )

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_happy_path_click_recorded(self):
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": str(self.interaction.uuid),
                "feedback_token": self._valid_token(),
                "offering_uuid": self.recommended,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            anonymous_models.AnonymousChatClick.objects.filter(
                interaction=self.interaction
            ).count(),
            1,
        )

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_multiple_clicks_create_multiple_rows(self):
        token = self._valid_token()
        for _ in range(3):
            self.client.post(
                self.url,
                data={
                    "interaction_uuid": str(self.interaction.uuid),
                    "feedback_token": token,
                    "offering_uuid": self.recommended,
                },
            )
        self.assertEqual(
            anonymous_models.AnonymousChatClick.objects.filter(
                interaction=self.interaction
            ).count(),
            3,
        )

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_400_when_offering_not_in_recommended_set(self):
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": str(self.interaction.uuid),
                "feedback_token": self._valid_token(),
                # Different UUID — never appeared in this interaction's
                # recommendation set.
                "offering_uuid": "22222222-2222-2222-2222-222222222222",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_403_on_forged_token(self):
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": str(self.interaction.uuid),
                "feedback_token": "deadbeef" * 8,
                "offering_uuid": self.recommended,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(**_ANONYMOUS_LIVE)
    def test_404_on_unknown_interaction(self):
        response = self.client.post(
            self.url,
            data={
                "interaction_uuid": "00000000-0000-0000-0000-000000000000",
                "feedback_token": "anything",
                "offering_uuid": self.recommended,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ExtractOfferingUuidsTest(SimpleTestCase):
    """Offerings surface as homeport_nav links or as markdown prose links; the
    UUID lives in the URL either way, and the frontend reports a click on both."""

    def test_extracts_uuids_from_markdown_prose_links(self):
        # The assistant frequently answers with plain markdown links and no nav
        # block at all. Those were skipped, so clicking them failed validation
        # and the conversation reported zero offerings shown.
        blocks = [
            {
                "key": "markdown",
                "content": (
                    "- Access: [View offering]"
                    "(http://localhost:8001/marketplace-public-offering/"
                    "f3000000000000000000000000000041/)\n"
                    "- Access: [View offering]"
                    "(http://localhost:8001/marketplace-public-offering/"
                    "f3000000000000000000000000000042/)"
                ),
            }
        ]
        self.assertEqual(
            _extract_offering_uuids(blocks),
            [
                "f3000000000000000000000000000041",
                "f3000000000000000000000000000042",
            ],
        )

    def test_dedupes_across_markdown_and_nav_surfaces(self):
        uuid = "f3000000000000000000000000000001"
        url = f"http://localhost:8001/marketplace-public-offering/{uuid}/"
        blocks = [
            {"key": "markdown", "content": f"see [it]({url})"},
            {"key": "tool", "result": {"key": "homeport_nav", "links": [{"url": url}]}},
        ]
        self.assertEqual(_extract_offering_uuids(blocks), [uuid])

    def test_extracts_uuids_from_homeport_nav_links(self):
        blocks = [
            {
                "key": "tool",
                "tool": {"name": "search_offerings"},
                "result": {
                    "key": "homeport_nav",
                    "links": [
                        {
                            "url": "http://localhost:8001/marketplace-public-offering/f3000000000000000000000000000001/"
                        },
                        {
                            "url": "http://localhost:8001/marketplace-public-offering/8adeda2b89624706b1b217d42f3cc64e/"
                        },
                    ],
                },
            }
        ]
        self.assertEqual(
            _extract_offering_uuids(blocks),
            [
                "f3000000000000000000000000000001",
                "8adeda2b89624706b1b217d42f3cc64e",
            ],
        )

    def test_dedupes_and_skips_non_offering_blocks(self):
        url = "http://localhost:8001/marketplace-public-offering/f3000000000000000000000000000001/"
        blocks = [
            {"key": "markdown", "content": "hi"},
            {"key": "tool", "result": {"key": "ask_user_form", "questions": []}},
            {
                "key": "tool",
                "result": {
                    "key": "homeport_nav",
                    "links": [{"url": url}, {"url": url}],
                },
            },
        ]
        self.assertEqual(
            _extract_offering_uuids(blocks), ["f3000000000000000000000000000001"]
        )

    def test_falls_back_to_structured_data(self):
        blocks = [{"key": "tool", "result": {"data": {"uuid": "abc-123"}}}]
        self.assertEqual(_extract_offering_uuids(blocks), ["abc-123"])
