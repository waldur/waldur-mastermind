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
        self.invoke_url = reverse("chat-invoke")


class InvokeEndpointTest(ChatBaseTest):
    @override_constance_config(LLM_CHAT_ENABLED=True)
    def test_invoke_returns_200(self):
        response = self.client.post(self.invoke_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, "Invoke chat response")


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
    def test_returns_404_if_chat_disabled(self):
        response = self.client.post(
            self.stream_url,
            data={"input": "Hello"},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["detail"], "Not found.")

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


class StreamEndpointSseTest(ChatBaseTest):
    @mock.patch("waldur_mastermind.chat.views.requests.post")
    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
    )
    def test_stream_proxies_sse_and_strips_metadata(self, post_mock):
        # Upstream LLM API sends "content", we transform it to "c" for bandwidth
        upstream_payload = {
            "content": "Hello",
            "additional_kwargs": {"foo": "bar", "unused": 1},
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
        self.assertEqual(response["Content-Type"], "text/event-stream")

        body = b"".join(chunk for chunk in response.streaming_content).decode("utf-8")

        data_lines = [line for line in body.splitlines() if line.startswith("data: ")]

        self.assertTrue(
            len(data_lines) >= 2, "Expected at least separate content and kwargs events"
        )

        content_found = False
        kwargs_found = False

        for line in data_lines:
            data = json.loads(line[len("data: ") :])

            # Content is transformed from "content" to "c" for bandwidth optimization
            if "c" in data:
                self.assertEqual(data["c"], "Hello")
                content_found = True

            if "additional_kwargs" in data:
                self.assertIn("foo", data["additional_kwargs"])
                self.assertIn("unused", data["additional_kwargs"])
                kwargs_found = True

        self.assertTrue(content_found, "Did not find chunk with key 'c'")
        self.assertTrue(kwargs_found, "Did not find chunk with 'additional_kwargs'")
