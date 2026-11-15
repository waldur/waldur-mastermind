import gzip
import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Feature
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.chat import deployment_profile
from waldur_mastermind.chat.deployment_profile import TermState
from waldur_mastermind.chat.docs import corpus, search
from waldur_mastermind.chat.docs.models import DocSection
from waldur_mastermind.chat.tools.docs.search_docs import SearchDocsTool
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.executor import ToolExecutor
from waldur_mastermind.chat.tools.tool_sets import get_tool_set_for_user
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

PAT = {
    "path": "user-guide/end-users/personal-access-tokens.md",
    "anchor": "",
    "title": "Personal access tokens",
    "breadcrumb": "User guide > End users",
    "text": "Create a personal access token under your profile to call the API.",
    "audience": "all",
    "requires": ["setting:PAT_ENABLED"],
}
ORDERING = {
    "path": "user-guide/end-users/ordering.md",
    "anchor": "pricing",
    "title": "Ordering and pricing",
    "breadcrumb": "User guide > End users",
    "text": "Order an offering from the marketplace and review its pricing plan.",
    "audience": "all",
    "requires": ["!feature:marketplace.catalogue_only"],
}
PROJECTS = {
    "path": "user-guide/end-users/projects.md",
    "anchor": "create-a-project",
    "title": "Create a project",
    "breadcrumb": "User guide > End users",
    "text": "Open your organization and add a project with a name.",
    "audience": "all",
    "requires": [],
}
STAFF_USERS = {
    "path": "user-guide/staff-users/projects.md",
    "anchor": "",
    "title": "Managing projects of every organization",
    "breadcrumb": "User guide > Staff users",
    "text": "Support can browse each project across organizations.",
    "audience": "staff_support",
    "requires": [],
}
ADMIN = {
    "path": "admin-guide/projects.md",
    "anchor": "",
    "title": "Project settings for administrators",
    "breadcrumb": "Admin guide",
    "text": "Administrators configure project defaults in the settings.",
    "audience": "staff",
    "requires": [],
}
FUTURE = {
    "path": "user-guide/end-users/project-teleport.md",
    "anchor": "",
    "title": "Teleport a project",
    "breadcrumb": "User guide > End users",
    "text": "Teleport a project to another organization.",
    "audience": "all",
    "requires": ["setting:PROJECT_TELEPORT_ENABLED"],
}

CORPUS = [PAT, ORDERING, PROJECTS, STAFF_USERS, ADMIN, FUTURE]


def write_corpus(directory, sections) -> Path:
    path = Path(directory) / "docs-corpus.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for section in sections:
            stream.write(json.dumps(section) + "\n")
    return path


class DocsTestMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        corpus.load_sections(
            [corpus._parse_line(json.dumps(s), i) for i, s in enumerate(CORPUS)]
        )
        self.fixture = structure_fixtures.ProjectFixture()
        self.tool = SearchDocsTool()

    def titles(self, user, query):
        result = self.tool.execute(user, {"query": query})
        self.assertEqual(result["type"], "success")
        return {s["title"]: s for s in result["data"]["sections"]}


