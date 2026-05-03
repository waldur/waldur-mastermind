from datetime import timedelta

from constance.test import override_config as override_constance_config
from django.utils import timezone
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat import tasks
from waldur_mastermind.chat.anonymous import models as anonymous_models
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


class AnonymousChatArtifactCleanupTest(test.APITestCase):
    """Periodic cleanup of pseudonymous artifacts created by the public
    anonymous chat endpoint.

    ``SessionBinding`` and ``AnonymousChatBudget`` rows are created on
    every distinct session-id / IP that hits the public stream endpoint.
    Without auth or captcha gating, an attacker can fan random session-ids
    across the endpoint to grow these tables without bound. The cleanup
    task purges idle rows past a configurable retention window so the
    tables stay bounded.
    """

    def setUp(self):
        self.anon_models = anonymous_models
        now = timezone.now()
        self.now = now

        # SessionBinding: one fresh, one stale.
        self.fresh_binding = anonymous_models.SessionBinding.objects.create(
            session_id="fresh-session", ip_address="1.1.1.1"
        )
        anonymous_models.SessionBinding.objects.filter(pk=self.fresh_binding.pk).update(
            last_seen=now - timedelta(days=2)
        )

        self.stale_binding = anonymous_models.SessionBinding.objects.create(
            session_id="stale-session", ip_address="2.2.2.2"
        )
        anonymous_models.SessionBinding.objects.filter(pk=self.stale_binding.pk).update(
            last_seen=now - timedelta(days=120)
        )

        # AnonymousChatBudget: stale & idle (eligible) vs stale & blocked
        # (must be kept while the block window is active) vs fresh.
        self.fresh_budget = anonymous_models.AnonymousChatBudget.objects.create(
            ip_address="3.3.3.3",
        )
        anonymous_models.AnonymousChatBudget.objects.filter(
            pk=self.fresh_budget.pk
        ).update(modified=now - timedelta(days=1))

        self.stale_idle_budget = anonymous_models.AnonymousChatBudget.objects.create(
            ip_address="4.4.4.4",
        )
        anonymous_models.AnonymousChatBudget.objects.filter(
            pk=self.stale_idle_budget.pk
        ).update(modified=now - timedelta(days=120))

        self.stale_blocked_budget = anonymous_models.AnonymousChatBudget.objects.create(
            ip_address="5.5.5.5",
            is_blocked_until=now + timedelta(hours=2),
        )
        anonymous_models.AnonymousChatBudget.objects.filter(
            pk=self.stale_blocked_budget.pk
        ).update(modified=now - timedelta(days=120))

    @override_constance_config(ANONYMOUS_CHAT_ARTIFACT_RETENTION_DAYS=90)
    def test_cleanup_deletes_stale_session_bindings_only(self):
        result = tasks.cleanup_anonymous_chat_artifacts()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["deleted_session_bindings"], 1)
        self.assertTrue(
            self.anon_models.SessionBinding.objects.filter(
                pk=self.fresh_binding.pk
            ).exists()
        )
        self.assertFalse(
            self.anon_models.SessionBinding.objects.filter(
                pk=self.stale_binding.pk
            ).exists()
        )

    @override_constance_config(ANONYMOUS_CHAT_ARTIFACT_RETENTION_DAYS=90)
    def test_cleanup_keeps_blocked_budgets_even_if_stale(self):
        """Stale rows whose ``is_blocked_until`` is still in the future
        must NOT be deleted — otherwise an attacker could simply wait out
        the cleanup window to clear their block.
        """
        result = tasks.cleanup_anonymous_chat_artifacts()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["deleted_budgets"], 1)

        self.assertTrue(
            self.anon_models.AnonymousChatBudget.objects.filter(
                pk=self.fresh_budget.pk
            ).exists()
        )
        self.assertFalse(
            self.anon_models.AnonymousChatBudget.objects.filter(
                pk=self.stale_idle_budget.pk
            ).exists()
        )
        self.assertTrue(
            self.anon_models.AnonymousChatBudget.objects.filter(
                pk=self.stale_blocked_budget.pk
            ).exists(),
            "Active blocks must survive cleanup",
        )

    @override_constance_config(ANONYMOUS_CHAT_ARTIFACT_RETENTION_DAYS=-1)
    def test_cleanup_disabled_when_retention_negative(self):
        result = tasks.cleanup_anonymous_chat_artifacts()

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["deleted_session_bindings"], 0)
        self.assertEqual(result["deleted_budgets"], 0)
        self.assertEqual(self.anon_models.SessionBinding.objects.count(), 2)
        self.assertEqual(self.anon_models.AnonymousChatBudget.objects.count(), 3)
