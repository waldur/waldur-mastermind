import json
from unittest.mock import MagicMock, patch

from constance.test.unittest import override_config as override_constance_config
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.llm_streamer import LLMStreamer
from waldur_mastermind.chat.models import (
    ChatSession,
    Message,
    ThreadSession,
    TokenQuota,
)
from waldur_mastermind.chat.tests.utils import (
    SYNC_THREAD_PATCH,
    _fake_stream,
    _make_content_chunk,
    _make_usage_chunk,
    _SynchronousThread,
    blocks_from_text,
    markdown_block,
)


class MessageModelTest(test.APITestCase):
    """Test Message model business logic."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)

    def test_unique_sequence_index_per_thread_for_active_messages(self):
        """Active messages (not replaced) must have unique sequence_index per thread."""
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("First"),
            sequence_index=1,
        )

        # Creating another active message with same sequence_index should fail
        with self.assertRaises(Exception):  # IntegrityError
            Message.objects.create(
                thread=self.thread,
                role=Message.Role.USER,
                blocks=blocks_from_text("Duplicate"),
                sequence_index=1,
            )


@override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class ChatSessionViewSetTest(test.APITestCase):
    """Test ChatSessionViewSet endpoints."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)
        self.current_url = reverse("chat-session-current")

    def test_current_returns_existing_session(self):
        """current endpoint returns existing ChatSession."""
        session = ChatSession.objects.create(user=self.user)

        response = self.client.get(self.current_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(str(response.data["uuid"]), str(session.uuid))
        self.assertEqual(str(response.data["user"]), str(self.user.uuid))

    def test_current_creates_session_if_none_exists(self):
        """current endpoint creates ChatSession if user doesn't have one."""
        self.assertFalse(ChatSession.objects.filter(user=self.user).exists())

        response = self.client.get(self.current_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(ChatSession.objects.filter(user=self.user).exists())

    def test_current_is_idempotent(self):
        """current endpoint returns same session on repeated calls."""
        response1 = self.client.get(self.current_url)
        response2 = self.client.get(self.current_url)

        self.assertEqual(response1.data["uuid"], response2.data["uuid"])
        self.assertEqual(ChatSession.objects.filter(user=self.user).count(), 1)


@override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class ThreadSessionViewSetTest(test.APITestCase):
    """Test ThreadSessionViewSet endpoints."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        self.session = ChatSession.objects.create(user=self.user)
        self.other_session = ChatSession.objects.create(user=self.other_user)

        self.list_url = reverse("chat-thread-list")

    def test_list_returns_only_user_threads(self):
        """list endpoint returns only current user's threads."""
        my_thread = ThreadSession.objects.create(
            chat_session=self.session, name="My thread"
        )
        ThreadSession.objects.create(
            chat_session=self.other_session, name="Other thread"
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(my_thread.uuid))

    def test_list_includes_archived_when_filtered(self):
        """list endpoint includes archived threads when filtered."""
        ThreadSession.objects.create(
            chat_session=self.session, name="Active", is_archived=False
        )
        archived_thread = ThreadSession.objects.create(
            chat_session=self.session, name="Archived", is_archived=True
        )

        response = self.client.get(self.list_url, {"is_archived": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(archived_thread.uuid))

    def test_archive_thread(self):
        """archive action sets is_archived to True."""
        thread = ThreadSession.objects.create(
            chat_session=self.session, is_archived=False
        )
        url = reverse("chat-thread-archive", kwargs={"uuid": thread.uuid})

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        thread.refresh_from_db()
        self.assertTrue(thread.is_archived)

    def test_unarchive_thread(self):
        """unarchive action sets is_archived to False."""
        thread = ThreadSession.objects.create(
            chat_session=self.session, is_archived=True
        )
        # Need to access with is_archived filter to find archived threads
        url = reverse("chat-thread-unarchive", kwargs={"uuid": thread.uuid})

        # The queryset filters out archived by default, so we need to provide the filter
        response = self.client.post(url + "?is_archived=true")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        thread.refresh_from_db()
        self.assertFalse(thread.is_archived)

    def test_message_count_in_list_response(self):
        """List response includes message_count."""
        thread = ThreadSession.objects.create(chat_session=self.session)
        Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Test"),
            sequence_index=1,
        )
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Response"),
            sequence_index=2,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["message_count"], 2)

    def test_token_totals_in_list_response_for_staff(self):
        """Staff sees aggregated token totals from messages."""
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        thread = ThreadSession.objects.create(chat_session=self.session)
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Hi"),
            sequence_index=1,
            input_tokens=100,
            output_tokens=50,
        )
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("More"),
            sequence_index=3,
            input_tokens=200,
            output_tokens=80,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data[0]
        self.assertEqual(data["input_tokens"], 300)
        self.assertEqual(data["output_tokens"], 130)
        self.assertEqual(data["total_tokens"], 430)

    def test_token_totals_include_title_gen_tokens(self):
        """Title-generation tokens are added to the thread totals."""
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        thread = ThreadSession.objects.create(
            chat_session=self.session,
            title_gen_input_tokens=20,
            title_gen_output_tokens=5,
        )
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Reply"),
            sequence_index=1,
            input_tokens=100,
            output_tokens=50,
        )

        response = self.client.get(self.list_url)

        data = response.data[0]
        self.assertEqual(data["input_tokens"], 120)
        self.assertEqual(data["output_tokens"], 55)
        self.assertEqual(data["total_tokens"], 175)
        self.assertEqual(data["title_gen_input_tokens"], 20)
        self.assertEqual(data["title_gen_output_tokens"], 5)

    def test_token_totals_null_when_no_tokens_recorded(self):
        """When no messages have token data, totals are null."""
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        thread = ThreadSession.objects.create(chat_session=self.session)
        Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Hello"),
            sequence_index=1,
        )

        response = self.client.get(self.list_url)

        data = response.data[0]
        self.assertIsNone(data["input_tokens"])
        self.assertIsNone(data["output_tokens"])
        self.assertIsNone(data["total_tokens"])

    def test_replaced_messages_included_in_token_totals(self):
        """Both original and replacement messages contribute to token totals."""
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        thread = ThreadSession.objects.create(chat_session=self.session)
        original = Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Old"),
            sequence_index=1,
            input_tokens=500,
            output_tokens=200,
        )
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("New"),
            sequence_index=1,
            replaces=original,
            input_tokens=100,
            output_tokens=40,
        )

        response = self.client.get(self.list_url)

        data = response.data[0]
        self.assertEqual(data["input_tokens"], 600)
        self.assertEqual(data["output_tokens"], 240)
        self.assertEqual(data["total_tokens"], 840)

    def test_token_fields_visible_to_regular_users(self):
        """Regular users should see token fields in the response."""
        thread = ThreadSession.objects.create(
            chat_session=self.session,
            title_gen_input_tokens=10,
            title_gen_output_tokens=3,
        )
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Hi"),
            sequence_index=1,
            input_tokens=100,
            output_tokens=50,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data[0]
        self.assertEqual(data["input_tokens"], 110)  # 100 msg + 10 title_gen
        self.assertEqual(data["output_tokens"], 53)  # 50 msg + 3 title_gen
        self.assertEqual(data["total_tokens"], 163)
        self.assertEqual(data["title_gen_input_tokens"], 10)
        self.assertEqual(data["title_gen_output_tokens"], 3)

    def test_token_fields_visible_to_staff(self):
        """Staff users should see all token fields."""
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        thread = ThreadSession.objects.create(chat_session=self.session)
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Hi"),
            sequence_index=1,
            input_tokens=50,
            output_tokens=25,
        )

        response = self.client.get(self.list_url)

        data = response.data[0]
        self.assertIn("input_tokens", data)
        self.assertIn("output_tokens", data)
        self.assertIn("total_tokens", data)
        self.assertIn("title_gen_input_tokens", data)
        self.assertIn("title_gen_output_tokens", data)

    def test_filter_by_total_tokens_range(self):
        """Users can filter threads by total_tokens_min / total_tokens_max."""
        t_small = ThreadSession.objects.create(chat_session=self.session)
        Message.objects.create(
            thread=t_small,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Small"),
            sequence_index=1,
            input_tokens=10,
            output_tokens=5,
        )
        t_large = ThreadSession.objects.create(chat_session=self.session)
        Message.objects.create(
            thread=t_large,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Large"),
            sequence_index=1,
            input_tokens=500,
            output_tokens=300,
        )

        # Filter: total_tokens >= 100
        response = self.client.get(self.list_url, {"total_tokens_min": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(t_large.uuid), uuids)
        self.assertNotIn(str(t_small.uuid), uuids)

        # Filter: total_tokens <= 50
        response = self.client.get(self.list_url, {"total_tokens_max": 50})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["uuid"] for item in response.data}
        self.assertIn(str(t_small.uuid), uuids)
        self.assertNotIn(str(t_large.uuid), uuids)

    def test_order_by_total_tokens(self):
        """Users can order threads by total_tokens ascending and descending."""
        t1 = ThreadSession.objects.create(chat_session=self.session)
        Message.objects.create(
            thread=t1,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("A"),
            sequence_index=1,
            input_tokens=10,
            output_tokens=5,
        )
        t2 = ThreadSession.objects.create(chat_session=self.session)
        Message.objects.create(
            thread=t2,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("B"),
            sequence_index=1,
            input_tokens=500,
            output_tokens=300,
        )

        # Ascending
        response = self.client.get(self.list_url, {"o": "total_tokens"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["uuid"], str(t1.uuid))
        self.assertEqual(response.data[1]["uuid"], str(t2.uuid))

        # Descending
        response = self.client.get(self.list_url, {"o": "-total_tokens"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["uuid"], str(t2.uuid))
        self.assertEqual(response.data[1]["uuid"], str(t1.uuid))

    def test_regular_user_token_filters_applied(self):
        """Regular users can use token range filters."""
        thread = ThreadSession.objects.create(chat_session=self.session)
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Hi"),
            sequence_index=1,
            input_tokens=1000,
            output_tokens=500,
        )

        # Restrictive filter excludes the thread
        response = self.client.get(self.list_url, {"total_tokens_min": 999999})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@patch(SYNC_THREAD_PATCH, _SynchronousThread)
class ThreadTitleGenerationTest(test.APITestCase):
    """Tests for server-side thread title generation via _generate_thread_name."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)

    def _make_streamer(self, is_new_thread=False, **kwargs):
        defaults = dict(
            messages=[{"role": "user", "content": "How do I manage VMs?"}],
            url="https://example.com/stream",
            token="dummy-token",
            user=self.user,
            thread=self.thread,
            original_input="How do I manage VMs?",
            is_new_thread=is_new_thread,
        )
        defaults.update(kwargs)
        return LLMStreamer(**defaults)

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_new_thread_gets_llm_generated_title(self, mock_openai_cls):
        """New thread's name is updated with the title from a second AI Assistant call."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Here is how you manage VMs.")]),
            _fake_stream(
                [
                    _make_content_chunk("Managing Virtual Machines"),
                    _make_usage_chunk(20, 5),
                ]
            ),
        ]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)  # consume the stream

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "Managing Virtual Machines")

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_existing_thread_does_not_regenerate_title(self, mock_openai_cls):
        """Existing thread (is_new_thread=False) should not trigger title generation."""
        self.thread.name = "Existing Title"
        self.thread.save()

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _fake_stream(
            [_make_content_chunk("Response content")]
        )

        streamer = self._make_streamer(is_new_thread=False)
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "Existing Title")
        # Only one create() call (main stream), no title generation call
        mock_client.chat.completions.create.assert_called_once()

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_title_generation_failure_does_not_break_stream(self, mock_openai_cls):
        """If the title AI Assistant call fails, the main response still works and name stays default."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Main response content")]),
            Exception("timeout"),
        ]

        streamer = self._make_streamer(is_new_thread=True)
        chunks = list(streamer)

        # Main response content should still be streamed
        all_content = "".join(json.loads(c).get("c", "") for c in chunks if "c" in c)
        self.assertIn("Main response content", all_content)

        # Thread name should remain default
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "New chat")

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_long_title_is_truncated(self, mock_openai_cls):
        """Title longer than 150 chars is truncated in DB."""
        long_title = "A" * 200
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Main response")]),
            _fake_stream([_make_content_chunk(long_title)]),
        ]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(len(self.thread.name), 150)
        self.assertEqual(self.thread.name, "A" * 150)

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_empty_title_leaves_default_name(self, mock_openai_cls):
        """If the AI Assistant returns only whitespace/quotes, the default name is kept."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Main response")]),
            _fake_stream([_make_content_chunk('  "" ')]),
        ]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "New chat")

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_title_quotes_are_stripped(self, mock_openai_cls):
        """Surrounding quotes are stripped from the generated title."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Main response")]),
            _fake_stream([_make_content_chunk('"My Title"')]),
        ]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "My Title")

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_canned_response_new_thread_generates_title(self, mock_openai_cls):
        """Canned response path with is_new_thread=True still generates a title."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _fake_stream(
            [_make_content_chunk("Blocked Query Title")]
        )

        streamer = self._make_streamer(
            is_new_thread=True,
            canned_response="I cannot help with that.",
        )
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "Blocked Query Title")

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_title_tokens_counted_in_usage(self, mock_openai_cls):
        """Title generation tokens should be added to the streamer's token counts."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Response"), _make_usage_chunk(100, 50)]),
            _fake_stream(
                [_make_content_chunk("VM Management"), _make_usage_chunk(20, 5)]
            ),
        ]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)

        # Main (100+50) + title (20+5) = 175 total
        quota = TokenQuota.objects.get(user=self.user)
        self.assertEqual(quota.daily_usage, 175)

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_title_gen_tokens_persisted_on_thread(self, mock_openai_cls):
        """Title-generation token counts are stored on the ThreadSession model."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Response"), _make_usage_chunk(100, 50)]),
            _fake_stream(
                [_make_content_chunk("VM Management"), _make_usage_chunk(20, 5)]
            ),
        ]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.title_gen_input_tokens, 20)
        self.assertEqual(self.thread.title_gen_output_tokens, 5)

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_message_token_fields_persisted(self, mock_openai_cls):
        """Assistant message stores input_tokens and output_tokens after streaming."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _fake_stream(
            [_make_content_chunk("Hello!"), _make_usage_chunk(80, 30)]
        )

        streamer = self._make_streamer(is_new_thread=False)
        list(streamer)

        assistant_msg = Message.objects.filter(
            thread=self.thread, role=Message.Role.ASSISTANT
        ).first()
        self.assertIsNotNone(assistant_msg)
        self.assertEqual(assistant_msg.input_tokens, 80)
        self.assertEqual(assistant_msg.output_tokens, 30)

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_title_generation_reads_blocks_from_user_msg(self, mock_openai_cls):
        """When user_msg has blocks, title gen uses blocks[0]['content'], not original_input."""
        user_msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=[markdown_block("How do I list my VMs?")],
            sequence_index=1,
        )

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Main response")]),
            _fake_stream([_make_content_chunk("List VMs Title")]),
        ]

        streamer = self._make_streamer(
            is_new_thread=True,
            user_msg=user_msg,
            original_input="stale raw input",
        )
        list(streamer)

        # Explicit assert so a missing title call produces a clear failure,
        # not a cryptic IndexError on call_args_list[1].
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        title_prompt = mock_client.chat.completions.create.call_args_list[1].kwargs[
            "messages"
        ][0]["content"]
        self.assertIn("How do I list my VMs?", title_prompt)
        self.assertNotIn("stale raw input", title_prompt)

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_title_generation_falls_back_to_original_input_when_no_user_msg(
        self, mock_openai_cls
    ):
        """Without user_msg (canned-response path), title gen falls back to original_input."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Main response")]),
            _fake_stream([_make_content_chunk("Fallback Title")]),
        ]

        streamer = self._make_streamer(
            is_new_thread=True,
            original_input="Fallback text",
        )
        list(streamer)

        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        title_prompt = mock_client.chat.completions.create.call_args_list[1].kwargs[
            "messages"
        ][0]["content"]
        self.assertIn("Fallback text", title_prompt)

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_title_generation_falls_back_when_user_msg_has_empty_blocks(
        self, mock_openai_cls
    ):
        """When user_msg.blocks is empty, title gen falls back to original_input."""
        user_msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=[],
            sequence_index=1,
        )

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Main response")]),
            _fake_stream([_make_content_chunk("Empty Blocks Fallback Title")]),
        ]

        streamer = self._make_streamer(
            is_new_thread=True,
            user_msg=user_msg,
            original_input="Fallback when blocks empty",
        )
        list(streamer)

        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        title_prompt = mock_client.chat.completions.create.call_args_list[1].kwargs[
            "messages"
        ][0]["content"]
        self.assertIn("Fallback when blocks empty", title_prompt)

    @patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_title_generation_falls_back_when_first_block_has_empty_content(
        self, mock_openai_cls
    ):
        """When blocks[0] is a non-textual/empty-content block (e.g. a
        vm_order block), title gen falls back to original_input instead
        of silently skipping. Regression guard: earlier version would
        set source_text='' and return.
        """
        user_msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=[
                {
                    "id": "b0",
                    "key": "vm_order",
                    "status": "complete",
                    "order_status": "project_form",
                    "projects": [{"name": "proj-a"}],
                },
            ],
            sequence_index=1,
        )

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = [
            _fake_stream([_make_content_chunk("Main response")]),
            _fake_stream([_make_content_chunk("Non-text Block Fallback Title")]),
        ]

        streamer = self._make_streamer(
            is_new_thread=True,
            user_msg=user_msg,
            original_input="show my vms",
        )
        list(streamer)

        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        title_prompt = mock_client.chat.completions.create.call_args_list[1].kwargs[
            "messages"
        ][0]["content"]
        self.assertIn("show my vms", title_prompt)
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "Non-text Block Fallback Title")
