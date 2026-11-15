from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest import mock

from django.test import SimpleTestCase, TestCase

from waldur_core.configuration import config
from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Feature
from waldur_mastermind.chat.docs import evaluation, search

BASE = "https://docs.example.org"


def question(**kwargs):
    defaults = {
        "id": "q",
        "question": "How do I create a project?",
        "category": "howto",
    }
    defaults.update(kwargs)
    return evaluation.Question(**defaults)


@mock.patch.object(search, "docs_base_url", return_value=BASE)
class ScoringTest(SimpleTestCase):
    gold = ["user-guide/customer-organization/project-management.md"]

    def hits(self, *urls, enabled=True):
        return [{"url": url, "enabled_here": enabled} for url in urls]

    def test_gold_page_rank_counts_any_section_of_it(self, _):
        hits = self.hits(
            f"{BASE}/user-guide/other/#a",
            f"{BASE}/user-guide/customer-organization/project-management/#members",
        )
        score = evaluation.score_search(question(gold=self.gold), "owner", "q", hits)
        self.assertEqual(score["rank"], 2)
        self.assertTrue(score["ok"])

    def test_gold_page_returned_to_a_role_it_is_hidden_from_is_a_leak(self, _):
        hits = self.hits(f"{BASE}/user-guide/customer-organization/project-management/")
        score = evaluation.score_search(
            question(gold=self.gold, hidden_from=["member"]), "member", "q", hits
        )
        self.assertTrue(score["hidden_leak"])
        self.assertFalse(score["ok"])

    def test_hidden_gold_page_not_returned_is_ok(self, _):
        score = evaluation.score_search(
            question(gold=self.gold, hidden_from=["member"]), "member", "q", []
        )
        self.assertTrue(score["ok"])
        self.assertFalse(score["hidden_leak"])

    def test_answer_links_that_search_did_not_return_are_invented(self, _):
        returned = f"{BASE}/user-guide/customer-organization/project-management/#a"
        turn = {
            "answer": f"See [this]({returned}) and [that]({BASE}/user-guide/made-up/).",
            "tool_calls": [{"name": "search_docs", "arguments": {"query": "x"}}],
        }
        score = evaluation.score_answer(
            question(gold=self.gold), "owner", turn, {returned}
        )
        self.assertEqual(score["invented_links"], [f"{BASE}/user-guide/made-up/"])
        self.assertTrue(score["cites_gold"])
        self.assertTrue(score["searched"])

    def test_unavailable_answer_must_say_so(self, _):
        q = question(unavailable_for=["member"])
        steps = {"answer": "Open Credentials and click Create.", "tool_calls": []}
        said = {
            "answer": "Personal access tokens are not enabled on this portal.",
            "tool_calls": [],
        }
        self.assertFalse(
            evaluation.score_answer(q, "member", steps, set())["availability_ok"]
        )
        self.assertTrue(
            evaluation.score_answer(q, "member", said, set())["availability_ok"]
        )
        self.assertTrue(
            evaluation.score_answer(q, "staff", steps, set())["availability_ok"]
        )

    def test_required_and_forbidden_phrases(self, _):
        q = question(required=["(?i)credentials"], forbidden=["(?i)click create"])
        score = evaluation.score_answer(
            q, "owner", {"answer": "Click Create.", "tool_calls": []}, set()
        )
        self.assertEqual(score["missing_required"], ["(?i)credentials"])
        self.assertEqual(score["found_forbidden"], ["(?i)click create"])


class VerdictTest(SimpleTestCase):
    def test_json_is_found_after_thinking(self):
        verdict = evaluation.parse_verdict(
            '<think>hmm {"x": 1}</think>\nVerdict: {"grounded": true, '
            '"availability_correct": false, "answer_score": 2, "problems": "steps"}'
        )
        self.assertEqual(
            verdict,
            {
                "grounded": True,
                "availability_correct": False,
                "answer_score": 2,
                "problems": "steps",
            },
        )

    def test_unusable_reply_is_an_error(self):
        self.assertIn("error", evaluation.parse_verdict("no verdict"))
        self.assertIn("error", evaluation.parse_verdict('{"grounded": true}'))


class QuestionsTest(SimpleTestCase):
    def test_bundled_pack_loads(self):
        questions = evaluation.load_questions()
        self.assertTrue(questions)
        for q in questions:
            self.assertTrue(set(q.roles) <= set(evaluation.ROLES), q.id)
            self.assertTrue(set(q.hidden_from) <= set(q.roles), q.id)

    def write(self, text):
        f = NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return Path(f.name)

    def test_only_settings_and_features_can_be_switched(self):
        path = self.write("- id: a\n  question: x\n  switches: {capability:hpc: off}\n")
        with self.assertRaises(ValueError):
            evaluation.load_questions(path)

    def test_off_terms_include_assumed_ones(self):
        path = self.write(
            "- id: a\n  question: x\n  switches: {setting:PAT_ENABLED: off, setting:MATRIX_ENABLED: on}\n"
            "  assume_off: [offering_type:VMware.VirtualMachine]\n"
        )
        (q,) = evaluation.load_questions(path)
        self.assertEqual(
            q.off_terms, ["offering_type:VMware.VirtualMachine", "setting:PAT_ENABLED"]
        )


class SwitchedTest(TestCase):
    @override_config(PAT_ENABLED=True)
    def test_settings_and_features_are_restored(self):
        Feature.objects.update_or_create(
            key="project.show_matrix_chat", defaults={"value": True}
        )
        with evaluation.switched(
            {
                "setting:PAT_ENABLED": False,
                "feature:project.show_matrix_chat": False,
                "feature:marketplace.call_only": True,
            }
        ):
            self.assertFalse(config.PAT_ENABLED)
            self.assertFalse(Feature.objects.get(key="project.show_matrix_chat").value)
            self.assertTrue(Feature.objects.get(key="marketplace.call_only").value)
        self.assertTrue(config.PAT_ENABLED)
        self.assertTrue(Feature.objects.get(key="project.show_matrix_chat").value)
        self.assertFalse(Feature.objects.filter(key="marketplace.call_only").exists())

    def test_restored_after_an_error(self):
        with self.assertRaises(RuntimeError):
            with evaluation.switched({"feature:marketplace.call_only": True}):
                raise RuntimeError
        self.assertFalse(Feature.objects.filter(key="marketplace.call_only").exists())


class UsersTest(TestCase):
    def test_one_user_per_role_and_idempotent(self):
        users = evaluation.ensure_users()
        again = evaluation.ensure_users()
        self.assertEqual(
            {r: u.pk for r, u in users.items()}, {r: u.pk for r, u in again.items()}
        )
        self.assertTrue(users["staff"].is_staff)
        self.assertTrue(users["support"].is_support)
        self.assertFalse(users["owner"].is_staff or users["owner"].is_support)