@override_config(PAT_ENABLED=False)
class SiteConfigurationTest(DocsTestMixin, TestCase):
    def test_end_user_gets_no_section_of_a_disabled_setting(self):
        self.assertNotIn(
            PAT["title"], self.titles(self.fixture.user, "personal access token")
        )

    def test_staff_get_it_marked_disabled(self):
        hits = self.titles(self.fixture.staff, "personal access token")
        self.assertFalse(hits[PAT["title"]]["enabled_here"])

    def test_support_get_it_marked_disabled(self):
        hits = self.titles(self.fixture.global_support, "personal access token")
        self.assertFalse(hits[PAT["title"]]["enabled_here"])

    @override_config(PAT_ENABLED=True)
    def test_enabled_setting_reaches_end_user(self):
        hits = self.titles(self.fixture.user, "personal access token")
        self.assertTrue(hits[PAT["title"]]["enabled_here"])

    def test_catalogue_only_hides_ordering_guides_from_end_users(self):
        self.assertIn(ORDERING["title"], self.titles(self.fixture.user, "pricing"))
        Feature.objects.create(key="marketplace.catalogue_only", value=True)
        deployment_profile.invalidate_cache()
        self.assertNotIn(ORDERING["title"], self.titles(self.fixture.user, "pricing"))
        hits = self.titles(self.fixture.staff, "pricing")
        self.assertFalse(hits[ORDERING["title"]]["enabled_here"])

    def test_unknown_term_is_dropped_for_end_users_and_kept_for_staff(self):
        self.assertNotIn(FUTURE["title"], self.titles(self.fixture.user, "teleport"))
        self.assertIn(FUTURE["title"], self.titles(self.fixture.staff, "teleport"))


class AudienceTest(DocsTestMixin, TestCase):
    def test_end_user_gets_only_sections_for_all(self):
        hits = self.titles(self.fixture.user, "project")
        self.assertIn(PROJECTS["title"], hits)
        self.assertNotIn(STAFF_USERS["title"], hits)
        self.assertNotIn(ADMIN["title"], hits)

    def test_support_gets_staff_users_but_not_admin_guide(self):
        hits = self.titles(self.fixture.global_support, "project")
        self.assertIn(STAFF_USERS["title"], hits)
        self.assertNotIn(ADMIN["title"], hits)

    def test_staff_get_everything(self):
        hits = self.titles(self.fixture.staff, "project")
        self.assertIn(STAFF_USERS["title"], hits)
        self.assertIn(ADMIN["title"], hits)


class SearchToolTest(DocsTestMixin, TestCase):
    def test_result_carries_link_and_excerpt(self):
        hit = self.titles(self.fixture.user, "create a project")[PROJECTS["title"]]
        self.assertEqual(
            hit["url"],
            "https://docs.waldur.com/user-guide/end-users/projects/#create-a-project",
        )
        self.assertEqual(hit["excerpt"], PROJECTS["text"])
        self.assertEqual(hit["breadcrumb"], PROJECTS["breadcrumb"])

    @override_config(DOCS_URL="https://docs.example.com/")
    def test_link_uses_docs_url(self):
        hit = self.titles(self.fixture.user, "create a project")[PROJECTS["title"]]
        self.assertTrue(hit["url"].startswith("https://docs.example.com/user-guide/"))

    def test_no_match_says_so(self):
        result = self.tool.execute(self.fixture.user, {"query": "kubernetes"})
        self.assertEqual(result["data"]["sections"], [])
        self.assertIn("does not cover", result["summary"])

    def test_empty_query_is_rejected(self):
        result = self.tool.execute(self.fixture.user, {"query": "  "})
        self.assertEqual(result["type"], "validation_error")

    def test_returns_at_most_five_sections(self):
        corpus.load_sections(
            [
                corpus._parse_line(json.dumps({**PROJECTS, "anchor": f"part-{i}"}), i)
                for i in range(10)
            ]
        )
        result = self.tool.execute(self.fixture.user, {"query": "project"})
        self.assertEqual(len(result["data"]["sections"]), search.MAX_RESULTS)

    def test_boost_orders_equal_matches(self):
        corpus.load_sections(
            [
                corpus._parse_line(
                    json.dumps({**PROJECTS, "anchor": "low", "boost": 0.3}), 1
                ),
                corpus._parse_line(
                    json.dumps({**PROJECTS, "anchor": "high", "boost": 1.5}), 2
                ),
            ]
        )
        result = self.tool.execute(self.fixture.user, {"query": "project"})
        urls = [s["url"] for s in result["data"]["sections"]]
        self.assertTrue(urls[0].endswith("#high"))
        self.assertTrue(urls[-1].endswith("#low"))


