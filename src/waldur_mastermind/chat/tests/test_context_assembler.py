import json
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from django.test import TestCase
from django.urls import reverse
from rest_framework import status, test
from rest_framework.exceptions import PermissionDenied

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.context_assembler import build_context
from waldur_mastermind.chat.models import ChatSession, Message, ThreadSession
from waldur_mastermind.chat.prompts import UI_CAPABILITIES


class BuildContextTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=session)

    @override_constance_config(LLM_CHAT_STORAGE_ENABLED=True)
    def test_builds_context_with_system_prompt_and_history(self):
        Message.objects.create(
            thread=self.thread, role="user", content="Hello", sequence_index=1
        )
        Message.objects.create(
            thread=self.thread,
            role="assistant",
            content="Hi there!",
            sequence_index=2,
        )

        context = build_context(self.user, "What resources?", thread=self.thread)

        # System prompt present
        self.assertIn("You are a highly knowledgeable", context)
        self.assertIn(UI_CAPABILITIES, context)
        # Tool instructions present
        self.assertIn("show_user_resources", context)
        # History present
        self.assertIn("user: Hello", context)
        self.assertIn("assistant: Hi there!", context)
        # Current message present
        self.assertIn("user: What resources?", context)

    @override_constance_config(LLM_CHAT_STORAGE_ENABLED=True)
    def test_no_thread_skips_history(self):
        context = build_context(self.user, "Hello", thread=None)

        self.assertIn("You are a highly knowledgeable", context)
        self.assertIn("user: Hello", context)
        # No history section
        self.assertNotIn("assistant:", context)

    @override_constance_config(LLM_CHAT_STORAGE_ENABLED=True)
    def test_conversation_history_chronological_order(self):
        Message.objects.create(
            thread=self.thread, role="user", content="First", sequence_index=1
        )
        Message.objects.create(
            thread=self.thread,
            role="assistant",
            content="Second",
            sequence_index=2,
        )
        Message.objects.create(
            thread=self.thread, role="user", content="Third", sequence_index=3
        )

        context = build_context(self.user, "Fourth", thread=self.thread)

        first_pos = context.index("user: First")
        second_pos = context.index("assistant: Second")
        third_pos = context.index("user: Third")
        fourth_pos = context.index("user: Fourth")

        self.assertLess(first_pos, second_pos)
        self.assertLess(second_pos, third_pos)
        self.assertLess(third_pos, fourth_pos)

    @override_constance_config(LLM_CHAT_STORAGE_ENABLED=True)
    def test_excludes_replaced_messages(self):
        original = Message.objects.create(
            thread=self.thread, role="user", content="Original", sequence_index=1
        )
        Message.objects.create(
            thread=self.thread,
            role="user",
            content="Edited",
            sequence_index=1,
            replaces=original,
        )

        context = build_context(self.user, "Next message", thread=self.thread)

        # Only the replacement (which has no replaced_by) should appear
        self.assertIn("user: Edited", context)
        self.assertNotIn("user: Original", context)

    @override_constance_config(LLM_CHAT_STORAGE_ENABLED=True, LLM_CHAT_HISTORY_LIMIT=50)
    def test_caps_at_history_limit(self):
        for i in range(60):
            Message.objects.create(
                thread=self.thread,
                role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i}",
                sequence_index=i + 1,
            )

        context = build_context(self.user, "Latest", thread=self.thread)

        # First 50 messages should be included (sequence_index 1-50 → Message 0-49)
        for i in range(50):
            self.assertIn(f"Message {i}", context)

        # Messages beyond 50 should NOT be included
        for i in range(50, 60):
            self.assertNotIn(f"Message {i}", context)

    @override_constance_config(LLM_CHAT_STORAGE_ENABLED=True)
    def test_title_generation_skips_history(self):
        Message.objects.create(
            thread=self.thread, role="user", content="Hello", sequence_index=1
        )

        context = build_context(
            self.user, "Generate title", thread=self.thread, include_history=False
        )

        self.assertIn("user: Generate title", context)
        self.assertNotIn("user: Hello", context)

    @override_constance_config(LLM_CHAT_STORAGE_ENABLED=False)
    def test_storage_disabled_skips_history(self):
        Message.objects.create(
            thread=self.thread, role="user", content="Old msg", sequence_index=1
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        self.assertIn("user: Hello", context)
        self.assertNotIn("Old msg", context)

    def test_raises_permission_denied_for_other_users_thread(self):
        other_user = structure_factories.UserFactory()
        with self.assertRaises(PermissionDenied):
            build_context(other_user, "Hello", thread=self.thread)

    @override_constance_config(LLM_CHAT_STORAGE_ENABLED=True, LLM_CHAT_HISTORY_LIMIT=-1)
    def test_invalid_history_limit_skips_history(self):
        Message.objects.create(
            thread=self.thread, role="user", content="Old msg", sequence_index=1
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        self.assertIn("user: Hello", context)
        self.assertNotIn("Old msg", context)

    @override_constance_config(LLM_CHAT_STORAGE_ENABLED=False)
    def test_no_thread_works_without_storage(self):
        context = build_context(self.user, "Hello", thread=None)
        self.assertIn("user: Hello", context)


class ChatStreamIntegrationTest(test.APITestCase):
    """Integration tests for the stream endpoint with backend context assembly."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)
        self.stream_url = reverse("chat-stream")

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_CHAT_STORAGE_ENABLED=True,
    )
    @mock.patch("waldur_mastermind.chat.views.requests.post")
    def test_stream_response(self, post_mock):
        fake_stream = [
            "data: " + json.dumps({"content": "Hello from backend context!"}),
        ]
        post_mock.return_value.__enter__.return_value = mock.Mock(
            iter_lines=lambda decode_unicode=False: fake_stream,
            raise_for_status=lambda: None,
        )

        response = self.client.post(
            self.stream_url, data={"input": "What resources do I have?"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/x-ndjson")

        body = b"".join(response.streaming_content).decode()
        lines = [line for line in body.splitlines() if line.strip()]

        content_found = False
        for line in lines:
            data = json.loads(line)
            if data.get("k") == "markdown":
                self.assertEqual(data["c"], "Hello from backend context!")
                content_found = True
        self.assertTrue(content_found)

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_CHAT_STORAGE_ENABLED=True,
    )
    @mock.patch("waldur_mastermind.chat.views.requests.post")
    def test_sends_assembled_context_to_llm(self, post_mock):
        fake_stream = [
            "data: " + json.dumps({"content": "Response"}),
        ]
        post_mock.return_value.__enter__.return_value = mock.Mock(
            iter_lines=lambda decode_unicode=False: fake_stream,
            raise_for_status=lambda: None,
        )

        response = self.client.post(self.stream_url, data={"input": "Test message"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        list(response.streaming_content)

        # Verify the payload sent to the LLM includes system prompt
        call_kwargs = post_mock.call_args
        sent_payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        self.assertIn("You are a highly knowledgeable", sent_payload["input"])
        self.assertIn("user: Test message", sent_payload["input"])

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_CHAT_STORAGE_ENABLED=True,
    )
    @mock.patch("waldur_mastermind.chat.views.requests.post")
    def test_persists_raw_input_not_context(self, post_mock):
        fake_stream = [
            "data: " + json.dumps({"content": "Assistant reply"}),
        ]
        post_mock.return_value.__enter__.return_value = mock.Mock(
            iter_lines=lambda decode_unicode=False: fake_stream,
            raise_for_status=lambda: None,
        )

        response = self.client.post(self.stream_url, data={"input": "My raw question"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        list(response.streaming_content)

        # The persisted user message should be the raw message, not the full context
        user_msg = Message.objects.filter(role="user").first()
        self.assertIsNotNone(user_msg)
        self.assertEqual(user_msg.content, "My raw question")

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
    )
    def test_missing_input_rejected(self):
        response = self.client.post(self.stream_url, data={})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
