from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import ChatSession, Message, ThreadSession


class MessageViewSetTest(test.APITestCase):
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

    def test_include_history_false_excludes_replaced(self):
        """include_history=false correctly excludes replaced messages (not truthy string)."""
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

        response = self.client.get(self.list_url, {"include_history": "false"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["content"], "Edited")

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


class MessageEditTest(test.APITestCase):
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
        """Cannot edit a message that has already been edited (it's no longer the last)."""
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
        self.assertIn("Can only edit the last user message", str(response.data))

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
