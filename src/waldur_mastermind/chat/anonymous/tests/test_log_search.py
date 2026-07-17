"""Staff log search over anonymous marketplace-chat transcripts.

Anon twin of chat/tests/test_log_search.py. Unlike Message (one row = one
side of a turn), an AnonymousChatInteraction row holds both the user's
question (`user_input`) and the assistant's reply (`assistant_blocks`), so
search_text combines both.
"""

from django.contrib.postgres.search import SearchQuery
from django.test import TestCase
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.anonymous import models as anonymous_models
from waldur_mastermind.chat.tests.utils import blocks_from_text


def _make_interaction(**overrides):
    defaults = dict(
        user_input="hi",
        ip_address="1.2.3.4",
        session_id="session-abc",
        user_slug="abc123",
        offering_uuids=[],
        is_flagged=False,
    )
    defaults.update(overrides)
    return anonymous_models.AnonymousChatInteraction.objects.create(**defaults)


class AnonSearchTextTest(TestCase):
    def test_search_text_combines_user_input_and_assistant_reply(self):
        interaction = _make_interaction(
            user_input="How do I resize an OpenStack volume?",
            assistant_blocks=blocks_from_text("Use the volume extend action."),
        )
        interaction.refresh_from_db()
        self.assertIn("resize an OpenStack volume", interaction.search_text)
        self.assertIn("volume extend action", interaction.search_text)

    def test_search_text_refreshed_when_assistant_blocks_updated(self):
        # Mirrors production: create() persists user_input, then a second
        # save(update_fields=["assistant_blocks"]) finalises the reply.
        interaction = _make_interaction(user_input="my question about SLURM")
        interaction.assistant_blocks = blocks_from_text("here is the SLURM answer")
        interaction.save(update_fields=["assistant_blocks"])
        interaction.refresh_from_db()
        self.assertIn("my question about SLURM", interaction.search_text)
        self.assertIn("here is the SLURM answer", interaction.search_text)

    def test_search_text_user_input_only_when_no_assistant_reply(self):
        interaction = _make_interaction(
            user_input="standalone question", assistant_blocks=[]
        )
        interaction.refresh_from_db()
        self.assertIn("standalone question", interaction.search_text)


class AnonSearchVectorTest(TestCase):
    def setUp(self):
        _make_interaction(
            user_input="How do I resize an OpenStack volume?",
            assistant_blocks=blocks_from_text("extend the block storage"),
        )

    def _search(self, term):
        return anonymous_models.AnonymousChatInteraction.objects.filter(
            search_vector=SearchQuery(term, search_type="websearch", config="english")
        )

    def test_vector_matches_stemmed_user_input(self):
        self.assertTrue(self._search("resizing").exists())

    def test_vector_matches_assistant_content(self):
        self.assertTrue(self._search("storage").exists())

    def test_vector_matches_quoted_phrase(self):
        self.assertTrue(self._search('"OpenStack volume"').exists())

    def test_vector_ignores_non_matching(self):
        self.assertFalse(self._search("kubernetes").exists())


class AnonQuerySearchTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("anonymous-chat-interaction-list")
        self.content = _make_interaction(
            user_slug="slug-content",
            session_id="sess-content",
            user_input="My OpenStack volume will not resize",
            assistant_blocks=blocks_from_text("try the extend action"),
        )
        self.other = _make_interaction(
            user_slug="slug-billing",
            session_id="sess-billing",
            user_input="hello there",
            assistant_blocks=blocks_from_text("general greeting"),
        )
        self.staff = structure_factories.UserFactory(is_staff=True)

    def _rows(self, response):
        data = response.data
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def _uuids_for(self, query):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.url, {"query": query})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {str(row["uuid"]) for row in self._rows(response)}

    def test_query_matches_user_input_content(self):
        uuids = self._uuids_for("resize")
        self.assertIn(str(self.content.uuid), uuids)
        self.assertNotIn(str(self.other.uuid), uuids)

    def test_query_matches_assistant_content(self):
        uuids = self._uuids_for("extend")
        self.assertIn(str(self.content.uuid), uuids)
        self.assertNotIn(str(self.other.uuid), uuids)

    def test_query_matches_quoted_phrase(self):
        self.assertIn(str(self.content.uuid), self._uuids_for('"OpenStack volume"'))

    def test_query_matches_user_slug(self):
        self.assertIn(str(self.content.uuid), self._uuids_for("slug-content"))

    def test_query_ignores_non_matching(self):
        self.assertNotIn(str(self.content.uuid), self._uuids_for("kubernetes"))
