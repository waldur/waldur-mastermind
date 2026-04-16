import json
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from django.test import TestCase
from django.urls import reverse
from rest_framework import status, test
from rest_framework.exceptions import PermissionDenied

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.context_assembler import (
    ContextResult,
    build_context,
    build_context_with_intent,
)
from waldur_mastermind.chat.intent_classifier import Intent
from waldur_mastermind.chat.models import ChatSession, Message, ThreadSession
from waldur_mastermind.chat.prompts.ui_capabilities import UI_CAPABILITIES
from waldur_mastermind.chat.tests.utils import (
    _make_content_chunk,
    _mock_openai_client,
    blocks_from_text,
)


class BuildContextTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=session)

    def test_builds_context_with_system_prompt_and_history(self):
        Message.objects.create(
            thread=self.thread,
            role="user",
            blocks=blocks_from_text("Hello"),
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.thread,
            role="assistant",
            blocks=blocks_from_text("Hi there!"),
            sequence_index=2,
        )

        context = build_context(self.user, "What resources?", thread=self.thread)

        # context is a list[dict] in OpenAI messages format
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertIn("a highly knowledgeable", system_msg["content"])
        self.assertIn("SCOPE: Waldur CLOUD MANAGEMENT ONLY", system_msg["content"])
        self.assertIn(UI_CAPABILITIES, system_msg["content"])
        self.assertIn("TOOL USAGE GUIDELINES", system_msg["content"])
        history_roles_contents = [(m["role"], m["content"]) for m in context]
        self.assertIn(("user", "Hello"), history_roles_contents)
        self.assertIn(("assistant", "Hi there!"), history_roles_contents)
        self.assertIn(("user", "What resources?"), history_roles_contents)

    def test_no_thread_skips_history(self):
        context = build_context(self.user, "Hello", thread=None)

        system_msg = next(m for m in context if m["role"] == "system")
        self.assertIn("a highly knowledgeable", system_msg["content"])
        roles = [m["role"] for m in context]
        self.assertIn("user", roles)
        self.assertNotIn("assistant", roles)
        user_msgs = [m for m in context if m["role"] == "user"]
        self.assertTrue(any("Hello" in m["content"] for m in user_msgs))

    @override_constance_config(AI_ASSISTANT_NAME="Mari", SITE_NAME="ETAIS")
    def test_custom_assistant_identity_in_system_prompt(self):
        context = build_context(self.user, "Hello", thread=None)
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertIn("Mari", system_msg["content"])
        self.assertIn("ETAIS", system_msg["content"])
        self.assertNotIn("{assistant_name}", system_msg["content"])
        self.assertNotIn("{organization}", system_msg["content"])

    def test_conversation_history_chronological_order(self):
        Message.objects.create(
            thread=self.thread,
            role="user",
            blocks=blocks_from_text("First"),
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.thread,
            role="assistant",
            blocks=blocks_from_text("Second"),
            sequence_index=2,
        )
        Message.objects.create(
            thread=self.thread,
            role="user",
            blocks=blocks_from_text("Third"),
            sequence_index=3,
        )

        context = build_context(self.user, "Fourth", thread=self.thread)

        roles_contents = [(m["role"], m["content"]) for m in context]
        first_pos = next(
            i for i, (r, c) in enumerate(roles_contents) if r == "user" and c == "First"
        )
        second_pos = next(
            i
            for i, (r, c) in enumerate(roles_contents)
            if r == "assistant" and c == "Second"
        )
        third_pos = next(
            i for i, (r, c) in enumerate(roles_contents) if r == "user" and c == "Third"
        )
        fourth_pos = next(
            i
            for i, (r, c) in enumerate(roles_contents)
            if r == "user" and c == "Fourth"
        )

        self.assertLess(first_pos, second_pos)
        self.assertLess(second_pos, third_pos)
        self.assertLess(third_pos, fourth_pos)

    def test_excludes_replaced_messages(self):
        original = Message.objects.create(
            thread=self.thread,
            role="user",
            blocks=blocks_from_text("Original"),
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.thread,
            role="user",
            blocks=blocks_from_text("Edited"),
            sequence_index=1,
            replaces=original,
        )

        context = build_context(self.user, "Next message", thread=self.thread)

        # Only the replacement (which has no replaced_by) should appear
        all_contents = [m["content"] for m in context]
        self.assertIn("Edited", all_contents)
        self.assertNotIn("Original", all_contents)

    @override_constance_config(AI_ASSISTANT_HISTORY_LIMIT=50)
    def test_caps_at_history_limit(self):
        for i in range(60):
            Message.objects.create(
                thread=self.thread,
                role="user" if i % 2 == 0 else "assistant",
                blocks=blocks_from_text(f"Message {i}"),
                sequence_index=i + 1,
            )

        context = build_context(self.user, "Latest", thread=self.thread)

        all_contents = " ".join(m["content"] for m in context)
        # First 50 messages should be included (sequence_index 1-50 → Message 0-49)
        for i in range(50):
            self.assertIn(f"Message {i}", all_contents)

        # Messages beyond 50 should NOT be included
        for i in range(50, 60):
            self.assertNotIn(f"Message {i}", all_contents)

    def test_tool_call_history_reconstructed_for_llm(self):
        """Assistant messages with tool blocks are expanded into assistant + tool messages."""
        Message.objects.create(
            thread=self.thread,
            role="assistant",
            blocks=[
                {
                    "id": "blk_0",
                    "key": "tool",
                    "status": "complete",
                    "tool": {
                        "call_id": "call_abc123",
                        "name": "show_user_resources",
                        "arguments": {},
                        "summary": "",
                    },
                    "result": {
                        "id": "blk_0_r",
                        "key": "markdown",
                        "status": "complete",
                        "content": "",
                    },
                }
            ],
            sequence_index=1,
        )

        context = build_context(self.user, "Follow-up", thread=self.thread)

        roles = [m["role"] for m in context]
        # assistant message should be followed by a tool result message
        asst_idx = next(i for i, m in enumerate(context) if m["role"] == "assistant")
        self.assertEqual(context[asst_idx + 1]["role"], "tool")

        # assistant message must carry tool_calls in OpenAI format
        asst_msg = context[asst_idx]
        self.assertEqual(len(asst_msg["tool_calls"]), 1)
        tc = asst_msg["tool_calls"][0]
        self.assertEqual(tc["id"], "call_abc123")
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "show_user_resources")
        self.assertEqual(tc["function"]["arguments"], "{}")

        # tool message must reference the same call id
        tool_msg = context[asst_idx + 1]
        self.assertEqual(tool_msg["tool_call_id"], "call_abc123")
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)

    def test_raises_permission_denied_for_other_users_thread(self):
        other_user = structure_factories.UserFactory()
        with self.assertRaises(PermissionDenied):
            build_context(other_user, "Hello", thread=self.thread)

    @override_constance_config(AI_ASSISTANT_HISTORY_LIMIT=-1)
    def test_invalid_history_limit_skips_history(self):
        Message.objects.create(
            thread=self.thread,
            role="user",
            blocks=blocks_from_text("Old msg"),
            sequence_index=1,
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        all_contents = " ".join(m["content"] for m in context)
        self.assertIn("Hello", all_contents)
        self.assertNotIn("Old msg", all_contents)


class BuildContextWithIntentTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=session)

    def test_returns_context_result(self):
        result = build_context_with_intent(self.user, "hello")
        self.assertIsInstance(result, ContextResult)
        self.assertIsInstance(result.messages, list)
        self.assertIsInstance(result.intent, Intent)

    def test_greeting_classified(self):
        result = build_context_with_intent(self.user, "hello")
        self.assertEqual(result.intent, Intent.GREETING)

    def test_tool_action_classified(self):
        result = build_context_with_intent(self.user, "show my resources")
        self.assertEqual(result.intent, Intent.TOOL_ACTION)

    def test_knowledge_classified_as_ambiguous(self):
        """Knowledge queries are now AMBIGUOUS (tools included, LLM decides)."""
        result = build_context_with_intent(self.user, "what is a VM?")
        self.assertEqual(result.intent, Intent.AMBIGUOUS)

    def test_messages_match_build_context(self):
        result = build_context_with_intent(
            self.user, "show my resources", thread=self.thread
        )
        expected = build_context(self.user, "show my resources", thread=self.thread)
        self.assertEqual(len(result.messages), len(expected))
        self.assertEqual(result.messages[0]["role"], expected[0]["role"])

    def test_knowledge_intent_includes_tool_instructions(self):
        """Knowledge queries now include tools (AMBIGUOUS) so the LLM decides."""
        result = build_context_with_intent(self.user, "what is a VM?")
        system_msg = next(m for m in result.messages if m["role"] == "system")
        self.assertIn("TOOL USAGE GUIDELINES", system_msg["content"])

    def test_tool_action_intent_includes_tool_instructions(self):
        """When intent is TOOL_ACTION, tool usage guidelines should appear."""
        result = build_context_with_intent(self.user, "show my resources")
        system_msg = next(m for m in result.messages if m["role"] == "system")
        self.assertIn("TOOL USAGE GUIDELINES", system_msg["content"])

    def test_mid_workflow_returns_ambiguous(self):
        """History with tool blocks should override to AMBIGUOUS."""
        Message.objects.create(
            thread=self.thread,
            role="assistant",
            blocks=[
                {
                    "id": "blk_0",
                    "key": "tool",
                    "status": "complete",
                    "tool": {
                        "call_id": "call_abc",
                        "name": "show_user_resources",
                        "arguments": {},
                        "summary": "",
                    },
                    "result": {
                        "id": "blk_0_r",
                        "key": "markdown",
                        "status": "complete",
                        "content": "",
                    },
                }
            ],
            sequence_index=1,
        )
        result = build_context_with_intent(
            self.user, "what is this?", thread=self.thread
        )
        self.assertEqual(result.intent, Intent.AMBIGUOUS)


