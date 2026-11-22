import io
import os
import subprocess  # noqa: S404
import sys
from decimal import Decimal, InvalidOperation
from unittest import mock

import openpyxl
from ddt import data, ddt
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import status

from waldur_mastermind.proposal import exports, models
from waldur_mastermind.proposal.enums import ProposalStates

from . import factories
from .test_exports import BaseExportTest, read_csv

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook_bytes(response) -> bytes:
    return b"".join(response.streaming_content)


def read_workbook(response):
    """The response's only sheet, loaded the way a reader would open it."""
    workbook = openpyxl.load_workbook(io.BytesIO(workbook_bytes(response)))
    assert len(workbook.worksheets) == 1
    return workbook.worksheets[0]


def sheet_rows(sheet):
    return [list(row) for row in sheet.iter_rows(values_only=True)]


def same_cell(value, csv_text) -> bool:
    """Whether a workbook cell holds what the CSV has as text.

    Numbers are compared as numbers: the CSV writes an average of 5 as
    ``5.0``, which a workbook stores, and any reader shows, as 5.
    """
    if value is None:
        return csv_text == ""
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            return Decimal(csv_text) == Decimal(str(value))
        except InvalidOperation:
            return False
    return value == csv_text


class BaseExcelExportTest(BaseExportTest):
    def setUp(self):
        super().setUp()
        self.review = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.fixture.reviewer_1,
            state=models.Review.States.SUBMITTED,
            summary_score=4,
            summary_public_comment="Solid case.",
            summary_private_comment="Reviewer knows the applicant.",
        )

    def workbook(self, action, user="staff", **query):
        response = self.get(user, action, file_format="xlsx", **query)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return read_workbook(response)

    def proposal_row(self, sheet):
        """The header, and the sheet row number of ``self.proposal``."""
        rows = sheet_rows(sheet)
        index = next(i for i, row in enumerate(rows) if row[0] == "ROUND-001")
        return rows[0], index + 1

    def review_cell(self, sheet, column):
        """``column`` of ``self.review``'s row; the fixture adds a review by
        the same reviewer, so the row is found by its creation time."""
        rows = sheet_rows(sheet)
        header = rows[0]
        created = header.index("Created")
        index = next(
            i
            for i, row in enumerate(rows)
            if row[created] == self.review.created.isoformat()
        )
        return sheet.cell(row=index + 1, column=header.index(column) + 1)


@ddt
class ExcelResponseTest(BaseExcelExportTest):
    @data(("export-proposals", "proposals"), ("export-reviews", "reviews"))
    def test_workbook_is_an_xlsx_attachment(self, case):
        action, kind = case
        response = self.get("staff", action, file_format="xlsx")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], XLSX)
        self.assertIn(
            f'filename="{self.call.slug}-{kind}.xlsx"',
            response["Content-Disposition"],
        )
        self.assertEqual(read_workbook(response).title, kind.capitalize())

    @data("export-proposals", "export-reviews")
    def test_csv_stays_the_default(self, action):
        default = self.get("staff", action)
        explicit = self.get("staff", action, file_format="csv")
        self.assertIn("text/csv", explicit["Content-Type"])
        self.assertEqual(
            b"".join(default.streaming_content), b"".join(explicit.streaming_content)
        )

    @data("export-proposals", "export-reviews")
    def test_unknown_file_format_is_refused(self, action):
        for file_format in ("pdf", "XLSX", "xls"):
            with self.subTest(file_format=file_format):
                response = self.get("staff", action, file_format=file_format)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("file_format", response.data)

    def test_workbook_is_sent_from_a_file_not_held_in_memory(self):
        response = self.get("staff", "export-proposals", file_format="xlsx")
        self.assertTrue(response.streaming)
        body = workbook_bytes(response)
        self.assertEqual(int(response["Content-Length"]), len(body))
        openpyxl.load_workbook(io.BytesIO(body))

    def test_workbook_is_written_in_write_only_mode(self):
        with mock.patch("openpyxl.Workbook", wraps=openpyxl.Workbook) as workbook:
            self.workbook("export-proposals")
        workbook.assert_called_once_with(write_only=True)


