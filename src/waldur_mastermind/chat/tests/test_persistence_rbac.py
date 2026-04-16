import json
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from constance.test.unittest import override_config as override_constance_config
from django.db.models import Max
from django.urls import reverse
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.llm_streamer import LLMStreamer
from waldur_mastermind.chat.models import ChatMode, ChatSession, Message, ThreadSession
from waldur_mastermind.chat.serializers import ChatRequestSerializer
from waldur_mastermind.chat.tests.utils import (
    SYNC_THREAD_PATCH,
    _make_content_chunk,
    _make_usage_chunk,
    _mock_openai_client,
    _SynchronousThread,
    blocks_from_text,
    text_from_blocks,
)


@patch(SYNC_THREAD_PATCH, _SynchronousThread)
class LLMStreamerPersistenceTest(test.APITestCase):
    """Verify that LLMStreamer._persist_messages writes (or skips) Message rows."""

    def _make_thread(self, user):
        session = ChatSession.objects.create(user=user)
        return ThreadSession.objects.create(chat_session=session)

    def _content_and_usage_chunks(self, content="Hello"):
        """Minimal OpenAI SDK chunks: one content chunk, then usage metadata."""
        return [
            _make_content_chunk(content),
            _make_usage_chunk(10, 5),
        ]

    def _pre_create_user_msg(self, thread, content):
        """Simulate what ChatViewSet.stream() does: pre-create the user message."""
        last_index = (
            thread.messages.aggregate(Max("sequence_index"))["sequence_index__max"] or 0
        )
        return Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text(content),
            sequence_index=last_index + 1,
        )

    def _pre_create_assistant_placeholder(self, thread, user_msg=None, replace=False):
        """Simulate what ChatViewSet._persist_messages() does for the assistant slot.

        replace=True for EDIT/RELOAD (replaces last active assistant).
        replace=False for normal mode (appends at user_msg.sequence_index + 1).
        """
        if replace:
            last_assistant = (
                thread.messages.filter(
                    role=Message.Role.ASSISTANT,
                    replaced_by__isnull=True,
                )
                .order_by("-sequence_index")
                .first()
            )
            if last_assistant:
                return Message.objects.create(
                    thread=thread,
                    role=Message.Role.ASSISTANT,
                    blocks=[],
                    sequence_index=last_assistant.sequence_index,
                    replaces=last_assistant,
                )
            return None
        if user_msg:
            return Message.objects.create(
                thread=thread,
                role=Message.Role.ASSISTANT,
                blocks=[],
                sequence_index=user_msg.sequence_index + 1,
            )
        return None

    def _run_streamer(self, user, thread, original_input, chunks, mode=None):
        # Pre-create messages like the view does
        user_msg = None
        assistant_msg = None
        if thread:
            if mode != ChatMode.RELOAD:
                user_msg = self._pre_create_user_msg(thread, original_input)
            if mode in (ChatMode.RELOAD, ChatMode.EDIT):
                assistant_msg = self._pre_create_assistant_placeholder(
                    thread, replace=True
                )
            else:
                assistant_msg = self._pre_create_assistant_placeholder(
                    thread, user_msg=user_msg
                )
        with patch(
            "waldur_mastermind.chat.llm_streamer.openai.OpenAI"
        ) as mock_openai_cls:
            mock_openai_cls.return_value = _mock_openai_client(chunks)
            streamer = LLMStreamer(
                [{"role": "user", "content": original_input}],
                "https://llm/stream",
                "tok",
                user=user,
                thread=thread,
                original_input=original_input,
                mode=mode,
                user_msg=user_msg,
                assistant_msg=assistant_msg,
            )
            list(streamer)  # generator must be fully consumed for finally block

    def test_messages_persisted(self):
        """Both user and assistant messages appear with correct indices."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        self._run_streamer(
            user, thread, "User question", self._content_and_usage_chunks()
        )

        user_msg = Message.objects.get(thread=thread, role=Message.Role.USER)
        assistant_msg = Message.objects.get(thread=thread, role=Message.Role.ASSISTANT)

        self.assertEqual(text_from_blocks(user_msg.blocks), "User question")
        self.assertEqual(text_from_blocks(assistant_msg.blocks), "Hello")
        self.assertEqual(user_msg.sequence_index, 1)
        self.assertEqual(assistant_msg.sequence_index, 2)

    def test_messages_not_persisted_when_thread_is_none(self):
        user = structure_factories.UserFactory()

        self._run_streamer(user, None, "Q", self._content_and_usage_chunks())

        self.assertEqual(Message.objects.count(), 0)

    def test_persistence_appends_after_existing_messages(self):
        """New messages pick up sequence_index after the last existing one."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Old Q"),
            sequence_index=1,
        )
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Old A"),
            sequence_index=2,
        )

        self._run_streamer(user, thread, "New Q", self._content_and_usage_chunks())

        new_user = Message.objects.get(thread=thread, sequence_index=3)
        new_asst = Message.objects.get(thread=thread, sequence_index=4)
        self.assertEqual(text_from_blocks(new_user.blocks), "New Q")
        self.assertEqual(text_from_blocks(new_asst.blocks), "Hello")

    def test_partial_content_persisted_on_stream_error(self):
        """Unexpected errors are caught, an error frame is yielded, and partial content is persisted."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        def failing_chunks():
            yield _make_content_chunk("Partial")
            raise Exception("dropped")

        user_msg = self._pre_create_user_msg(thread, "Q")

        with patch(
            "waldur_mastermind.chat.llm_streamer.openai.OpenAI"
        ) as mock_openai_cls:
            client = MagicMock()

            @contextmanager
            def _failing_stream(*args, **kwargs):
                yield failing_chunks()

            client.chat.completions.create.return_value = _failing_stream()
            mock_openai_cls.return_value = client
            streamer = LLMStreamer(
                [{"role": "user", "content": "Q"}],
                "https://llm/stream",
                "tok",
                user=user,
                thread=thread,
                original_input="Q",
                user_msg=user_msg,
            )
            # Exception is caught internally; error frame is yielded and messages persisted
            frames = list(streamer)

        error_frames = [f for f in frames if '"e"' in f]
        self.assertTrue(error_frames, "Expected an error frame in the stream output")

        self.assertEqual(
            text_from_blocks(
                Message.objects.get(thread=thread, role=Message.Role.USER).blocks
            ),
            "Q",
        )
        self.assertEqual(
            text_from_blocks(
                Message.objects.get(thread=thread, role=Message.Role.ASSISTANT).blocks
            ),
            "Partial",
        )

    def test_empty_assistant_content_persisted(self):
        """Metadata-only stream still creates an assistant message with empty content."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        # Only a usage chunk, no content
        metadata_only = [_make_usage_chunk(5, 0)]

        self._run_streamer(user, thread, "Hi", metadata_only)

        assistant_msg = Message.objects.get(thread=thread, role=Message.Role.ASSISTANT)
        self.assertEqual(assistant_msg.blocks, [])

    def test_reload_mode_replaces_last_assistant_only(self):
        """reload mode creates replacement assistant message, no new user message."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        # Create initial pair
        Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Question"),
            sequence_index=1,
        )
        original_assistant = Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Old answer"),
            sequence_index=2,
        )

        # Run streamer in reload mode (no user_msg pre-created for reload)
        self._run_streamer(
            user,
            thread,
            "Ignored input",
            self._content_and_usage_chunks("New answer"),
            mode=ChatMode.RELOAD,
        )

        # Should have 1 user message (original) and 2 assistant messages (original + replacement)
        self.assertEqual(
            Message.objects.filter(thread=thread, role=Message.Role.USER).count(), 1
        )
        self.assertEqual(
            Message.objects.filter(thread=thread, role=Message.Role.ASSISTANT).count(),
            2,
        )

        # Find the replacement
        replacement = Message.objects.get(
            thread=thread, role=Message.Role.ASSISTANT, replaces=original_assistant
        )
        self.assertEqual(text_from_blocks(replacement.blocks), "New answer")
        self.assertEqual(replacement.sequence_index, 2)  # Same index as original
        self.assertEqual(replacement.replaces, original_assistant)

    def test_reload_mode_falls_back_when_no_assistant(self):
        """reload mode falls back to normal mode if no assistant message exists."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        # Thread is empty, no assistant to replace -- reload falls back to normal
        self._run_streamer(
            user,
            thread,
            "Question",
            self._content_and_usage_chunks("Answer"),
            mode=ChatMode.RELOAD,
        )

        # Should create normal pair (fallback behavior)
        self.assertEqual(
            Message.objects.filter(thread=thread, role=Message.Role.USER).count(), 1
        )
        self.assertEqual(
            Message.objects.filter(thread=thread, role=Message.Role.ASSISTANT).count(),
            1,
        )

        user_msg = Message.objects.get(thread=thread, role=Message.Role.USER)
        assistant_msg = Message.objects.get(thread=thread, role=Message.Role.ASSISTANT)
        self.assertEqual(text_from_blocks(user_msg.blocks), "Question")
        self.assertEqual(text_from_blocks(assistant_msg.blocks), "Answer")
        self.assertIsNone(assistant_msg.replaces)

    def test_stream_returns_message_uuids(self):
        """Stream yields metadata with user_message_uuid and assistant_message_uuid."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        user_msg = self._pre_create_user_msg(thread, "Question")

        with patch(
            "waldur_mastermind.chat.llm_streamer.openai.OpenAI"
        ) as mock_openai_cls:
            mock_openai_cls.return_value = _mock_openai_client(
                self._content_and_usage_chunks("Answer")
            )
            streamer = LLMStreamer(
                [{"role": "user", "content": "Question"}],
                "https://llm/stream",
                "tok",
                user=user,
                thread=thread,
                original_input="Question",
                user_msg=user_msg,
            )
            output = list(streamer)

        # Parse all NDJSON lines
        initial_metadata = None
        persist_metadata = None
        for line in output:
            if line.strip():
                obj = json.loads(line)
                if "m" in obj:
                    meta = obj["m"]
                    if "thread_uuid" in meta:
                        initial_metadata = meta
                    if "assistant_message_uuid" in meta:
                        persist_metadata = meta

        self.assertIsNotNone(initial_metadata)
        self.assertIn("thread_uuid", initial_metadata)
        self.assertIsNotNone(persist_metadata)
        self.assertIn("user_message_uuid", persist_metadata)
        self.assertIn("assistant_message_uuid", persist_metadata)

        # Verify UUIDs match actual messages
        user_msg_db = Message.objects.get(thread=thread, role=Message.Role.USER)
        assistant_msg = Message.objects.get(thread=thread, role=Message.Role.ASSISTANT)
        self.assertEqual(persist_metadata["user_message_uuid"], str(user_msg_db.uuid))
        self.assertEqual(
            persist_metadata["assistant_message_uuid"], str(assistant_msg.uuid)
        )

    def test_default_mode_unchanged(self):
        """Normal mode (mode=None) works as before."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        self._run_streamer(
            user, thread, "User question", self._content_and_usage_chunks()
        )

        user_msg = Message.objects.get(thread=thread, role=Message.Role.USER)
        assistant_msg = Message.objects.get(thread=thread, role=Message.Role.ASSISTANT)

        self.assertEqual(text_from_blocks(user_msg.blocks), "User question")
        self.assertEqual(text_from_blocks(assistant_msg.blocks), "Hello")
        self.assertIsNone(user_msg.replaces)
        self.assertIsNone(assistant_msg.replaces)

    def test_user_message_persisted_before_streaming(self):
        """User message exists in DB before the generator is consumed."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        user_msg = self._pre_create_user_msg(thread, "My question")

        with patch(
            "waldur_mastermind.chat.llm_streamer.openai.OpenAI"
        ) as mock_openai_cls:
            mock_openai_cls.return_value = _mock_openai_client(
                self._content_and_usage_chunks("Answer")
            )
            streamer = LLMStreamer(
                [{"role": "user", "content": "My question"}],
                "https://llm/stream",
                "tok",
                user=user,
                thread=thread,
                original_input="My question",
                user_msg=user_msg,
            )
            # Do NOT consume the generator -- the user message should already exist
            self.assertEqual(
                Message.objects.filter(thread=thread, role=Message.Role.USER).count(),
                1,
            )
            db_msg = Message.objects.get(thread=thread, role=Message.Role.USER)
            self.assertEqual(text_from_blocks(db_msg.blocks), "My question")
            self.assertEqual(db_msg.sequence_index, 1)

            # No assistant message yet (generator not consumed)
            self.assertEqual(
                Message.objects.filter(
                    thread=thread, role=Message.Role.ASSISTANT
                ).count(),
                0,
            )

            # Now consume -- assistant message should appear
            list(streamer)

        self.assertEqual(
            Message.objects.filter(thread=thread, role=Message.Role.ASSISTANT).count(),
            1,
        )
        assistant_msg = Message.objects.get(thread=thread, role=Message.Role.ASSISTANT)
        self.assertEqual(text_from_blocks(assistant_msg.blocks), "Answer")
        self.assertEqual(assistant_msg.sequence_index, 2)

    def test_user_message_uuid_in_persist_metadata(self):
        """Persist metadata includes user_message_uuid when pre-created."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)

        user_msg = self._pre_create_user_msg(thread, "Q")

        with patch(
            "waldur_mastermind.chat.llm_streamer.openai.OpenAI"
        ) as mock_openai_cls:
            mock_openai_cls.return_value = _mock_openai_client(
                self._content_and_usage_chunks("A")
            )
            streamer = LLMStreamer(
                [{"role": "user", "content": "Q"}],
                "https://llm/stream",
                "tok",
                user=user,
                thread=thread,
                original_input="Q",
                user_msg=user_msg,
            )
            output = list(streamer)

        # Find persist metadata line (emitted after messages are saved)
        persist_meta = None
        for line in output:
            if line.strip():
                obj = json.loads(line)
                if "m" in obj and "user_message_uuid" in obj["m"]:
                    persist_meta = obj["m"]

        self.assertIsNotNone(persist_meta)
        self.assertEqual(persist_meta["user_message_uuid"], str(user_msg.uuid))

    @freeze_time("2025-01-01 12:00:00", as_kwarg="frozen_time")
    def test_thread_modified_updated_on_message_persist(self, frozen_time):
        """Thread's modified timestamp is updated when messages are added."""
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)
        initial_modified = thread.modified

        frozen_time.tick()

        self._run_streamer(
            user, thread, "New message", self._content_and_usage_chunks()
        )

        thread.refresh_from_db()
        self.assertGreater(thread.modified, initial_modified)