class ChatStreamIntegrationTest(test.APITestCase):
    """Integration tests for the stream endpoint with backend context assembly."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)
        self.stream_url = reverse("chat-stream")

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
    )
    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_stream_response(self, mock_openai_cls):
        mock_client = _mock_openai_client(
            [_make_content_chunk("Hello from backend context!")]
        )
        mock_openai_cls.return_value = mock_client

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
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
    )
    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_sends_assembled_context_to_llm(self, mock_openai_cls):
        # Use an existing thread so title generation is not triggered
        session = ChatSession.objects.get_or_create(user=self.user)[0]
        thread = ThreadSession.objects.create(chat_session=session)

        mock_client = _mock_openai_client([_make_content_chunk("Response")])
        mock_openai_cls.return_value = mock_client

        response = self.client.post(
            self.stream_url,
            data={"input": "Test message", "thread_uuid": str(thread.uuid)},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        list(response.streaming_content)

        # Only one client instantiated (main stream) — no title generation for existing thread
        mock_openai_cls.assert_called_once()
        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args
        sent_messages = call_kwargs.kwargs["messages"]
        system_content = next(
            m["content"] for m in sent_messages if m["role"] == "system"
        )
        user_content = next(
            m["content"] for m in reversed(sent_messages) if m["role"] == "user"
        )
        self.assertIn("a highly knowledgeable", system_content)
        self.assertIn("Test message", user_content)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
    )
    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_persists_raw_input_not_context(self, mock_openai_cls):
        mock_client = _mock_openai_client([_make_content_chunk("Assistant reply")])
        mock_openai_cls.return_value = mock_client

        response = self.client.post(self.stream_url, data={"input": "My raw question"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        list(response.streaming_content)

        # The persisted user message should be the raw message, not the full context
        user_msg = Message.objects.filter(role="user").first()
        self.assertIsNotNone(user_msg)
        self.assertEqual(user_msg.blocks[0]["content"], "My raw question")

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
    )
    def test_missing_input_rejected(self):
        response = self.client.post(self.stream_url, data={})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
