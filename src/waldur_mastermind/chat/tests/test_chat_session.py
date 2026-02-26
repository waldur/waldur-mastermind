import json
from unittest.mock import Mock, patch

import requests
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import (
    ChatSession,
    Message,
    ThreadSession,
    TokenQuota,
)
from waldur_mastermind.chat.views import LLMStreamer


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
            content="First",
            sequence_index=1,
        )

        # Creating another active message with same sequence_index should fail
        with self.assertRaises(Exception):  # IntegrityError
            Message.objects.create(
                thread=self.thread,
                role=Message.Role.USER,
                content="Duplicate",
                sequence_index=1,
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
            thread=thread, role=Message.Role.USER, content="Test", sequence_index=1
        )
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            content="Response",
            sequence_index=2,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["message_count"], 2)


class ThreadTitleGenerationTest(test.APITestCase):
    """Tests for server-side thread title generation via _generate_thread_name."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)

    def _fake_response(self, lines):
        resp = Mock()
        resp.iter_lines.return_value = lines
        resp.raise_for_status = Mock()
        return resp

    def _make_streamer(self, is_new_thread=False, **kwargs):
        defaults = dict(
            llm_prompt="test prompt",
            url="https://example.com/stream",
            token="dummy-token",
            user=self.user,
            thread=self.thread,
            original_input="How do I manage VMs?",
            is_new_thread=is_new_thread,
        )
        defaults.update(kwargs)
        return LLMStreamer(**defaults)

    @patch("waldur_mastermind.chat.views.requests.post")
    def test_new_thread_gets_llm_generated_title(self, mock_post):
        """New thread's name is updated with the title from a second LLM call."""
        # First call: main stream response
        main_resp = self._fake_response(
            ["data: " + json.dumps({"content": "Here is how you manage VMs."})]
        )
        # Second call: title generation response
        title_resp = self._fake_response(
            [
                "data: " + json.dumps({"content": "Managing Virtual Machines"}),
                "data: "
                + json.dumps(
                    {
                        "additional_kwargs": {
                            "usage_metadata": {
                                "input_tokens": 20,
                                "output_tokens": 5,
                            }
                        }
                    }
                ),
            ]
        )

        mock_post.return_value.__enter__.side_effect = [main_resp, title_resp]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)  # consume the stream

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "Managing Virtual Machines")

    @patch("waldur_mastermind.chat.views.requests.post")
    def test_existing_thread_does_not_regenerate_title(self, mock_post):
        """Existing thread (is_new_thread=False) should not trigger title generation."""
        self.thread.name = "Existing Title"
        self.thread.save()

        main_resp = self._fake_response(
            ["data: " + json.dumps({"content": "Response content"})]
        )
        mock_post.return_value.__enter__.return_value = main_resp

        streamer = self._make_streamer(is_new_thread=False)
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "Existing Title")
        # Only one call (main stream), no title generation call
        mock_post.assert_called_once()

    @patch("waldur_mastermind.chat.views.requests.post")
    def test_title_generation_failure_does_not_break_stream(self, mock_post):
        """If the title LLM call fails, the main response still works and name stays default."""
        main_resp = self._fake_response(
            ["data: " + json.dumps({"content": "Main response content"})]
        )

        # Title generation call raises an exception
        mock_post.return_value.__enter__.side_effect = [
            main_resp,
            requests.ConnectionError("timeout"),
        ]

        streamer = self._make_streamer(is_new_thread=True)
        chunks = list(streamer)

        # Main response content should still be streamed
        all_content = "".join(json.loads(c).get("c", "") for c in chunks if "c" in c)
        self.assertIn("Main response content", all_content)

        # Thread name should remain default
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "New chat")

    @patch("waldur_mastermind.chat.views.requests.post")
    def test_long_title_is_truncated(self, mock_post):
        """Title longer than 150 chars is truncated in DB."""
        long_title = "A" * 200
        main_resp = self._fake_response(
            ["data: " + json.dumps({"content": "Main response"})]
        )
        title_resp = self._fake_response(
            ["data: " + json.dumps({"content": long_title})]
        )
        mock_post.return_value.__enter__.side_effect = [main_resp, title_resp]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(len(self.thread.name), 150)
        self.assertEqual(self.thread.name, "A" * 150)

    @patch("waldur_mastermind.chat.views.requests.post")
    def test_empty_title_leaves_default_name(self, mock_post):
        """If the LLM returns only whitespace/quotes, the default name is kept."""
        main_resp = self._fake_response(
            ["data: " + json.dumps({"content": "Main response"})]
        )
        title_resp = self._fake_response(["data: " + json.dumps({"content": '  "" '})])
        mock_post.return_value.__enter__.side_effect = [main_resp, title_resp]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "New chat")

    @patch("waldur_mastermind.chat.views.requests.post")
    def test_title_quotes_are_stripped(self, mock_post):
        """Surrounding quotes are stripped from the generated title."""
        main_resp = self._fake_response(
            ["data: " + json.dumps({"content": "Main response"})]
        )
        title_resp = self._fake_response(
            ["data: " + json.dumps({"content": '"My Title"'})]
        )
        mock_post.return_value.__enter__.side_effect = [main_resp, title_resp]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "My Title")

    @patch("waldur_mastermind.chat.views.requests.post")
    def test_canned_response_new_thread_generates_title(self, mock_post):
        """Canned response path with is_new_thread=True still generates a title."""
        title_resp = self._fake_response(
            ["data: " + json.dumps({"content": "Blocked Query Title"})]
        )
        mock_post.return_value.__enter__.return_value = title_resp

        streamer = self._make_streamer(
            is_new_thread=True,
            canned_response="I cannot help with that.",
        )
        list(streamer)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.name, "Blocked Query Title")

    @patch("waldur_mastermind.chat.views.requests.post")
    def test_title_tokens_counted_in_usage(self, mock_post):
        """Title generation tokens should be added to the streamer's token counts."""
        main_resp = self._fake_response(
            [
                "data: " + json.dumps({"content": "Response"}),
                "data: "
                + json.dumps(
                    {
                        "additional_kwargs": {
                            "usage_metadata": {
                                "input_tokens": 100,
                                "output_tokens": 50,
                            }
                        }
                    }
                ),
            ]
        )
        title_resp = self._fake_response(
            [
                "data: " + json.dumps({"content": "VM Management"}),
                "data: "
                + json.dumps(
                    {
                        "additional_kwargs": {
                            "usage_metadata": {
                                "input_tokens": 20,
                                "output_tokens": 5,
                            }
                        }
                    }
                ),
            ]
        )

        mock_post.return_value.__enter__.side_effect = [main_resp, title_resp]

        streamer = self._make_streamer(is_new_thread=True)
        list(streamer)

        # Main (100+50) + title (20+5) = 175 total
        quota = TokenQuota.objects.get(user=self.user)
        self.assertEqual(quota.daily_usage, 175)
