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
