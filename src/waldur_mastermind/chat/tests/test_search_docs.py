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
from django.core.management.base import CommandError
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

    def test_a_term_loaded_after_the_term_list_was_cached_hides_its_section(self):
        # The term list is cached per process; a corpus loaded since may
        # require a term it does not hold. The end user must not get it.
        self.titles(self.fixture.user, "project")
        corpus.load_sections(
            [
                corpus._parse_line(json.dumps(s), i)
                for i, s in enumerate(
                    [
                        *CORPUS,
                        {
                            **PROJECTS,
                            "anchor": "archive",
                            "title": "Archive a project",
                            "requires": ["setting:PAT_ENABLED"],
                        },
                    ]
                )
            ]
        )
        self.assertNotIn("Archive a project", self.titles(self.fixture.user, "project"))
        staff_hits = self.titles(self.fixture.staff, "project")
        self.assertFalse(staff_hits["Archive a project"]["enabled_here"])

    @override_config(MATRIX_ENABLED=True)
    def test_staff_see_an_uncached_term_that_is_on_as_enabled(self):
        # MATRIX_ENABLED is in no section of CORPUS, so not in the cached list.
        self.titles(self.fixture.staff, "project")
        corpus.load_sections(
            [
                corpus._parse_line(json.dumps(s), i)
                for i, s in enumerate(
                    [
                        *CORPUS,
                        {
                            **PROJECTS,
                            "anchor": "tokens",
                            "title": "Project tokens",
                            "requires": ["setting:MATRIX_ENABLED"],
                        },
                    ]
                )
            ]
        )
        hits = self.titles(self.fixture.staff, "project")
        self.assertTrue(hits["Project tokens"]["enabled_here"])

    def test_unknown_term_is_not_reported_as_enabled(self):
        # This code cannot tell whether the setting is on, so staff must not be
        # told the feature works here.
        for user in (self.fixture.staff, self.fixture.global_support):
            with self.subTest(user=user):
                hits = self.titles(user, "teleport")
                self.assertFalse(hits[FUTURE["title"]]["enabled_here"])


@override_config(PAT_ENABLED=False)
class SwitchedOffHereTest(DocsTestMixin, TestCase):
    PAT_OFF = [{"name": "Personal access tokens", "term": "setting:PAT_ENABLED"}]

    def result(self, user, query):
        return self.tool.execute(user, {"query": query})

    def test_end_user_is_told_the_feature_is_off_not_shown_its_section(self):
        result = self.result(self.fixture.user, "personal access token")
        self.assertEqual(result["data"]["switched_off_here"], self.PAT_OFF)
        self.assertEqual(result["data"]["sections"], [])
        self.assertIn("switched off on this portal", result["summary"])
        self.assertNotIn(PAT["text"], json.dumps(result))

    def test_staff_get_the_section_and_the_name(self):
        result = self.result(self.fixture.staff, "personal access token")
        self.assertEqual(result["data"]["switched_off_here"], self.PAT_OFF)
        self.assertIn(PAT["title"], {s["title"] for s in result["data"]["sections"]})

    @override_config(PAT_ENABLED=True)
    def test_nothing_is_named_when_the_feature_is_on(self):
        result = self.result(self.fixture.user, "personal access token")
        self.assertEqual(result["data"]["switched_off_here"], [])

    def test_a_query_about_something_else_names_nothing(self):
        result = self.result(self.fixture.user, "add a project")
        self.assertEqual(result["data"]["switched_off_here"], [])

    def test_a_term_that_may_be_on_is_not_named(self):
        # The corpus knows a setting this code does not: it may be on.
        result = self.result(self.fixture.user, "teleport")
        self.assertEqual(result["data"]["switched_off_here"], [])

    def test_unnamed_term_falls_back_to_its_description(self):
        self.assertEqual(
            deployment_profile.term_name("feature:customer.show_project_digest"),
            "Enable display of project digest configuration in organization settings",
        )

    def test_questions_written_for_a_hidden_section_do_not_name_its_feature(self):
        corpus.load_sections(
            [
                corpus._parse_line(
                    json.dumps({**PAT, "context": "How do I get project API access?"}),
                    1,
                )
            ]
        )
        # Only the questions mention a project: nothing is named.
        result = self.result(self.fixture.user, "project API access")
        self.assertEqual(result["data"]["switched_off_here"], [])
        result = self.result(self.fixture.user, "personal access token")
        self.assertEqual(result["data"]["switched_off_here"], self.PAT_OFF)


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

    def test_query_in_other_words_still_finds_the_guide(self):
        # "new" is not in the section, so not every word matches.
        hits = self.titles(self.fixture.user, "how do I create a new project")
        self.assertIn(PROJECTS["title"], hits)

    def test_sections_matching_every_word_come_first(self):
        result = self.tool.execute(self.fixture.user, {"query": "create project"})
        self.assertEqual(result["data"]["sections"][0]["title"], PROJECTS["title"])

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

    def test_context_words_find_a_section_its_text_does_not_use(self):
        corpus.load_sections(
            [
                corpus._parse_line(
                    json.dumps(
                        {**PROJECTS, "context": "How do I start a new workspace?"}
                    ),
                    1,
                )
            ]
        )
        titles = self.titles(self.fixture.user, "start workspace")
        self.assertEqual(list(titles), ["Create a project"])
        # Context helps find the section; it is not shown.
        self.assertNotIn("workspace", titles["Create a project"]["excerpt"])

    def load_concept_and_howto(self):
        concept = {
            **PROJECTS,
            "anchor": "concept",
            "title": "Projects",
            "text": "A project groups resources and members of an organization.",
            "kind": "concept",
        }
        howto = {
            **PROJECTS,
            "anchor": "howto",
            "title": "Project members",
            "text": "Add members to a project of an organization; a project groups "
            "resources and members, and members see project resources.",
            "kind": "howto",
        }
        corpus.load_sections(
            [
                corpus._parse_line(json.dumps(s), i)
                for i, s in enumerate([concept, howto])
            ]
        )

    def anchors(self, query):
        result = self.tool.execute(self.fixture.user, {"query": query})
        return [s["url"].rpartition("#")[2] for s in result["data"]["sections"]]

    def test_concept_question_brings_the_concept_section_up(self):
        self.load_concept_and_howto()
        self.assertEqual(self.anchors("project members resources")[0], "howto")
        self.assertEqual(
            self.anchors("what is a project members resources")[0], "concept"
        )
        self.assertEqual(
            self.anchors("project members resources difference")[0], "concept"
        )

    def test_concept_queries(self):
        for query in (
            "What is a project?",
            "organization project difference",
            "member vs manager",
            "how do roles differ",
            "member compared to manager",
            "resource end date passed what happens",
            "how billing works",
            "how does billing work",
        ):
            self.assertTrue(search.is_concept_query(query), query)
        for query in (
            "How do I create a project?",
            "add member",
            "whatever",
            "Why can't I sign in with my passkey?",
            "compare invoices between months",
            "How do I work with volumes?",
            "how can I work around the quota",
            "Show an overview of my costs",
        ):
            self.assertFalse(search.is_concept_query(query), query)


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

    @override_config(DOCS_SEARCH_ENABLED=False)
    def test_turned_off_answers_424(self, _):
        response = self.get(self.fixture.staff, query="project")
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)

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
# capability, then the search, DOCS_URL and the hidden sections that name what
# is switched off. None of these grows with the data.
COLD_QUERIES = 10


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
    # The search, DOCS_URL and the hidden sections that name what is switched
    # off (the corpus has a term that is off here).
    with django_assert_max_num_queries(3):
        tool.execute(user, {"query": "project"})
    cache.clear()
    with django_assert_max_num_queries(COLD_QUERIES):
        tool.execute(user, {"query": "project"})