@override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class ChatSessionRBACTest(test.APITestCase):
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


@override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class ThreadSessionRBACTest(test.APITestCase):
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

    def test_query_filter_matches_thread_name(self):
        """ThreadSessionFilter.query matches on thread name."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.list_url, {"query": "Mine"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.user_thread.uuid), uuids)
        self.assertNotIn(str(self.other_thread.uuid), uuids)

    def test_query_filter_matches_username(self):
        """ThreadSessionFilter.query matches on user's username."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.list_url, {"query": self.other_user.username})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.other_thread.uuid), uuids)

    @freeze_time("2030-01-01 00:00:00")
    def test_ordering_by_modified(self):
        """ThreadSessionFilter ordering by modified works."""
        self.client.force_authenticate(user=self.staff)

        # Touch user_thread to make it more recently modified than setUp timestamps
        self.user_thread.save(update_fields=["modified"])

        response = self.client.get(self.list_url, {"o": "-modified"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        user_idx = uuids.index(str(self.user_thread.uuid))
        other_idx = uuids.index(str(self.other_thread.uuid))
        self.assertLess(user_idx, other_idx)

    def test_filter_by_created_date(self):
        """ThreadSessionFilter.created filters by date."""
        self.client.force_authenticate(user=self.staff)
        today = self.user_thread.created.strftime("%Y-%m-%d")
        response = self.client.get(self.list_url, {"created": today})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(self.user_thread.uuid), uuids)

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


class ChatRequestSerializerTest(test.APITestCase):
    """Test ChatRequestSerializer validation."""

    def test_mode_requires_thread_uuid(self):
        """mode='reload' requires thread_uuid."""
        # mode without thread_uuid is invalid
        serializer = ChatRequestSerializer(data={"input": "test", "mode": "reload"})
        self.assertFalse(serializer.is_valid())
        self.assertIn("mode", serializer.errors)

        # mode with thread_uuid is valid
        thread_uuid = str(uuid.uuid4())
        serializer = ChatRequestSerializer(
            data={"input": "test", "mode": "reload", "thread_uuid": thread_uuid}
        )
        self.assertTrue(serializer.is_valid())

    def test_mode_rejects_invalid_values(self):
        """mode only accepts 'reload' or None."""
        thread_uuid = str(uuid.uuid4())

        # Invalid mode value
        serializer = ChatRequestSerializer(
            data={"input": "test", "mode": "invalid", "thread_uuid": thread_uuid}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("mode", serializer.errors)

    def test_mode_omitted_is_valid(self):
        """Omitting mode field works (backward compatibility)."""
        serializer = ChatRequestSerializer(data={"input": "test"})
        self.assertTrue(serializer.is_valid())
        self.assertIsNone(serializer.validated_data.get("mode"))

    def test_edit_mode_requires_edit_message_uuid(self):
        """mode='edit' without edit_message_uuid should fail validation."""
        thread_uuid = str(uuid.uuid4())
        serializer = ChatRequestSerializer(
            data={"input": "test", "mode": "edit", "thread_uuid": thread_uuid}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("edit_message_uuid", serializer.errors)

    def test_edit_mode_with_edit_message_uuid_valid(self):
        """mode='edit' with edit_message_uuid and thread_uuid should pass."""
        thread_uuid = str(uuid.uuid4())
        msg_uuid = str(uuid.uuid4())
        serializer = ChatRequestSerializer(
            data={
                "input": "edited text",
                "mode": "edit",
                "thread_uuid": thread_uuid,
                "edit_message_uuid": msg_uuid,
            }
        )
        self.assertTrue(serializer.is_valid())

    def test_edit_message_uuid_rejected_without_edit_mode(self):
        """edit_message_uuid without mode='edit' should fail validation."""
        thread_uuid = str(uuid.uuid4())
        msg_uuid = str(uuid.uuid4())
        serializer = ChatRequestSerializer(
            data={
                "input": "test",
                "thread_uuid": thread_uuid,
                "edit_message_uuid": msg_uuid,
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("edit_message_uuid", serializer.errors)


@override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class MessageRBACTest(test.APITestCase):
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
            thread=thread_a,
            role=Message.Role.USER,
            blocks=blocks_from_text("A"),
            sequence_index=1,
        )
        self.msg_b = Message.objects.create(
            thread=thread_b,
            role=Message.Role.USER,
            blocks=blocks_from_text("B"),
            sequence_index=1,
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
