from datetime import timedelta

from constance.test import override_config as override_constance_config
from django.utils import timezone
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat import tasks
from waldur_mastermind.chat.models import ChatSession, Message, ThreadSession
from waldur_mastermind.chat.tests.utils import blocks_from_text


class ChatSessionCleanupTest(test.APITestCase):
    def setUp(self):
        self.user1 = structure_factories.UserFactory()
        self.user2 = structure_factories.UserFactory()

        now = timezone.now()

        # Create a recent session
        self.recent_session = ChatSession.objects.create(user=self.user1)
        ChatSession.objects.filter(pk=self.recent_session.pk).update(
            modified=now - timedelta(days=10)
        )

        # Create an old session with threads and messages
        self.old_session = ChatSession.objects.create(user=self.user2)
        old_thread = ThreadSession.objects.create(
            chat_session=self.old_session, name="Old thread"
        )
        Message.objects.create(
            thread=old_thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Old message"),
            sequence_index=1,
        )
        ChatSession.objects.filter(pk=self.old_session.pk).update(
            modified=now - timedelta(days=100)
        )

    @override_constance_config(AI_ASSISTANT_SESSION_RETENTION_DAYS=90)
    def test_cleanup_deletes_old_sessions(self):
        result = tasks.cleanup_old_chat_sessions()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["deleted_count"], 1)
        self.assertTrue(ChatSession.objects.filter(pk=self.recent_session.pk).exists())
        self.assertFalse(ChatSession.objects.filter(pk=self.old_session.pk).exists())

    @override_constance_config(AI_ASSISTANT_SESSION_RETENTION_DAYS=-1)
    def test_cleanup_disabled(self):
        result = tasks.cleanup_old_chat_sessions()

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(ChatSession.objects.count(), 2)

    @override_constance_config(AI_ASSISTANT_SESSION_RETENTION_DAYS=90)
    def test_cleanup_cascades_deletion(self):
        tasks.cleanup_old_chat_sessions()

        self.assertEqual(
            ThreadSession.objects.filter(chat_session=self.old_session).count(), 0
        )
        self.assertEqual(
            Message.objects.filter(thread__chat_session=self.old_session).count(), 0
        )

    @override_constance_config(AI_ASSISTANT_SESSION_RETENTION_DAYS=90)
    def test_cleanup_nothing_to_delete(self):
        self.old_session.delete()

        result = tasks.cleanup_old_chat_sessions()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["deleted_count"], 0)
