import json
from unittest.mock import Mock, patch

import requests
from django.urls import reverse
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import ChatSession, Message, ThreadSession
from waldur_mastermind.chat.views import LLMStreamer


class LLMStreamerPersistenceTest(test.APITransactionTestCase):
    """Verify that LLMStreamer._persist_messages writes (or skips) Message rows."""

    def _fake_response(self, lines):
        resp = Mock()
        resp.iter_lines.return_value = lines
        resp.raise_for_status = Mock()
        return resp

    def _make_thread(self, user):
        session = ChatSession.objects.create(user=user)
        return ThreadSession.objects.create(chat_session=session)

    def _content_and_usage_lines(self, content="Hello"):
        """Minimal SSE stream: one content chunk, then usage metadata."""
        return [
            "data: " + json.dumps({"content": content}),
            "data: "
            + json.dumps(
                {
                    "additional_kwargs": {
                        "usage_metadata": {"input_tokens": 10, "output_tokens": 5}
                    }
                }
            ),
        ]

    def _run_streamer(self, user, thread, storage_enabled, original_input, lines):
        resp = self._fake_response(lines)
        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = resp
            streamer = LLMStreamer(
                original_input,
                "https://llm/stream",
                "tok",
                user=user,
                thread=thread,
                storage_enabled=storage_enabled,
                original_input=original_input,
            )
            list(streamer)  # generator must be fully consumed for finally block

    def test_messages_persisted_when_storage_enabled(self):
        """Both user and assistant messages appear with correct indices."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        self._run_streamer(
            user, thread, True, "User question", self._content_and_usage_lines()
        )

        user_msg = Message.objects.get(thread=thread, role=Message.Role.USER)
        assistant_msg = Message.objects.get(thread=thread, role=Message.Role.ASSISTANT)

        self.assertEqual(user_msg.content, "User question")
        self.assertEqual(assistant_msg.content, "Hello")
        self.assertEqual(user_msg.sequence_index, 1)
        self.assertEqual(assistant_msg.sequence_index, 2)

    def test_messages_not_persisted_when_storage_disabled(self):
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        self._run_streamer(user, thread, False, "Q", self._content_and_usage_lines())

        self.assertEqual(Message.objects.filter(thread=thread).count(), 0)

    def test_messages_not_persisted_when_thread_is_none(self):
        user = structure_factories.UserFactory()

        self._run_streamer(user, None, True, "Q", self._content_and_usage_lines())

        self.assertEqual(Message.objects.count(), 0)

    def test_persistence_appends_after_existing_messages(self):
        """New messages pick up sequence_index after the last existing one."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        Message.objects.create(
            thread=thread, role=Message.Role.USER, content="Old Q", sequence_index=1
        )
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            content="Old A",
            sequence_index=2,
        )

        self._run_streamer(user, thread, True, "New Q", self._content_and_usage_lines())

        new_user = Message.objects.get(thread=thread, content="New Q")
        new_asst = Message.objects.get(thread=thread, content="Hello")
        self.assertEqual(new_user.sequence_index, 3)
        self.assertEqual(new_asst.sequence_index, 4)

    def test_partial_content_persisted_on_stream_error(self):
        """The finally block runs even when iter_lines raises mid-stream."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        def failing_lines():
            yield "data: " + json.dumps({"content": "Partial"})
            raise requests.ConnectionError("dropped")

        resp = Mock()
        resp.iter_lines.return_value = failing_lines()
        resp.raise_for_status = Mock()

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = resp
            streamer = LLMStreamer(
                "Q",
                "https://llm/stream",
                "tok",
                user=user,
                thread=thread,
                storage_enabled=True,
                original_input="Q",
            )
            list(streamer)

        self.assertEqual(
            Message.objects.get(thread=thread, role=Message.Role.USER).content, "Q"
        )
        self.assertEqual(
            Message.objects.get(thread=thread, role=Message.Role.ASSISTANT).content,
            "Partial",
        )

    def test_empty_assistant_content_persisted(self):
        """Metadata-only stream still creates an assistant message with empty content."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        metadata_only = [
            "data: "
            + json.dumps(
                {
                    "additional_kwargs": {
                        "usage_metadata": {"input_tokens": 5, "output_tokens": 0}
                    }
                }
            ),
        ]

        self._run_streamer(user, thread, True, "Hi", metadata_only)

        assistant_msg = Message.objects.get(thread=thread, role=Message.Role.ASSISTANT)
        self.assertEqual(assistant_msg.content, "")


