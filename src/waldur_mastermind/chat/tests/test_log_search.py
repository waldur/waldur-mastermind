"""Staff log search: message-content full-text search over chat threads."""

from constance.test.unittest import override_config as override_constance_config
from django.contrib.postgres.search import SearchQuery
from django.test import TestCase
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat import models
from waldur_mastermind.chat.tests.utils import blocks_from_text, markdown_block

CONSTANCE = dict(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)


class MessageSearchTextTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.session = models.ChatSession.objects.create(user=self.user)
        self.thread = models.ThreadSession.objects.create(chat_session=self.session)

    def _msg(self, text, seq=1, role="user"):
        return models.Message.objects.create(
            thread=self.thread,
            role=role,
            blocks=blocks_from_text(text),
            sequence_index=seq,
        )

    def test_search_text_populated_on_create(self):
        msg = self._msg("How do I fix a SLURM quota error?")
        msg.refresh_from_db()
        self.assertIn("SLURM quota error", msg.search_text)

    def test_search_text_refreshed_when_blocks_updated(self):
        msg = self._msg("original text")
        msg.blocks = [markdown_block("replacement about kubernetes ingress")]
        msg.save(update_fields=["blocks"])
        msg.refresh_from_db()
        self.assertIn("kubernetes ingress", msg.search_text)
        self.assertNotIn("original", msg.search_text)

    def test_search_text_empty_for_blockless_message(self):
        msg = self._msg("")  # blocks_from_text("") -> []
        msg.refresh_from_db()
        self.assertEqual(msg.search_text, "")


class MessageSearchVectorTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.session = models.ChatSession.objects.create(user=self.user)
        self.thread = models.ThreadSession.objects.create(chat_session=self.session)
        models.Message.objects.create(
            thread=self.thread,
            role="user",
            blocks=blocks_from_text("How do I resize an OpenStack volume?"),
            sequence_index=1,
        )

    def _search(self, term):
        return models.Message.objects.filter(
            search_vector=SearchQuery(term, search_type="websearch", config="english")
        )

    def test_vector_matches_stemmed_word(self):
        # english config stems "resize"/"resizing" to a common lexeme.
        self.assertTrue(self._search("resizing").exists())

    def test_vector_matches_quoted_phrase(self):
        self.assertTrue(self._search('"OpenStack volume"').exists())

    def test_vector_ignores_non_matching(self):
        self.assertFalse(self._search("kubernetes").exists())


class ThreadQuerySearchTest(test.APITestCase):
    def setUp(self):
        self.owner = structure_factories.UserFactory(username="alice")
        self.session = models.ChatSession.objects.create(user=self.owner)
        self.url = reverse("chat-thread-list")

        self.thread_content = models.ThreadSession.objects.create(
            chat_session=self.session, name="Untitled"
        )
        models.Message.objects.create(
            thread=self.thread_content,
            role="user",
            blocks=blocks_from_text("My OpenStack volume will not resize"),
            sequence_index=1,
        )
        self.thread_named = models.ThreadSession.objects.create(
            chat_session=self.session, name="Billing question about credits"
        )
        models.Message.objects.create(
            thread=self.thread_named,
            role="user",
            blocks=blocks_from_text("hello there"),
            sequence_index=1,
        )

    def _uuids_for(self, query, user=None):
        self.client.force_authenticate(
            user=user or structure_factories.UserFactory(is_staff=True)
        )
        response = self.client.get(self.url, {"query": query})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {str(t["uuid"]) for t in response.data}

    @override_constance_config(**CONSTANCE)
    def test_query_matches_message_content(self):
        uuids = self._uuids_for("resize")
        self.assertIn(str(self.thread_content.uuid), uuids)
        self.assertNotIn(str(self.thread_named.uuid), uuids)

    @override_constance_config(**CONSTANCE)
    def test_query_matches_quoted_phrase(self):
        self.assertIn(
            str(self.thread_content.uuid), self._uuids_for('"OpenStack volume"')
        )

    @override_constance_config(**CONSTANCE)
    def test_query_still_matches_thread_name(self):
        # Regression: existing thread-name matching must keep working.
        self.assertIn(str(self.thread_named.uuid), self._uuids_for("Billing"))

    @override_constance_config(**CONSTANCE)
    def test_query_still_matches_username(self):
        self.assertIn(str(self.thread_content.uuid), self._uuids_for("alice"))

    @override_constance_config(**CONSTANCE)
    def test_non_staff_cannot_see_others_threads_via_search(self):
        # Regression: content search must not widen visibility for regular users.
        other = structure_factories.UserFactory(username="mallory")
        self.assertNotIn(
            str(self.thread_content.uuid), self._uuids_for("resize", user=other)
        )
