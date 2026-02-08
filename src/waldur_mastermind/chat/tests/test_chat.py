import json
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import TokenQuota


class ChatBaseTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        self.stream_url = reverse("chat-stream")


class StreamEndpointTest(ChatBaseTest):
    """Test stream endpoint validation and configuration checks."""

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
    )
    def test_missing_input_returns_400(self):
        response = self.client.post(self.stream_url, data={})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_constance_config(LLM_CHAT_ENABLED=False)
    def test_returns_424_if_chat_disabled(self):
        response = self.client.post(
            self.stream_url,
            data={"input": "Hello"},
        )
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)
        self.assertEqual(response.data["detail"], "Extension is disabled.")

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="",
        LLM_INFERENCES_API_TOKEN="dummy-token",
    )
    def test_returns_409_if_inference_url_missing(self):
        response = self.client.post(
            self.stream_url,
            data={"input": "Hello"},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            response.data["detail"], "LLM inference API URL is not configured."
        )


class StreamResponseTest(ChatBaseTest):
    """Test NDJSON streaming response format."""

    @mock.patch("waldur_mastermind.chat.views.requests.post")
    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
    )
    def test_stream_proxies_ndjson_and_minifies(self, post_mock):
        upstream_payload = {
            "content": "Hello",
            "additional_kwargs": {"foo": "bar"},
        }

        fake_stream = [
            "data: " + json.dumps(upstream_payload),
        ]

        post_mock.return_value.__enter__.return_value = mock.Mock(
            iter_lines=lambda decode_unicode=False: fake_stream,
            raise_for_status=lambda: None,
        )

        response = self.client.post(
            self.stream_url,
            data={"input": "Hello"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/x-ndjson")

        body = b"".join(chunk for chunk in response.streaming_content).decode("utf-8")
        lines = [line for line in body.splitlines() if line.strip()]

        content_found = False
        meta_found = False

        for line in lines:
            data = json.loads(line)

            if data.get("k") == "markdown":
                self.assertEqual(data["c"], "Hello")
                content_found = True

            if "m" in data:  # don't send out metadata chunks
                self.assertEqual(data["m"]["foo"], "bar")
                meta_found = True

        self.assertTrue(content_found, "Did not find chunk with key 'k'='markdown'")
        self.assertFalse(meta_found, "Did find chunk with 'm'")


class StreamQuotaIntegrationTest(ChatBaseTest):
    """Test quota validation and usage recording integration with streaming."""

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_MONTHLY=100,
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
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_MONTHLY=-1,  # Unlimited
    )
    @mock.patch("waldur_mastermind.chat.views.requests.post")
    def test_stream_allowed_with_unlimited_quota(self, post_mock):
        """User with unlimited quota can always stream."""

        TokenQuota.for_user(self.user)
        # Leave limits as None (uses system defaults)

        fake_stream = [
            "data: " + json.dumps({"content": "Hello"}),
        ]

        post_mock.return_value.__enter__.return_value = mock.Mock(
            iter_lines=lambda decode_unicode=False: fake_stream,
            raise_for_status=lambda: None,
        )

        response = self.client.post(
            self.stream_url,
            data={"input": "x" * 10000},  # Very large input
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_MONTHLY=100,
    )
    @mock.patch("waldur_mastermind.chat.views.requests.post")
    def test_usage_recorded_after_stream(self, post_mock):
        """Verify that token usage is recorded after successful streaming."""

        quota = TokenQuota.for_user(self.user)
        quota.monthly_limit = 1000
        quota.save()
        initial_usage = quota.monthly_usage

        upstream_payload = {
            "content": "Hello world response",
            "additional_kwargs": {
                "usage_metadata": {"input_tokens": 5, "output_tokens": 8}
            },
        }

        fake_stream = [
            "data: " + json.dumps(upstream_payload),
        ]

        post_mock.return_value.__enter__.return_value = mock.Mock(
            iter_lines=lambda decode_unicode=False: fake_stream,
            raise_for_status=lambda: None,
        )

        response = self.client.post(
            self.stream_url,
            data={"input": "test"},
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
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_MONTHLY=100,
    )
    @mock.patch("waldur_mastermind.chat.views.requests.post")
    def test_stream_allowed_near_limit_may_exceed_after(self, post_mock):
        """User near limit can stream and may exceed limit after actual usage is recorded."""

        quota = TokenQuota.for_user(self.user)
        quota.monthly_limit = 100
        quota.monthly_usage = 95  # Near limit but not at/over
        quota.save()

        upstream_payload = {
            "content": "Response",
            "additional_kwargs": {
                "usage_metadata": {"input_tokens": 3, "output_tokens": 5}
            },
        }

        fake_stream = [
            "data: " + json.dumps(upstream_payload),
        ]

        post_mock.return_value.__enter__.return_value = mock.Mock(
            iter_lines=lambda decode_unicode=False: fake_stream,
            raise_for_status=lambda: None,
        )

        # Should be allowed because not yet at limit
        response = self.client.post(
            self.stream_url,
            data={"input": "test"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Consume stream to record usage
        list(response.streaming_content)

        # Verify usage was recorded and now exceeds limit
        quota.refresh_from_db()
        self.assertEqual(quota.monthly_usage, 103)  # 95 + 3 + 5 = 103 (over limit)

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_MONTHLY="invalid_value",
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
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_DAILY=-10,  # Invalid: below -1
    )
    def test_stream_fails_with_invalid_negative_limit(self):
        """Stream request fails when constance has limit below -1."""
        TokenQuota.for_user(self.user)

        response = self.client.post(
            self.stream_url,
            data={"input": "test"},
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
