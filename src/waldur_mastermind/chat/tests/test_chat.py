import json
import uuid
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import (
    ChatSession,
    Message,
    ThreadSession,
    TokenQuota,
)
from waldur_mastermind.chat.tests.utils import (
    SYNC_THREAD_PATCH,
    _make_content_chunk,
    _make_usage_chunk,
    _mock_openai_client,
    _SynchronousThread,
    text_from_blocks,
)


class ChatBaseTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        self.stream_url = reverse("chat-stream")


class StreamEndpointTest(ChatBaseTest):
    """Test stream endpoint validation and configuration checks."""

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
    )
    def test_missing_input_returns_400(self):
        response = self.client.post(self.stream_url, data={})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_constance_config(AI_ASSISTANT_ENABLED=False)
    def test_returns_424_if_chat_disabled(self):
        response = self.client.post(
            self.stream_url,
            data={"input": "Hello"},
        )
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)
        self.assertEqual(response.data["detail"], "Extension is disabled.")

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="",
        AI_ASSISTANT_API_TOKEN="dummy-token",
    )
    def test_returns_409_if_inference_url_missing(self):
        response = self.client.post(
            self.stream_url,
            data={"input": "Hello"},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            response.data["detail"], "AI Assistant API URL is not configured."
        )


class StreamResponseTest(ChatBaseTest):
    """Test NDJSON streaming response format."""

    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
    )
    def test_stream_proxies_ndjson_and_minifies(self, mock_openai_cls):
        mock_client = _mock_openai_client([_make_content_chunk("Hello")])
        mock_openai_cls.return_value = mock_client

        response = self.client.post(
            self.stream_url,
            data={"input": "Hello"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/x-ndjson")

        body = b"".join(chunk for chunk in response.streaming_content).decode("utf-8")
        lines = [line for line in body.splitlines() if line.strip()]

        content_found = False

        for line in lines:
            data = json.loads(line)

            if data.get("k") == "markdown":
                self.assertEqual(data["c"], "Hello")
                content_found = True

        self.assertTrue(content_found, "Did not find chunk with key 'k'='markdown'")


@mock.patch(SYNC_THREAD_PATCH, _SynchronousThread)
class StreamQuotaIntegrationTest(ChatBaseTest):
    """Test quota validation and usage recording integration with streaming."""

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_TOKEN_LIMIT_MONTHLY=100,
    )
    def test_stream_rejected_when_quota_exceeded(self):
        """User cannot stream if already at or over quota limit."""

        # Set user quota to 100 tokens and current usage to 100 (at limit)
        quota = TokenQuota.for_user(self.user)
        quota.monthly_limit = 100
        quota.monthly_usage = 100
        quota.save()

        # Try to stream - should be rejected because already at limit
        response = self.client.post(
            self.stream_url,
            data={"input": "test message"},
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("Monthly", str(response.data))

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_TOKEN_LIMIT_MONTHLY=-1,  # Unlimited
    )
    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_stream_allowed_with_unlimited_quota(self, mock_openai_cls):
        """User with unlimited quota can always stream."""

        TokenQuota.for_user(self.user)
        # Leave limits as None (uses system defaults)

        mock_client = _mock_openai_client([_make_content_chunk("Hello")])
        mock_openai_cls.return_value = mock_client

        response = self.client.post(
            self.stream_url,
            data={"input": "x" * 10000},  # Very large input
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_TOKEN_LIMIT_MONTHLY=100,
    )
    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_usage_recorded_after_stream(self, mock_openai_cls):
        """Verify that token usage is recorded after successful streaming."""

        quota = TokenQuota.for_user(self.user)
        quota.monthly_limit = 1000
        quota.save()
        initial_usage = quota.monthly_usage

        # Use existing thread to avoid title-generation AI Assistant call
        session, _ = ChatSession.objects.get_or_create(user=self.user)
        thread = ThreadSession.objects.create(chat_session=session)

        mock_client = _mock_openai_client(
            [
                _make_content_chunk("Hello world response"),
                _make_usage_chunk(5, 8),
            ]
        )
        mock_openai_cls.return_value = mock_client

        response = self.client.post(
            self.stream_url,
            data={"input": "test", "thread_uuid": str(thread.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Consume the streaming response to trigger _record_usage()
        list(response.streaming_content)

        # Verify quota was updated with actual token counts
        quota.refresh_from_db()
        final_usage = quota.monthly_usage
        expected_increase = 5 + 8  # input + output tokens
        self.assertEqual(final_usage - initial_usage, expected_increase)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_TOKEN_LIMIT_MONTHLY=100,
    )
    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_stream_allowed_near_limit_may_exceed_after(self, mock_openai_cls):
        """User near limit can stream and may exceed limit after actual usage is recorded."""

        quota = TokenQuota.for_user(self.user)
        quota.monthly_limit = 100
        quota.monthly_usage = 95  # Near limit but not at/over
        quota.save()

        # Use existing thread to avoid title-generation AI Assistant call
        session, _ = ChatSession.objects.get_or_create(user=self.user)
        thread = ThreadSession.objects.create(chat_session=session)

        mock_client = _mock_openai_client(
            [
                _make_content_chunk("Response"),
                _make_usage_chunk(3, 5),
            ]
        )
        mock_openai_cls.return_value = mock_client

        # Should be allowed because not yet at limit
        response = self.client.post(
            self.stream_url,
            data={"input": "test", "thread_uuid": str(thread.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Consume stream to record usage
        list(response.streaming_content)

        # Verify usage was recorded and now exceeds limit
        quota.refresh_from_db()
        self.assertEqual(quota.monthly_usage, 103)  # 95 + 3 + 5 = 103 (over limit)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
    )
    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_two_messages_have_correct_sequence_order(self, mock_openai_cls):
        """Sequential messages land in correct order: UserA, AsstA, UserB, AsstB."""
        session, _ = ChatSession.objects.get_or_create(user=self.user)
        thread = ThreadSession.objects.create(chat_session=session)

        mock_openai_cls.return_value = _mock_openai_client(
            [_make_content_chunk("Response A")]
        )
        r1 = self.client.post(
            self.stream_url,
            data={"input": "Question A", "thread_uuid": str(thread.uuid)},
        )
        list(r1.streaming_content)

        mock_openai_cls.return_value = _mock_openai_client(
            [_make_content_chunk("Response B")]
        )
        r2 = self.client.post(
            self.stream_url,
            data={"input": "Question B", "thread_uuid": str(thread.uuid)},
        )
        list(r2.streaming_content)

        msgs = list(Message.objects.filter(thread=thread).order_by("sequence_index"))
        self.assertEqual(len(msgs), 4)
        self.assertEqual(msgs[0].role, Message.Role.USER)
        self.assertEqual(text_from_blocks(msgs[0].blocks), "Question A")
        self.assertEqual(msgs[1].role, Message.Role.ASSISTANT)
        self.assertEqual(text_from_blocks(msgs[1].blocks), "Response A")
        self.assertEqual(msgs[2].role, Message.Role.USER)
        self.assertEqual(text_from_blocks(msgs[2].blocks), "Question B")
        self.assertEqual(msgs[3].role, Message.Role.ASSISTANT)
        self.assertEqual(text_from_blocks(msgs[3].blocks), "Response B")

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_TOKEN_LIMIT_MONTHLY="invalid_value",
    )
    def test_stream_fails_with_invalid_constance_config(self):
        """Stream request fails when constance config has invalid value."""
        TokenQuota.for_user(self.user)

        response = self.client.post(
            self.stream_url,
            data={"input": "test"},
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_TOKEN_LIMIT_DAILY=-10,  # Invalid: below -1
    )
    def test_stream_fails_with_invalid_negative_limit(self):
        """Stream request fails when constance has limit below -1."""
        TokenQuota.for_user(self.user)

        response = self.client.post(
            self.stream_url,
            data={"input": "test"},
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


@mock.patch(SYNC_THREAD_PATCH, _SynchronousThread)
class StreamGlobalBudgetTest(ChatBaseTest):
    """Auth chat path increments + checks the same singleton GlobalAssistantBudget
    as the anonymous chat path.

    Catches the abuse case where an authenticated user with a generous
    per-user cap could otherwise burn through site-wide budget in a way
    the per-user gate alone wouldn't notice.
    """

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_GLOBAL_DAILY_TOKEN_BUDGET=100,
    )
    def test_429_when_global_daily_token_exhausted(self):
        from django.db import transaction

        from waldur_mastermind.chat.models import GlobalAssistantBudget

        with transaction.atomic():
            budget = GlobalAssistantBudget.get(lock=True)
            budget.daily_token_usage = 1_000
            budget.save(update_fields=["daily_token_usage"])

        response = self.client.post(self.stream_url, data={"input": "hi"})

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data["code"], "global_daily_token")
        self.assertIn("Retry-After", response.headers)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_GLOBAL_REQUESTS_PER_MINUTE=1,
    )
    def test_429_when_global_minute_burst_exhausted(self):
        from django.db import transaction

        from waldur_mastermind.chat.models import GlobalAssistantBudget

        with transaction.atomic():
            budget = GlobalAssistantBudget.get(lock=True)
            budget.minute_request_usage = 5
            budget.save(update_fields=["minute_request_usage"])

        response = self.client.post(self.stream_url, data={"input": "hi"})

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data["code"], "global_minute_burst")
        self.assertIn("Retry-After", response.headers)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_TOKEN_LIMIT_MONTHLY=10000,
    )
    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_global_budget_incremented_after_auth_stream(self, mock_openai_cls):
        """Auth-path streaming bumps the same singleton anon increments."""
        from waldur_mastermind.chat.models import GlobalAssistantBudget

        TokenQuota.for_user(self.user)
        mock_openai_cls.return_value = _mock_openai_client(
            [_make_content_chunk("hi"), _make_usage_chunk(10, 20)]
        )

        # Use an existing thread to avoid title-generation usage noise.
        session, _ = ChatSession.objects.get_or_create(user=self.user)
        thread = ThreadSession.objects.create(chat_session=session)

        response = self.client.post(
            self.stream_url,
            data={"input": "test", "thread_uuid": str(thread.uuid)},
        )
        # Drain the stream so post-stream accounting runs.
        b"".join(response.streaming_content)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        budget = GlobalAssistantBudget.get()
        self.assertEqual(budget.daily_token_usage, 30)


@override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class CancelEndpointTest(ChatBaseTest):
    """Test the POST /chat-threads/{uuid}/cancel/ endpoint."""

    def _make_thread(self):
        session, _ = ChatSession.objects.get_or_create(user=self.user)
        return ThreadSession.objects.create(chat_session=session, name="test")

    def test_cancel_sets_flag(self):
        thread = self._make_thread()
        url = reverse("chat-thread-cancel", kwargs={"uuid": thread.uuid})
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        thread.refresh_from_db()
        self.assertIsNotNone(thread.cancel_requested_at)

    def test_cancel_is_idempotent(self):
        thread = self._make_thread()
        url = reverse("chat-thread-cancel", kwargs={"uuid": thread.uuid})
        self.client.post(url)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cancel_returns_404_for_unknown_thread(self):
        url = reverse("chat-thread-cancel", kwargs={"uuid": uuid.uuid4()})
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cancel_returns_404_for_other_users_thread(self):
        other_user = structure_factories.UserFactory()
        other_session, _ = ChatSession.objects.get_or_create(user=other_user)
        other_thread = ThreadSession.objects.create(chat_session=other_session)
        url = reverse("chat-thread-cancel", kwargs={"uuid": other_thread.uuid})
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