class ChatSessionRBACTest(test.APITransactionTestCase):
    """Staff and support see all sessions; audit fires on cross-user retrieve."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)

        self.user_session = ChatSession.objects.create(user=self.user)
        self.other_session = ChatSession.objects.create(user=self.other_user)

        self.list_url = reverse("chat-session-list")

    def test_staff_sees_all_sessions(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.user_session.uuid), uuids)
        self.assertIn(str(self.other_session.uuid), uuids)

    def test_support_sees_all_sessions(self):
        self.client.force_authenticate(user=self.support)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.user_session.uuid), uuids)
        self.assertIn(str(self.other_session.uuid), uuids)

    def test_list_includes_user_detail_fields(self):
        """ChatSessionSerializer exposes user_username and user_full_name."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.list_url)

        target = next(
            (r for r in response.data if r["uuid"] == str(self.user_session.uuid)), None
        )
        self.assertIsNotNone(target)
        self.assertEqual(target["user_username"], self.user.username)
        self.assertEqual(target["user_full_name"], self.user.full_name)

    @patch("waldur_mastermind.chat.views.event_logger")
    def test_staff_retrieve_other_session_emits_audit(self, mock_event_logger):
        self.client.force_authenticate(user=self.staff)
        url = reverse("chat-session-detail", kwargs={"uuid": self.other_session.uuid})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_event_logger.emit.assert_called_once()
        self.assertEqual(
            mock_event_logger.emit.call_args[1]["event_type"],
            EventType.CHAT_SESSION_ACCESSED,
        )

    @patch("waldur_mastermind.chat.views.event_logger")
    def test_own_session_retrieve_does_not_emit_audit(self, mock_event_logger):
        self.client.force_authenticate(user=self.user)
        url = reverse("chat-session-detail", kwargs={"uuid": self.user_session.uuid})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_event_logger.emit.assert_not_called()


class ThreadSessionRBACTest(test.APITransactionTestCase):
    """Staff sees all threads, can filter by user, audit fires on cross-user retrieve."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)

        self.user_session = ChatSession.objects.create(user=self.user)
        self.other_session = ChatSession.objects.create(user=self.other_user)

        self.user_thread = ThreadSession.objects.create(
            chat_session=self.user_session, name="Mine"
        )
        self.other_thread = ThreadSession.objects.create(
            chat_session=self.other_session, name="Theirs"
        )

        self.list_url = reverse("chat-thread-list")

    def test_staff_sees_all_threads(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.user_thread.uuid), uuids)
        self.assertIn(str(self.other_thread.uuid), uuids)

    def test_staff_filter_by_user_uuid(self):
        """ThreadSessionFilter.user scopes the list to a single owner."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.list_url, {"user": str(self.other_user.uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.other_thread.uuid), uuids)
        self.assertNotIn(str(self.user_thread.uuid), uuids)

    @patch("waldur_mastermind.chat.views.event_logger")
    def test_staff_retrieve_other_thread_emits_audit(self, mock_event_logger):
        self.client.force_authenticate(user=self.staff)
        url = reverse("chat-thread-detail", kwargs={"uuid": self.other_thread.uuid})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_event_logger.emit.assert_called_once()
        self.assertEqual(
            mock_event_logger.emit.call_args[1]["event_type"],
            EventType.CHAT_THREAD_ACCESSED,
        )

    @patch("waldur_mastermind.chat.views.event_logger")
    def test_own_thread_retrieve_does_not_emit_audit(self, mock_event_logger):
        self.client.force_authenticate(user=self.user)
        url = reverse("chat-thread-detail", kwargs={"uuid": self.user_thread.uuid})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_event_logger.emit.assert_not_called()


class MessageRBACTest(test.APITransactionTestCase):
    """Staff and support can list messages across all users."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)

        user_a = structure_factories.UserFactory()
        user_b = structure_factories.UserFactory()

        session_a = ChatSession.objects.create(user=user_a)
        session_b = ChatSession.objects.create(user=user_b)
        thread_a = ThreadSession.objects.create(chat_session=session_a)
        thread_b = ThreadSession.objects.create(chat_session=session_b)

        self.msg_a = Message.objects.create(
            thread=thread_a, role=Message.Role.USER, content="A", sequence_index=1
        )
        self.msg_b = Message.objects.create(
            thread=thread_b, role=Message.Role.USER, content="B", sequence_index=1
        )

        self.list_url = reverse("chat-message-list")

    def test_staff_sees_all_messages(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.msg_a.uuid), uuids)
        self.assertIn(str(self.msg_b.uuid), uuids)

    def test_support_sees_all_messages(self):
        self.client.force_authenticate(user=self.support)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.msg_a.uuid), uuids)
        self.assertIn(str(self.msg_b.uuid), uuids)
