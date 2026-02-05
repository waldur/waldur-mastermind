from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import ChatSession, Message, ThreadSession


class MessageModelTest(test.APITransactionTestCase):
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


class ChatSessionViewSetTest(test.APITransactionTestCase):
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


class ThreadSessionViewSetTest(test.APITransactionTestCase):
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

    def test_list_excludes_archived_by_default(self):
        """list endpoint excludes archived threads by default."""
        active_thread = ThreadSession.objects.create(
            chat_session=self.session, name="Active", is_archived=False
        )
        ThreadSession.objects.create(
            chat_session=self.session, name="Archived", is_archived=True
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(active_thread.uuid))

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

    def test_create_thread(self):
        """create endpoint creates new thread for current user."""
        data = {"name": "New conversation"}
        response = self.client.post(self.list_url, data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            ThreadSession.objects.filter(
                chat_session__user=self.user, name="New conversation"
            ).exists()
        )

    def test_create_thread_auto_creates_session(self):
        """create endpoint auto-creates ChatSession if user doesn't have one."""
        new_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=new_user)

        self.assertFalse(ChatSession.objects.filter(user=new_user).exists())

        data = {"name": "First thread"}
        response = self.client.post(self.list_url, data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ChatSession.objects.filter(user=new_user).exists())

    def test_retrieve_thread(self):
        """retrieve endpoint returns thread details."""
        thread = ThreadSession.objects.create(
            chat_session=self.session, name="Test thread"
        )
        url = reverse("chat-thread-detail", kwargs={"uuid": thread.uuid})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Test thread")

    def test_user_cannot_access_other_user_thread(self):
        """User cannot access another user's thread."""
        other_thread = ThreadSession.objects.create(
            chat_session=self.other_session, name="Other thread"
        )
        url = reverse("chat-thread-detail", kwargs={"uuid": other_thread.uuid})

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_update_thread_name(self):
        """update endpoint updates thread name."""
        thread = ThreadSession.objects.create(
            chat_session=self.session, name="Original"
        )
        url = reverse("chat-thread-detail", kwargs={"uuid": thread.uuid})

        data = {"name": "Updated"}
        response = self.client.patch(url, data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        thread.refresh_from_db()
        self.assertEqual(thread.name, "Updated")

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

    def test_message_count_in_response(self):
        """Response includes message_count."""
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

        url = reverse("chat-thread-detail", kwargs={"uuid": thread.uuid})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["message_count"], 2)
