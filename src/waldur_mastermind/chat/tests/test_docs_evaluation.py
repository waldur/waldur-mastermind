from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase

from waldur_core.configuration import config
from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Feature, User
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

    def test_not_available_is_recognised_in_the_pack_languages(self, _):
        q = question(unavailable_for=["member"])
        for answer in (
            "Isiklikke juurdepääsulube ei ole sellel portaalil lubatud.",
            "Persönliche Zugriffstoken sind auf diesem Portal nicht aktiviert.",
            "Les jetons d'accès personnels ne sont pas activés sur ce portail.",
        ):
            turn = {"answer": answer, "tool_calls": []}
            self.assertTrue(
                evaluation.score_answer(q, "member", turn, set())["availability_ok"],
                answer,
            )

    def test_deactivated_without_negation_is_not_unavailable(self, _):
        q = question(unavailable_for=["member"])
        for answer in (
            "Öffnen Sie Profil und klicken Sie Erstellen. Ein abgelaufenes "
            "Token wird automatisch deaktiviert.",
            "Ouvrez le profil et créez un jeton; le jeton est désactivé après "
            "expiration.",
            "Token on pärast aegumist automaatselt välja lülitatud.",
        ):
            turn = {"answer": answer, "tool_calls": []}
            self.assertFalse(
                evaluation.score_answer(q, "member", turn, set())["availability_ok"],
                answer,
            )

    def test_required_and_forbidden_phrases(self, _):
        q = question(required=["(?i)credentials"], forbidden=["(?i)click create"])
        score = evaluation.score_answer(
            q, "owner", {"answer": "Click Create.", "tool_calls": []}, set()
        )
        self.assertEqual(score["missing_required"], ["(?i)credentials"])
        self.assertEqual(score["found_forbidden"], ["(?i)click create"])


class UnsourcedTermsTest(SimpleTestCase):
    SOURCES = "Open Profile > Credentials > Personal access tokens. Click Save."

    def test_ui_names_missing_from_the_sections_are_reported(self):
        answer = 'Open **Credentials**, click "Create token", then click **Save**.'
        self.assertEqual(
            evaluation.unsourced_terms(answer, self.SOURCES), ["Create token"]
        )

    def test_menu_path_counts_step_by_step_whatever_the_arrow(self):
        answer = "Go to **Profile → Credentials → Personal access tokens**."
        self.assertEqual(evaluation.unsourced_terms(answer, self.SOURCES), [])
        answer = "Go to **Profile → Settings → Tokens**."
        self.assertEqual(
            evaluation.unsourced_terms(answer, self.SOURCES),
            ["Profile → Settings → Tokens"],
        )

    def test_bold_headings_are_not_ui_names(self):
        answer = "**Start the proposal** — then **Steps:** follow the guide."
        self.assertEqual(evaluation.unsourced_terms(answer, ""), [])


class AnswerLanguageTest(SimpleTestCase):
    def test_prose_language_is_found_despite_english_ui_names(self):
        answers = {
            "et": "Avage oma organisatsioon ja klõpsake **Add project**. Kui see "
            "puudub, siis küsige omanikult, kus see on.",
            "de": "Öffnen Sie die Organisation und klicken Sie auf **Add project**. "
            "Sie können den Namen mit der Schaltfläche ändern.",
            "fr": "Ouvrez votre organisation et cliquez sur **Add project**. Vous "
            "pouvez changer le nom dans la page du projet.",
            "en": "Open your organization and click **Add project**. You can "
            "change the name on the project page.",
        }
        for language, answer in answers.items():
            self.assertEqual(evaluation.answer_language(answer), language)
        self.assertEqual(evaluation.answer_language("[Docs](https://x)"), "")

    def test_language_word_lists_share_no_word(self):
        lists = list(evaluation._LANGUAGE_WORDS.items())
        for i, (a, words_a) in enumerate(lists):
            for b, words_b in lists[i + 1 :]:
                self.assertEqual(words_a & words_b, set(), (a, b))

    def test_shared_or_too_few_words_are_no_language(self):
        self.assertEqual(evaluation.answer_language("See on hea."), "")
        self.assertEqual(evaluation.answer_language("Klicken Sie in in in."), "")

    @mock.patch.object(search, "docs_base_url", return_value=BASE)
    def test_answer_in_another_language_than_asked_is_reported(self, _):
        q = question(language="de")
        turn = {
            "answer": "Open the organization and click the button.",
            "tool_calls": [],
        }
        score = evaluation.score_answer(q, "owner", turn, set())
        self.assertEqual(score["language"], "en")
        self.assertFalse(score["language_ok"])
        self.assertIsNone(
            evaluation.score_answer(question(), "owner", turn, set())["language_ok"]
        )


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
    def test_bundled_packs_load(self):
        questions = evaluation.load_questions()
        for name in ("languages", "validation", "test"):
            questions += evaluation.load_questions(
                evaluation.QUESTIONS_FILE.with_name(f"evaluation_questions_{name}.yaml")
            )
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
    def test_deactivated_evaluation_user_is_reactivated(self):
        users = evaluation.ensure_users()
        users["member"].is_active = False
        users["member"].save()
        again = evaluation.ensure_users()
        self.assertEqual(again["member"].pk, users["member"].pk)
        self.assertTrue(again["member"].is_active)

    def test_an_account_it_did_not_create_is_not_made_staff(self):
        User.objects.create(username="docs-eval-staff", email="someone@example.org")
        with self.assertRaises(ValueError):
            evaluation.ensure_users()
        self.assertFalse(User.objects.get(username="docs-eval-staff").is_staff)

    def test_one_user_per_role_and_idempotent(self):
        users = evaluation.ensure_users()
        again = evaluation.ensure_users()
        self.assertEqual(
            {r: u.pk for r, u in users.items()}, {r: u.pk for r, u in again.items()}
        )
        self.assertTrue(users["staff"].is_staff)
        self.assertTrue(users["support"].is_support)
        self.assertFalse(users["owner"].is_staff or users["owner"].is_support)


class CommandCorpusTest(TestCase):
    def test_a_corpus_that_does_not_load_stops_the_run(self):
        f = NamedTemporaryFile("wb", suffix=".jsonl.gz", delete=False)
        f.write(b"not gzip")
        f.close()
        self.addCleanup(Path(f.name).unlink)
        with self.assertRaisesMessage(CommandError, "cannot be read"):
            call_command(
                "ai_assistant",
                "docs_evaluation",
                "--database",
                connection.settings_dict["NAME"],
                "--corpus",
                f.name,
            )
