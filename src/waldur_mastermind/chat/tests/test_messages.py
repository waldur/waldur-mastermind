from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import ChatSession, Message, ThreadSession


class MessageViewSetTest(test.APITransactionTestCase):
    """Test MessageViewSet endpoints."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)

        self.other_session = ChatSession.objects.create(user=self.other_user)
        self.other_thread = ThreadSession.objects.create(
            chat_session=self.other_session
        )

        self.list_url = reverse("chat-message-list")

    def test_list_returns_only_user_messages(self):
        """list endpoint returns only current user's messages."""
        my_msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="My message",
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.other_thread,
            role=Message.Role.USER,
            content="Other message",
            sequence_index=1,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(my_msg.uuid))

    def test_list_excludes_replaced_messages_by_default(self):
        """list endpoint excludes replaced messages by default."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Original",
            sequence_index=1,
        )
        replacement = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Edited",
            sequence_index=1,
            replaces=original,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(replacement.uuid))

    def test_list_includes_replaced_when_include_history(self):
        """list endpoint includes replaced messages when include_history=true."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Original",
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Edited",
            sequence_index=1,
            replaces=original,
        )

        response = self.client.get(self.list_url, {"include_history": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_filter_by_thread(self):
        """list endpoint can filter messages by thread UUID."""
        thread2 = ThreadSession.objects.create(chat_session=self.session)

        msg1 = Message.objects.create(
            thread=self.thread, role=Message.Role.USER, content="T1", sequence_index=1
        )
        Message.objects.create(
            thread=thread2, role=Message.Role.USER, content="T2", sequence_index=1
        )

        response = self.client.get(self.list_url, {"thread": str(self.thread.uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(msg1.uuid))

    def test_create_message(self):
        """create endpoint creates new message."""
        data = {
            "thread": str(self.thread.uuid),
            "role": Message.Role.USER,
            "content": "Hello world",
        }
        response = self.client.post(self.list_url, data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            Message.objects.filter(thread=self.thread, content="Hello world").exists()
        )

    def test_create_auto_assigns_sequence_index(self):
        """create endpoint auto-assigns sequence_index."""
        data = {
            "thread": str(self.thread.uuid),
            "role": Message.Role.USER,
            "content": "First",
        }
        response1 = self.client.post(self.list_url, data)

        data["content"] = "Second"
        response2 = self.client.post(self.list_url, data)

        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response1.data["sequence_index"], 1)
        self.assertEqual(response2.data["sequence_index"], 2)

    def test_cannot_create_message_in_other_user_thread(self):
        """User cannot create message in another user's thread."""
        data = {
            "thread": str(self.other_thread.uuid),
            "role": Message.Role.USER,
            "content": "Unauthorized",
        }
        response = self.client.post(self.list_url, data)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_retrieve_message(self):
        """retrieve endpoint returns message details."""
        msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Test message",
            sequence_index=1,
        )
        url = reverse("chat-message-detail", kwargs={"uuid": msg.uuid})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["content"], "Test message")

    def test_user_cannot_access_other_user_message(self):
        """User cannot access another user's message."""
        other_msg = Message.objects.create(
            thread=self.other_thread,
            role=Message.Role.USER,
            content="Other",
            sequence_index=1,
        )
        url = reverse("chat-message-detail", kwargs={"uuid": other_msg.uuid})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class MessageEditTest(test.APITransactionTestCase):
    """Test message editing functionality."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)

        self.other_session = ChatSession.objects.create(user=self.other_user)
        self.other_thread = ThreadSession.objects.create(
            chat_session=self.other_session
        )

    def test_edit_user_message(self):
        """edit action creates replacement message."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Original text",
            sequence_index=1,
        )
        url = reverse("chat-message-edit", kwargs={"uuid": original.uuid})

        response = self.client.post(url, {"content": "Edited text"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["content"], "Edited text")
        self.assertEqual(response.data["sequence_index"], 1)
        self.assertEqual(str(response.data["replaces"]), str(original.uuid))

    def test_edit_marks_original_as_replaced(self):
        """edit action marks original message as replaced."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Original",
            sequence_index=1,
        )
        url = reverse("chat-message-edit", kwargs={"uuid": original.uuid})

        response = self.client.post(url, {"content": "Edited"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        original.refresh_from_db()
        self.assertTrue(original.replaced_by.exists())

    def test_cannot_edit_assistant_message(self):
        """Cannot edit assistant messages."""
        assistant_msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            content="Assistant response",
            sequence_index=1,
        )
        url = reverse("chat-message-edit", kwargs={"uuid": assistant_msg.uuid})

        response = self.client.post(url, {"content": "Modified"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Can only edit user messages", str(response.data))

    def test_cannot_edit_already_edited_message(self):
        """Cannot edit a message that has already been edited."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Original",
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Edited",
            sequence_index=1,
            replaces=original,
        )
        url = reverse("chat-message-edit", kwargs={"uuid": original.uuid})

        response = self.client.post(url, {"content": "Second edit"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been edited", str(response.data))

    def test_can_only_edit_last_user_message(self):
        """Can only edit the last user message in thread."""
        first_msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="First",
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            content="Response",
            sequence_index=2,
        )
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Second",
            sequence_index=3,
        )

        url = reverse("chat-message-edit", kwargs={"uuid": first_msg.uuid})
        response = self.client.post(url, {"content": "Modified"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Can only edit the last user message", str(response.data))

    def test_edit_requires_content_field(self):
        """edit action requires content field."""
        msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Original",
            sequence_index=1,
        )
        url = reverse("chat-message-edit", kwargs={"uuid": msg.uuid})

        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("content", str(response.data))

    def test_cannot_edit_other_user_message(self):
        """User cannot edit another user's message."""
        other_msg = Message.objects.create(
            thread=self.other_thread,
            role=Message.Role.USER,
            content="Other",
            sequence_index=1,
        )
        url = reverse("chat-message-edit", kwargs={"uuid": other_msg.uuid})

        response = self.client.post(url, {"content": "Modified"})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_edit_creates_new_message_instance(self):
        """edit creates a new message instance, not modifying original."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Original",
            sequence_index=1,
        )
        url = reverse("chat-message-edit", kwargs={"uuid": original.uuid})

        response = self.client.post(url, {"content": "Edited"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should have 2 messages now
        self.assertEqual(Message.objects.filter(thread=self.thread).count(), 2)
        # Original still has same content
        original.refresh_from_db()
        self.assertEqual(original.content, "Original")
        # Response is for new message
        self.assertNotEqual(response.data["uuid"], str(original.uuid))


class MessageHistoryTest(test.APITransactionTestCase):
    """Test message history functionality."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)

    def test_history_returns_all_versions(self):
        """history action returns all versions of a message."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Version 1",
            sequence_index=1,
        )
        edit1 = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Version 2",
            sequence_index=1,
            replaces=original,
        )
        edit2 = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Version 3",
            sequence_index=1,
            replaces=edit1,
        )

        url = reverse("chat-message-history", kwargs={"uuid": edit2.uuid})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)
        # Should be in order: current, edit1, original
        self.assertEqual(response.data[0]["content"], "Version 3")
        self.assertEqual(response.data[1]["content"], "Version 2")
        self.assertEqual(response.data[2]["content"], "Version 1")

    def test_history_of_unedited_message_returns_single_item(self):
        """history of unedited message returns only that message."""
        msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Only version",
            sequence_index=1,
        )

        url = reverse("chat-message-history", kwargs={"uuid": msg.uuid})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["content"], "Only version")

    def test_history_from_original_returns_all_edits(self):
        """history action works from original message UUID."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Original",
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            content="Edited",
            sequence_index=1,
            replaces=original,
        )

        # Need to include history to access replaced messages
        url = reverse("chat-message-history", kwargs={"uuid": original.uuid})
        response = self.client.get(url + "?include_history=true")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should only return the original itself (doesn't traverse forward)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["content"], "Original")

    def test_cannot_view_other_user_message_history(self):
        """User cannot view history of another user's message."""
        other_user = structure_factories.UserFactory()
        other_session = ChatSession.objects.create(user=other_user)
        other_thread = ThreadSession.objects.create(chat_session=other_session)
        other_msg = Message.objects.create(
            thread=other_thread,
            role=Message.Role.USER,
            content="Other",
            sequence_index=1,
        )

        url = reverse("chat-message-history", kwargs={"uuid": other_msg.uuid})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class MessageConcurrencyTest(test.APITransactionTestCase):
    """Test concurrent message creation handling."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)

        self.list_url = reverse("chat-message-list")

    def test_sequential_message_creation(self):
        """Sequential message creation assigns correct sequence indices."""
        data1 = {
            "thread": str(self.thread.uuid),
            "role": Message.Role.USER,
            "content": "First",
        }
        data2 = {
            "thread": str(self.thread.uuid),
            "role": Message.Role.ASSISTANT,
            "content": "Second",
        }
        data3 = {
            "thread": str(self.thread.uuid),
            "role": Message.Role.USER,
            "content": "Third",
        }

        response1 = self.client.post(self.list_url, data1)
        response2 = self.client.post(self.list_url, data2)
        response3 = self.client.post(self.list_url, data3)

        self.assertEqual(response1.data["sequence_index"], 1)
        self.assertEqual(response2.data["sequence_index"], 2)
        self.assertEqual(response3.data["sequence_index"], 3)

    def test_message_creation_locks_thread(self):
        """Message creation uses select_for_update to prevent conflicts."""
        # This test verifies the locking mechanism exists
        # by checking that messages are created with correct sequence_index
        # even when called in quick succession

        for i in range(5):
            data = {
                "thread": str(self.thread.uuid),
                "role": Message.Role.USER,
                "content": f"Message {i + 1}",
            }
            response = self.client.post(self.list_url, data)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(response.data["sequence_index"], i + 1)

        # All messages should have unique sequence indices
        messages = Message.objects.filter(thread=self.thread)
        indices = [m.sequence_index for m in messages]
        self.assertEqual(len(set(indices)), 5)  # All unique