@ddt
class ExcelAccessTest(BaseExcelExportTest):
    @data(
        *(
            (action, user)
            for action in ("export-proposals", "export-reviews")
            for user in (
                "staff",
                "global_support",
                "call_manager",
                "call_organizer_user",
            )
        )
    )
    def test_call_management_may_export(self, case):
        action, user = case
        response = self.get(user, action, file_format="xlsx")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        *(
            (action, user)
            for action in ("export-proposals", "export-reviews")
            for user in ("reviewer_1", "panel_member")
        )
    )
    def test_evaluators_are_refused(self, case):
        action, user = case
        response = self.get(user, action, file_format="xlsx")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("export-proposals", "export-reviews")
    def test_applicant_is_refused(self, action):
        self.client.force_authenticate(self.proposal.created_by)
        url = factories.CallFactory.get_protected_url(self.call, action)
        response = self.client.get(url, {"file_format": "xlsx"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_private_comment_is_absent_from_the_review_workbook(self):
        sheet = self.workbook("export-reviews")
        values = {value for row in sheet_rows(sheet) for value in row}
        self.assertNotIn("Reviewer knows the applicant.", values)
        self.assertFalse([value for value in values if "private" in str(value).lower()])
        self.assertIn("Solid case.", values)


class ExcelMatchesCsvTest(BaseExcelExportTest):
    """The workbook is the CSV's header and rows, cell for cell, in order."""

    def setUp(self):
        super().setUp()
        step = models.CallWorkflowStep.review_step_for(self.call)
        excellence = models.WorkflowCriterion.objects.create(
            workflow_step=step, name="Excellence", order=0
        )
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.fixture.reviewer_2,
            state=models.Review.States.SUBMITTED,
            summary_score=3,
            criteria_scores={excellence.uuid.hex: 5},
            summary_public_comment="Line one\nline two",
        )
        for index, limit in enumerate((-5, 0.25, 1200)):
            proposal = factories.ProposalFactory(
                round=self.fixture.round,
                state=ProposalStates.SUBMITTED,
                slug=f"ROUND-1{index}",
            )
            factories.RequestedResourceFactory(
                proposal=proposal,
                requested_offering=self.requested_offering,
                limits={"cpu_hours": limit},
            )
        # Left out by the state filter below.
        factories.ProposalFactory(round=self.fixture.round, slug="ROUND-DRAFT")

    def assert_same_rows(self, action, **query):
        csv_rows = read_csv(self.get("staff", action, **query))
        rows = sheet_rows(self.workbook(action, **query))
        self.assertGreater(len(csv_rows), 1)
        self.assertEqual(rows[0], csv_rows[0])
        self.assertEqual(len(rows), len(csv_rows))
        for row, csv_row in zip(rows, csv_rows):
            self.assertEqual(len(row), len(csv_row))
            mismatches = [
                (index, value, csv_text)
                for index, (value, csv_text) in enumerate(zip(row, csv_row))
                if not same_cell(value, csv_text)
            ]
            self.assertEqual(mismatches, [], csv_row)

    def test_proposal_workbook_holds_the_csv_rows(self):
        self.assert_same_rows("export-proposals")

    def test_review_workbook_holds_the_csv_rows(self):
        self.assert_same_rows("export-reviews")

    def test_filters_apply_to_the_workbook(self):
        self.assert_same_rows("export-proposals", proposal_state="submitted")
        rows = sheet_rows(self.workbook("export-proposals", proposal_state="submitted"))
        self.assertNotIn("ROUND-DRAFT", [row[0] for row in rows])
        self.assert_same_rows(
            "export-reviews", reviewer_uuid=self.fixture.reviewer_2.uuid.hex
        )


class ExcelCellTest(BaseExcelExportTest):
    def test_amounts_counts_and_scores_are_numbers(self):
        self.requested_resource.limits = {"cpu_hours": -5}
        self.requested_resource.save()
        sheet = self.workbook("export-proposals")
        header, row = self.proposal_row(sheet)
        for column, value in (
            ("HPC / CPU hours (h)", -5),
            ("Reviews submitted", 1),
            ("Average score", 4),
        ):
            with self.subTest(column=column):
                cell = sheet.cell(row=row, column=header.index(column) + 1)
                self.assertEqual(cell.data_type, "n")
                self.assertEqual(cell.value, value)

        score = self.review_cell(self.workbook("export-reviews"), "Score")
        self.assertEqual((score.data_type, score.value), ("n", 4))

    def test_fractional_amount_keeps_its_precision(self):
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={"cpu_hours": 0.2},
        )
        self.requested_resource.limits = {"cpu_hours": 0.1}
        self.requested_resource.save()
        sheet = self.workbook("export-proposals")
        header, row = self.proposal_row(sheet)
        cell = sheet.cell(row=row, column=header.index("HPC / CPU hours (h)") + 1)
        self.assertEqual(cell.value, 0.3)

    def test_formula_in_a_name_is_text_as_typed(self):
        self.proposal.name = '=HYPERLINK("http://example.com")'
        self.proposal.save()
        sheet = self.workbook("export-proposals")
        header, row = self.proposal_row(sheet)
        cell = sheet.cell(row=row, column=header.index("Name") + 1)
        self.assertEqual(cell.data_type, "s")
        self.assertEqual(cell.value, '=HYPERLINK("http://example.com")')

    def test_text_that_reads_as_a_number_stays_text(self):
        # Only what the export computes as a number becomes one: a name
        # typed as 0042 must not turn into 42.
        self.proposal.name = "0042"
        self.proposal.save()
        sheet = self.workbook("export-proposals")
        header, row = self.proposal_row(sheet)
        cell = sheet.cell(row=row, column=header.index("Name") + 1)
        self.assertEqual((cell.data_type, cell.value), ("s", "0042"))

    def test_text_spreadsheets_would_interpret_is_text(self):
        for comment in ("=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(1,1)", "#N/A"):
            with self.subTest(comment=comment):
                self.review.summary_public_comment = comment
                self.review.save()
                cell = self.review_cell(
                    self.workbook("export-reviews"), "Public comment"
                )
                self.assertEqual((cell.data_type, cell.value), ("s", comment))

    def test_control_characters_still_give_a_valid_workbook(self):
        # NUL is left out: Postgres refuses to store it in the first place.
        self.proposal.name = "Pasted\x0bsummary\x08 with\x1f controls\uffff"
        self.proposal.save()
        self.review.summary_public_comment = "Tab\tand\nnewline\ufffe stay"
        self.review.save()
        sheet = self.workbook("export-proposals")
        header, row = self.proposal_row(sheet)
        name = sheet.cell(row=row, column=header.index("Name") + 1).value
        self.assertEqual(name, "Pastedsummary with controls")

        comment = self.review_cell(self.workbook("export-reviews"), "Public comment")
        self.assertEqual(comment.value, "Tab\tand\nnewline stay")

    def test_lone_surrogate_still_gives_a_valid_workbook(self):
        # Postgres cannot store a lone surrogate, so the writer is fed one
        # directly.
        response = exports.xlsx_response(
            iter([["Name"], ["broken\ud800 pair\udfff"]]), "x.xlsx", "Sheet"
        )
        self.assertEqual(sheet_rows(read_workbook(response))[1], ["broken pair"])

    def test_text_past_the_cell_limit_is_cut_visibly(self):
        limit = exports.XLSX_MAX_CELL_LENGTH
        self.review.summary_public_comment = "a" * (limit + 1000)
        self.review.save()
        cell = self.review_cell(self.workbook("export-reviews"), "Public comment")
        self.assertEqual(len(cell.value), limit)
        self.assertEqual(cell.value, "a" * (limit - 1) + "\u2026")

    def test_text_at_the_cell_limit_is_kept_whole(self):
        text = "b" * exports.XLSX_MAX_CELL_LENGTH
        response = exports.xlsx_response(iter([["Name"], [text]]), "x.xlsx", "Sheet")
        self.assertEqual(sheet_rows(read_workbook(response))[1], [text])

    def test_header_is_bold_and_frozen(self):
        for action in ("export-proposals", "export-reviews"):
            with self.subTest(action=action):
                sheet = self.workbook(action)
                self.assertEqual(sheet.freeze_panes, "A2")
                self.assertTrue(all(cell.font.b for cell in sheet[1]))
                self.assertFalse(any(cell.font.b for cell in sheet[2] if cell.value))

    def test_columns_are_sized_from_their_content(self):
        sheet = self.workbook("export-proposals")
        header = sheet_rows(sheet)[0]
        widths = {
            title: sheet.column_dimensions[
                openpyxl.utils.get_column_letter(index + 1)
            ].width
            for index, title in enumerate(header)
        }
        self.assertGreater(widths["Applicant email"], widths["State"])
        self.assertLessEqual(max(widths.values()), exports.XLSX_MAX_WIDTH)
        self.assertGreaterEqual(min(widths.values()), exports.XLSX_MIN_WIDTH)

    def test_workbook_does_not_cost_a_query_per_proposal(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CallFactory.get_protected_url(self.call, "export-proposals")

        def export_query_count():
            with CaptureQueriesContext(connection) as queries:
                read_workbook(self.client.get(url, {"file_format": "xlsx"}))
            return len(queries.captured_queries)

        baseline = export_query_count()
        for index in range(10):
            proposal = factories.ProposalFactory(
                round=self.fixture.round, state=ProposalStates.SUBMITTED
            )
            factories.RequestedResourceFactory(
                proposal=proposal,
                requested_offering=self.requested_offering,
                limits={"cpu_hours": 100 * index},
            )
        self.assertEqual(export_query_count(), baseline)


class StartupImportTest(SimpleTestCase):
    def test_openpyxl_is_not_loaded_at_startup(self):
        # In a fresh interpreter: this test process has long since imported it.
        # Resolving the URLconf imports every app's views, which is the path a
        # module-level import would take.
        script = (
            "import sys, django; django.setup();"
            "from django.urls import get_resolver; get_resolver().url_patterns;"
            "from waldur_mastermind.proposal import exports, views;"
            "print(sorted(m for m in sys.modules if m.split('.')[0] == 'openpyxl'))"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=300,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip().splitlines()[-1], "[]")