class ToolSetTest(TestCase):
    def setUp(self):
        cache.clear()
        self.fixture = structure_fixtures.ProjectFixture()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def corpus_env(self, path):
        return mock.patch.dict(os.environ, {"WALDUR_DOCS_CORPUS_PATH": str(path)})

    def users(self):
        return (self.fixture.user, self.fixture.global_support, self.fixture.staff)

    def test_without_corpus_no_tool_set_has_search_docs(self):
        with self.corpus_env(Path(self.tmp.name) / "missing.jsonl.gz"):
            for user in self.users():
                self.assertNotIn(ToolName.SEARCH_DOCS, get_tool_set_for_user(user))

    def test_corpus_file_that_was_never_loaded_offers_no_tool(self):
        # Every search would answer "not covered".
        with self.corpus_env(write_corpus(self.tmp.name, CORPUS)):
            for user in self.users():
                self.assertNotIn(ToolName.SEARCH_DOCS, get_tool_set_for_user(user))

    def test_with_corpus_every_authenticated_role_has_search_docs(self):
        call_command(
            "load_docs_corpus",
            str(write_corpus(self.tmp.name, CORPUS)),
            stdout=StringIO(),
        )
        for user in self.users():
            self.assertIn(ToolName.SEARCH_DOCS, get_tool_set_for_user(user))
        self.assertIn(
            ToolName.SEARCH_DOCS,
            get_tool_set_for_user(self.fixture.user, delegated=True),
        )
        self.assertNotIn(ToolName.SEARCH_DOCS, get_tool_set_for_user(None))

    def test_availability_is_cached_and_reset_by_a_load(self):
        self.assertFalse(corpus.is_available())
        with self.assertNumQueries(0):
            self.assertFalse(corpus.is_available())
        call_command(
            "load_docs_corpus",
            str(write_corpus(self.tmp.name, CORPUS)),
            stdout=StringIO(),
        )
        self.assertTrue(corpus.is_available())

    @override_config(DOCS_SEARCH_ENABLED=False)
    def test_turned_off_no_tool_set_has_search_docs(self):
        call_command(
            "load_docs_corpus",
            str(write_corpus(self.tmp.name, CORPUS)),
            stdout=StringIO(),
        )
        for user in self.users():
            self.assertNotIn(ToolName.SEARCH_DOCS, get_tool_set_for_user(user))
        self.assertNotIn(
            ToolName.SEARCH_DOCS,
            get_tool_set_for_user(self.fixture.staff, delegated=True),
        )

    def test_executor_refuses_without_corpus(self):
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

    def test_strict_fails_on_a_missing_or_unreadable_corpus(self):
        with self.assertRaisesMessage(CommandError, "No docs corpus"):
            call_command(
                "load_docs_corpus",
                str(Path(self.tmp.name) / "missing.jsonl.gz"),
                strict=True,
                stdout=StringIO(),
            )
        broken = Path(self.tmp.name) / "broken.jsonl.gz"
        broken.write_bytes(b"not gzip")
        with self.assertRaisesMessage(CommandError, "cannot be read"):
            call_command(
                "load_docs_corpus", str(broken), strict=True, stdout=StringIO()
            )

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
        self.assertEqual((section.kind, section.context), ("", ""))

    def test_kind_and_context_are_loaded_and_an_unknown_kind_dropped(self):
        path = write_corpus(
            self.tmp.name,
            [
                {**PAT, "kind": "concept", "context": "How do I get a token?"},
                {**PROJECTS, "kind": "tutorial"},
            ],
        )
        self.load(path)
        pat = DocSection.objects.get(path=PAT["path"])
        self.assertEqual((pat.kind, pat.context), ("concept", "How do I get a token?"))
        self.assertEqual(DocSection.objects.get(path=PROJECTS["path"]).kind, "")

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

    def load_with_errors(self, path):
        out, err = StringIO(), StringIO()
        call_command("load_docs_corpus", str(path), stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_lines_the_columns_cannot_hold_are_skipped(self):
        bad = [
            {**PAT, "anchor": "a" * 256},
            {**PAT, "path": "p" * 501},
            {**PAT, "anchor": 5},
            {**PAT, "requires": ["setting:" + "X" * 260]},
            {**PAT, "boost": "NaN"},
            {**PAT, "boost": -1},
            {**PAT, "text": ["not", "a", "string"]},
            {**PAT, "text": "binary \u0000 snippet"},
            {**PAT, "title": "bad \u0000 title"},
        ]
        output, errors = self.load_with_errors(
            write_corpus(self.tmp.name, [*bad, PROJECTS])
        )
        self.assertIn("1 created", output)
        self.assertIn(f"{len(bad)} invalid line", errors)
        self.assertEqual(
            list(DocSection.objects.values_list("path", flat=True)),
            [PROJECTS["path"]],
        )

    def test_invalid_line_is_skipped_and_the_rest_loads(self):
        # An audience this release does not know cannot be filtered, so that
        # section is left out; the deployment goes on.
        path = write_corpus(self.tmp.name, [{**PAT, "audience": "everyone"}, PROJECTS])
        output, errors = self.load_with_errors(path)
        self.assertIn("1 created", output)
        self.assertIn("1 invalid line", errors)
        self.assertEqual(
            list(DocSection.objects.values_list("path", flat=True)),
            [PROJECTS["path"]],
        )

    def test_corpus_without_a_valid_line_leaves_loaded_sections(self):
        self.load(write_corpus(self.tmp.name, CORPUS))
        path = write_corpus(self.tmp.name, [{**PAT, "audience": "everyone"}])
        output, errors = self.load_with_errors(path)
        self.assertIn("no valid section", errors)
        self.assertIn("left unchanged", errors)
        self.assertEqual(DocSection.objects.count(), len(CORPUS))

    def test_unreadable_corpus_leaves_loaded_sections(self):
        self.load(write_corpus(self.tmp.name, CORPUS))
        for content in (b"not gzip at all", gzip.compress(b'{"path": "a"}\n')[:-8]):
            with self.subTest(content=content):
                path = Path(self.tmp.name) / "broken.jsonl.gz"
                path.write_bytes(content)
                _, errors = self.load_with_errors(path)
                self.assertIn("cannot be read", errors)
                self.assertEqual(DocSection.objects.count(), len(CORPUS))


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

    def test_excerpt_starts_at_a_whole_word(self):
        text = (
            "Your email address is kept. "
            + "filler " * 300
            + "Add user steps "
            + "tail " * 300
        )
        self.assertIn("Add user steps", search.excerpt(text, "add user"))

    def test_excerpt_falls_back_to_a_longer_word(self):
        text = "filler " * 300 + "the administrator decides " + "tail " * 300
        self.assertIn("administrator", search.excerpt(text, "admin rights"))

    def test_excerpt_starts_near_the_match(self):
        text = "filler " * 300 + "the token lives here " + "tail " * 300
        result = search.excerpt(text, "token")
        self.assertIn("token lives here", result)
        self.assertTrue(result.startswith("… "))
        self.assertLessEqual(len(result), search.EXCERPT_LENGTH + 4)

    def test_short_text_is_returned_whole(self):
        self.assertEqual(search.excerpt("a  short\ntext", "short"), "a short text")