@override_config(PAT_ENABLED=False, AI_ASSISTANT_ENABLED=False)
@mock.patch.object(corpus, "is_available", return_value=True)
class DocsSearchApiTest(DocsTestMixin, test.APITestCase):
    url = reverse("docs-search")

    def get(self, user, **params):
        self.client.force_authenticate(user)
        return self.client.get(self.url, params)

    def titles(self, user, query):
        response = self.get(user, query=query)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {s["title"]: s for s in response.data["sections"]}

    def test_works_without_the_ai_assistant(self, _):
        response = self.get(self.fixture.user, query="create a project")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["query"], "create a project")
        hit = response.data["sections"][0]
        self.assertEqual(hit["title"], PROJECTS["title"])
        self.assertEqual(
            hit["url"],
            "https://docs.waldur.com/user-guide/end-users/projects/#create-a-project",
        )
        self.assertTrue(hit["enabled_here"])

    def test_filters_as_the_assistant_does(self, _):
        hits = self.titles(self.fixture.user, "project")
        self.assertNotIn(STAFF_USERS["title"], hits)
        self.assertNotIn(ADMIN["title"], hits)
        self.assertNotIn(PAT["title"], self.titles(self.fixture.user, "token"))

        hits = self.titles(self.fixture.staff, "personal access token")
        self.assertFalse(hits[PAT["title"]]["enabled_here"])

    def test_limit(self, _):
        corpus.load_sections(
            [
                corpus._parse_line(json.dumps({**PROJECTS, "anchor": f"part-{i}"}), i)
                for i in range(12)
            ]
        )
        response = self.get(self.fixture.user, query="project")
        self.assertEqual(len(response.data["sections"]), search.MAX_RESULTS)
        response = self.get(self.fixture.user, query="project", limit=10)
        self.assertEqual(len(response.data["sections"]), 10)
        response = self.get(
            self.fixture.user, query="project", limit=search.MAX_PAGE_RESULTS + 1
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_query_is_required(self, _):
        for params in ({}, {"query": "   "}, {"query": "x" * 301}):
            with self.subTest(params=params):
                response = self.get(self.fixture.user, **params)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_anonymous_is_refused(self, _):
        response = self.client.get(self.url, {"query": "project"})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_corpus_answers_503(self, is_available):
        is_available.return_value = False
        response = self.get(self.fixture.user, query="project")
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @override_config(PAT_ENABLED=True)
    def test_token_without_staff_scope_cannot_read_as_staff(self, _):
        staff = self.fixture.staff
        staff.can_use_personal_access_tokens = True
        staff.save(update_fields=["can_use_personal_access_tokens"])
        pat = _create_pat(staff, [PermissionEnum.LIST_PROJECTS.value], [])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get(self.url, {"query": "project"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# Cold caches: corpus terms, settings, feature flags, offering types, the HPC
# capability, then the search and DOCS_URL. None of these grows with the data.
COLD_QUERIES = 9


@pytest.mark.django_db
def test_search_runs_a_bounded_number_of_queries(django_assert_max_num_queries):
    cache.clear()
    corpus.load_sections(
        [corpus._parse_line(json.dumps(s), i) for i, s in enumerate(CORPUS)]
    )
    fixture = structure_fixtures.ProjectFixture()
    user = fixture.user
    marketplace_factories.OfferingFactory.create_batch(3)
    tool = SearchDocsTool()
    tool.execute(user, {"query": "project"})  # warms the caches
    # The search and DOCS_URL.
    with django_assert_max_num_queries(2):
        tool.execute(user, {"query": "project"})
    cache.clear()
    with django_assert_max_num_queries(COLD_QUERIES):
        tool.execute(user, {"query": "project"})


class ToolSetTest(TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def corpus_env(self, path):
        return mock.patch.dict(os.environ, {"WALDUR_DOCS_CORPUS_PATH": str(path)})

    def test_without_corpus_no_tool_set_has_search_docs(self):
        with self.corpus_env(Path(self.tmp.name) / "missing.jsonl.gz"):
            for user in (
                self.fixture.user,
                self.fixture.global_support,
                self.fixture.staff,
            ):
                self.assertNotIn(ToolName.SEARCH_DOCS, get_tool_set_for_user(user))

    def test_with_corpus_every_authenticated_role_has_search_docs(self):
        with self.corpus_env(write_corpus(self.tmp.name, CORPUS)):
            for user in (
                self.fixture.user,
                self.fixture.global_support,
                self.fixture.staff,
            ):
                self.assertIn(ToolName.SEARCH_DOCS, get_tool_set_for_user(user))
            self.assertIn(
                ToolName.SEARCH_DOCS,
                get_tool_set_for_user(self.fixture.user, delegated=True),
            )
            self.assertNotIn(ToolName.SEARCH_DOCS, get_tool_set_for_user(None))

    def test_executor_refuses_without_corpus(self):
        with self.corpus_env(Path(self.tmp.name) / "missing.jsonl.gz"):
            result = ToolExecutor(self.fixture.user).execute_tool(
                "search_docs", {"query": "project"}
            )
        self.assertEqual(result["error"], "Permission denied")


class LoadDocsCorpusTest(TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def load(self, path):
        out = StringIO()
        call_command("load_docs_corpus", str(path), stdout=out)
        return out.getvalue()

    def test_missing_corpus_is_a_no_op(self):
        output = self.load(Path(self.tmp.name) / "missing.jsonl.gz")
        self.assertIn("No docs corpus", output)
        self.assertFalse(DocSection.objects.exists())

    def test_load_creates_sections(self):
        self.load(write_corpus(self.tmp.name, CORPUS))
        self.assertEqual(DocSection.objects.count(), len(CORPUS))
        section = DocSection.objects.get(path=PAT["path"])
        self.assertEqual(section.requires, ["setting:PAT_ENABLED"])
        self.assertEqual(section.audience, "all")

    def test_unchanged_corpus_writes_nothing(self):
        path = write_corpus(self.tmp.name, CORPUS)
        self.load(path)
        # A savepoint around the read of the existing rows, and no write.
        with self.assertNumQueries(3):
            result = corpus.load_corpus(path)
        self.assertEqual(result.written, 0)
        self.assertEqual(result.unchanged, len(CORPUS))

    def test_changed_corpus_updates_creates_and_deletes(self):
        self.load(write_corpus(self.tmp.name, CORPUS))
        changed = {**PROJECTS, "text": "Projects group resources."}
        new = {**PROJECTS, "anchor": "delete-a-project"}
        result = corpus.load_corpus(write_corpus(self.tmp.name, [PAT, changed, new]))
        self.assertEqual(
            (result.created, result.updated, result.deleted, result.unchanged),
            (1, 1, 4, 1),
        )
        self.assertEqual(
            DocSection.objects.get(
                path=PROJECTS["path"], anchor=PROJECTS["anchor"]
            ).text,
            "Projects group resources.",
        )

    def test_invalid_audience_fails(self):
        path = write_corpus(self.tmp.name, [{**PAT, "audience": "everyone"}])
        with self.assertRaisesMessage(Exception, "unknown audience"):
            self.load(path)
        self.assertFalse(DocSection.objects.exists())


class DeploymentProfileTest(TestCase):
    def setUp(self):
        cache.clear()

    def states(self, *terms):
        deployment_profile.invalidate_cache()
        return deployment_profile.get_term_states(terms)

    @override_config(PAT_ENABLED=True)
    def test_setting(self):
        self.assertEqual(
            self.states("setting:PAT_ENABLED")["setting:PAT_ENABLED"], TermState.ON
        )

    def test_feature_and_negated_feature(self):
        key = "marketplace.catalogue_only"
        states = self.states(f"feature:{key}", f"!feature:{key}")
        self.assertEqual(states[f"feature:{key}"], TermState.OFF)
        self.assertEqual(states[f"!feature:{key}"], TermState.ON)
        Feature.objects.create(key=key, value=True)
        states = self.states(f"feature:{key}", f"!feature:{key}")
        self.assertEqual(states[f"feature:{key}"], TermState.ON)
        self.assertEqual(states[f"!feature:{key}"], TermState.OFF)

    def test_feature_default_is_used_without_a_row(self):
        key = "customer.show_openportal_remote_projects"  # declared default=True
        self.assertEqual(self.states(f"feature:{key}")[f"feature:{key}"], TermState.ON)

    def test_offering_type_needs_an_active_offering(self):
        term = "offering_type:OpenStack.Instance"
        self.assertEqual(self.states(term)[term], TermState.OFF)
        marketplace_factories.OfferingFactory(type="OpenStack.Instance")
        self.assertEqual(self.states(term)[term], TermState.ON)

    def test_disabled_offering_type_is_off(self):
        marketplace_factories.OfferingFactory(type="OpenStack.Instance")
        term = "offering_type:OpenStack.Instance"
        with override_config(DISABLED_OFFERING_TYPES=["OpenStack.Instance"]):
            self.assertEqual(self.states(term)[term], TermState.OFF)

    def test_capability(self):
        with mock.patch("waldur_firecrest.workspace.is_configured", return_value=True):
            self.assertEqual(
                self.states("capability:hpc")["capability:hpc"], TermState.ON
            )
        with mock.patch("waldur_firecrest.workspace.is_configured", return_value=False):
            self.assertEqual(
                self.states("capability:hpc")["capability:hpc"], TermState.OFF
            )

    def test_unknown_terms(self):
        terms = (
            "setting:NO_SUCH_SETTING",
            "feature:no.such",
            "offering_type:No.Such",
            "capability:teleport",
            "!setting:PAT_ENABLED",
            "colour:blue",
            "PAT_ENABLED",
        )
        states = self.states(*terms)
        self.assertEqual(set(states.values()), {TermState.UNKNOWN})

    def test_vocabulary_lists_every_kind(self):
        vocabulary = deployment_profile.vocabulary()
        self.assertIn("PAT_ENABLED", vocabulary["setting"])
        self.assertIn("marketplace.catalogue_only", vocabulary["feature"])
        self.assertIn("OpenStack.Instance", vocabulary["offering_type"])
        self.assertEqual(vocabulary["capability"], ["hpc"])

    def test_cache_holds_no_setting_values(self):
        deployment_profile.get_term_states(["setting:PAT_ENABLED"])
        cached = cache.get(deployment_profile.CACHE_KEY)
        self.assertTrue(
            all(isinstance(value, bool) for value in cached["settings"].values())
        )


class HelpersTest(TestCase):
    def test_section_url(self):
        cases = {
            ("index.md", ""): "https://docs.waldur.com/",
            ("user-guide/index.md", "x"): "https://docs.waldur.com/user-guide/#x",
            ("user-guide/a.md", ""): "https://docs.waldur.com/user-guide/a/",
            ("user-guide/a/", "b"): "https://docs.waldur.com/user-guide/a/#b",
        }
        for (path, anchor), url in cases.items():
            self.assertEqual(search.section_url(path, anchor), url)

    def test_excerpt_starts_near_the_match(self):
        text = "filler " * 300 + "the token lives here " + "tail " * 300
        result = search.excerpt(text, "token")
        self.assertIn("token lives here", result)
        self.assertTrue(result.startswith("… "))
        self.assertLessEqual(len(result), search.EXCERPT_LENGTH + 4)

    def test_short_text_is_returned_whole(self):
        self.assertEqual(search.excerpt("a  short\ntext", "short"), "a short text")
