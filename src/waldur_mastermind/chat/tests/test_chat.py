import json
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories


class ChatBaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        self.stream_url = reverse("chat-stream")


class StreamEndpointValidationTest(ChatBaseTest):
    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
    )
    def test_missing_input_returns_400(self):
        response = self.client.post(self.stream_url, data={})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class StreamEndpointConfigTest(ChatBaseTest):
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


class StreamEndpointNdjsonTest(ChatBaseTest):
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

            if "m" in data:
                self.assertEqual(data["m"]["foo"], "bar")
                meta_found = True

        self.assertTrue(content_found, "Did not find chunk with key 'k'='markdown'")
        self.assertTrue(meta_found, "Did not find chunk with 'm'")
