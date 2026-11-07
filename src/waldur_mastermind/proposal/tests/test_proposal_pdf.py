"""The proposal as a PDF: each reader's document holds what the API gives
them, and nothing it conceals from them."""

import io
from decimal import Decimal
from unittest import mock

import pypdf
from django.core.cache import cache
from django.test import SimpleTestCase
from rest_framework import status, test

from waldur_core.core import pdf as core_pdf
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, proposal_pdf
from waldur_mastermind.proposal.tests.test_step_inputs import (
    PRIVATE,
    StepInputsTestMixin,
)

NOTES = "Internal: the panel was split."
REASON = "Strong science, weak scaling plan."
RANKING_NOTES = "Ranked first after a long debate."
RELEASED = "Released to you: fund it."


def _flat(text: str) -> str:
    # Lines break wherever the page is full; markers are compared as words.
    return " ".join(text.split())


class ProposalPdfTest(StepInputsTestMixin, test.APITestCase):
    def setUp(self):
        # The endpoint is throttled per user; earlier tests' requests must
        # not count against these.
        cache.clear()
        super().setUp()
        self.reach_consolidated_step()
        instance = self.instance(self.consolidated_step)
        instance.outcome_reason = REASON
        instance.feedback_withheld = True
        instance.internal_notes = NOTES
        instance.save()
        models.ProposalRanking.objects.create(
            round=self.proposal.round,
            proposal=self.proposal,
            rank=1,
            final_score=Decimal("4.50"),
            notes=RANKING_NOTES,
            created_by=self.manager,
        )

    def get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.url("pdf"))

    def text(self, user) -> str:
        response = self.get(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reader = pypdf.PdfReader(io.BytesIO(response.content))
        return _flat("\n".join(page.extract_text() for page in reader.pages))

    def test_whoever_reads_the_proposal_downloads_it(self):
        for user in (
            self.applicant,
            self.manager,
            self.fixture.call_organizer_user,
            self.fixture.staff,
            self.rapporteur_1,
            self.reviewer,
        ):
            with self.subTest(user=user):
                response = self.get(user)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response["Content-Type"], "application/pdf")
                self.assertTrue(response.content.startswith(b"%PDF"))
                self.assertIn("attachment;", response["Content-Disposition"])
                self.assertIn(
                    proposal_pdf.file_name({"slug": self.proposal.slug}),
                    response["Content-Disposition"],
                )

    def test_others_do_not(self):
        for user in (self.bystander, structure_factories.UserFactory()):
            with self.subTest(user=user):
                self.assertEqual(self.get(user).status_code, status.HTTP_404_NOT_FOUND)

    def test_the_applicant_reads_their_application_and_no_more(self):
        text = self.text(self.applicant)
        self.assertIn(_flat(self.proposal.name), text)
        self.assertIn("Project team", text)
        self.assertIn("Evaluation", text)
        for concealed in (NOTES, REASON, RANKING_NOTES, PRIVATE):
            self.assertNotIn(_flat(concealed), text)
        # Who completed each step is the call team's timeline.
        self.assertNotIn("Completed by", text)
        completer = self.instance(self.consolidated_step).completed_by
        self.assertNotIn(completer.full_name, text)
        self.assertNotIn("Ranking", text)

    def test_the_call_team_reads_the_evaluation_record(self):
        completer = self.instance(self.consolidated_step).completed_by
        for user in (self.manager, self.fixture.call_organizer_user):
            with self.subTest(user=user):
                text = self.text(user)
                for shown in (NOTES, REASON, RANKING_NOTES):
                    self.assertIn(_flat(shown), text)
                self.assertIn("Completed by", text)
                self.assertIn(completer.full_name, text)
                self.assertIn("Ranking", text)
                self.assertNotIn(PRIVATE, text)

    def test_a_released_report_reaches_the_applicant(self):
        self.assertNotIn("Released reports", self.text(self.applicant))
        response = self.release(self.manager, text=RELEASED)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        text = self.text(self.applicant)
        self.assertIn("Released reports", text)
        self.assertIn(_flat(RELEASED), text)
        self.assertIn(_flat(REASON), text)
        self.assertNotIn(_flat(NOTES), text)

    def test_markup_in_the_proposal_prints_literally(self):
        name = "Flux #1 $x$ *bold* = <tag> [link]"
        models.Proposal.objects.filter(pk=self.proposal.pk).update(name=name)
        self.assertIn(name, self.text(self.applicant))

    def test_the_document_is_not_cached(self):
        response = self.get(self.applicant)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_a_document_too_long_to_render_is_refused_and_frees_the_lock(self):
        with mock.patch.object(
            core_pdf,
            "render_pdf_isolated",
            side_effect=core_pdf.PdfRenderTimeout("too slow"),
        ):
            response = self.get(self.applicant)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("too long", str(response.data))
        self.assertEqual(self.get(self.applicant).status_code, status.HTTP_200_OK)

    def test_one_render_at_a_time_per_user(self):
        cache.add(f"proposal-pdf:{self.applicant.pk}", True)
        self.assertEqual(
            self.get(self.applicant).status_code, status.HTTP_429_TOO_MANY_REQUESTS
        )
        self.assertEqual(self.get(self.manager).status_code, status.HTTP_200_OK)


class OptionSummaryTest(SimpleTestCase):
    OPTIONS = {
        "order": ["qos", "gpu_model", "notes", "prepaid_duration_months"],
        "options": {
            "qos": {"type": "select_string", "label": "QoS"},
            "gpu_model": {
                "type": "string",
                "label": "GPU model",
                "visible_if": {"field": "qos", "values": ["gpu"]},
            },
            "notes": {"type": "html_text", "label": "Notes"},
            "prepaid_duration_months": {"type": "integer", "label": "Months"},
        },
    }

    def test_answered_options_in_the_offerings_order(self):
        self.assertEqual(
            proposal_pdf.option_summary(
                self.OPTIONS,
                {
                    "notes": "<p>Line <b>one</b></p><p>two</p>",
                    "qos": "gpu",
                    "gpu_model": "A100",
                    "prepaid_duration_months": 12,
                },
            ),
            ["QoS: gpu", "GPU model: A100", "Notes: Line one two"],
        )

    def test_hidden_and_empty_options_are_left_out(self):
        self.assertEqual(
            proposal_pdf.option_summary(
                self.OPTIONS, {"qos": "cpu", "gpu_model": "A100", "notes": ""}
            ),
            ["QoS: cpu"],
        )


class StepsTest(SimpleTestCase):
    def test_each_step_names_its_own_completer(self):
        # A post-award run repeats a step: the same key, another completer.
        steps = [
            {"step": "extension", "step_name": "Extension", "completed_by": "a"},
            {"step": "extension", "step_name": "Extension", "completed_by": "b"},
        ]
        document = proposal_pdf.build_document(
            proposal_pdf.ProposalReads(
                proposal={"name": "P"},
                steps=steps,
                timeline={"steps": []},
                completer_names={"a": "First Person", "b": "Second Person"},
            )
        )
        [evaluation] = [
            section
            for section in document["sections"]
            if section.get("columns") and "Completed by" in section["columns"]
        ]
        self.assertEqual(
            [row[-1] for row in evaluation["rows"]],
            [["First Person"], ["Second Person"]],
        )

    def test_without_the_timeline_nobody_is_named(self):
        document = proposal_pdf.build_document(
            proposal_pdf.ProposalReads(
                proposal={"name": "P"},
                steps=[{"step": "x", "step_name": "X", "completed_by": "a"}],
                completer_names={"a": "First Person"},
            )
        )
        self.assertNotIn("First Person", str(document))
