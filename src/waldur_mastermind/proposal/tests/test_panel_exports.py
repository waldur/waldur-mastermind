"""The evaluations export and the panel sheets: what they hold, the panel and
step filters, and who reads what in them."""

import csv
import io

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from openpyxl import load_workbook
from rest_framework import status, test

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import QuestionTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import exports, models
from waldur_mastermind.proposal.enums import (
    COIStatuses,
    ContributionOutcomes,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, fixtures


def read_csv(response):
    body = b"".join(response.streaming_content).decode("utf-8")
    return list(csv.reader(io.StringIO(body)))


def read_xlsx(response):
    workbook = load_workbook(io.BytesIO(b"".join(response.streaming_content)))
    return {
        sheet.title: [list(row) for row in sheet.iter_rows(values_only=True)]
        for sheet in workbook.worksheets
    }


class PanelExportTestMixin:
    """A call with two panels, physics and chemistry, each with a chair and a
    member. One proposal per panel. A blind step of individual reports by
    the panel members, a panel review chaired by the call's panel chair that
    reads those reports, and a technical assessment per requested offering.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.requested_offering = self.fixture.requested_offering_accepted
        self.offering = self.requested_offering.offering
        self.offering.name = "HPC"
        self.offering.save()
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu_hours",
            name="CPU hours",
            measured_unit="h",
            billing_type=BillingTypes.LIMIT,
        )

        self.p_chair, self.p_member, self.c_chair, self.c_member = (
            self.panel_member() for _ in range(4)
        )
        self.physics = self.make_panel("Physics", self.p_chair, [self.p_member])
        self.chemistry = self.make_panel("Chemistry", self.c_chair, [self.c_member])
        # The call's panel chair completes the panel review.
        self.call.panel_chair = self.p_chair
        self.call.save(update_fields=["panel_chair"])

        self.proposal = self.fixture.proposal_submitted
        self.proposal.slug = "ROUND-001"
        self.proposal.panel = self.physics
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={"cpu_hours": 80000},
        )
        self.other = factories.ProposalFactory(
            round=self.proposal.round,
            state=ProposalStates.IN_REVIEW,
            slug="ROUND-002",
            panel=self.chemistry,
        )

        self.checklist = checklist_factories.ChecklistFactory()
        self.score_question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="=Scientific excellence",
            question_type=QuestionTypes.NUMBER,
            order=1,
        )
        self.choice_question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="Recommendation",
            question_type=QuestionTypes.SINGLE_SELECT,
            order=2,
        )
        self.fund = checklist_models.QuestionOption.objects.create(
            question=self.choice_question, label="Fund", order=1
        )

        self.reports_step = self.add_step(
            "individual-reports",
            StepKinds.ASSESSMENT,
            "Individual reports",
            10,
            blind_review=True,
            checklist=self.checklist,
            contributor_source="call_role:panel_member",
            completer=ResponsibleRoles.CALL_MANAGER,
        )
        self.panel_step = self.add_step(
            "panel-review",
            StepKinds.ASSESSMENT,
            "Panel review",
            11,
            contributor_source="call_role:panel_chair",
            completer=ResponsibleRoles.PANEL_CHAIR,
        )
        self.panel_step.input_steps.set([self.reports_step])
        self.technical_step = self.add_step(
            "technical",
            StepKinds.ASSESSMENT,
            "Technical assessment",
            12,
            per_requested_offering=True,
            contributor_source="requested_offering_team",
            completer=ResponsibleRoles.OFFERING_MANAGER,
        )

        self.reports = self.instance(self.proposal, self.reports_step, "completed")
        self.reports.outcome = "reviewed"
        self.reports.save()
        self.p_report = self.contribute(
            self.reports, self.p_member, comment="Strong case"
        )
        self.answer(self.p_report, self.score_question, 4)
        self.answer(self.p_report, self.choice_question, [self.fund.uuid.hex])
        self.c_report = self.contribute(
            self.reports, self.c_member, comment="Needs work"
        )
        self.panel_review = self.instance(self.proposal, self.panel_step, "active")
        self.panel_review.input_steps.set([self.reports_step])
        self.technical = self.instance(self.proposal, self.technical_step, "completed")
        self.technical.outcome = "feasible"
        self.technical.save()
        self.assessment = factories.StepContributionFactory(
            proposal_step=self.technical,
            requested_offering=self.requested_offering,
            contributor=structure_factories.UserFactory(),
            status=StepContributionStatuses.SUBMITTED,
            submitted_at=timezone.now(),
            outcome=ContributionOutcomes.CONDITIONALLY_ACCEPTED,
            conditions="GPU partition only",
        )

        self.review = self.fixture.review
        self.review.proposal = self.proposal
        self.review.state = models.Review.States.SUBMITTED
        self.review.summary_score = 4
        self.review.save()

    # -- building ------------------------------------------------------------

    def panel_member(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.PANEL_MEMBER)
        return user

    def make_panel(self, name, chair, members):
        panel = models.CallPanel.objects.create(
            call=self.call, name=name, key=name.lower(), chair=chair
        )
        panel.members.set([chair, *members])
        return panel

    def add_step(self, key, kind, name, order, **values):
        models.CallWorkflowStep.make_room(self.call.pk, order)
        return models.CallWorkflowStep.objects.create(
            call=self.call, key=key, kind=kind, name=name, order=order, **values
        )

    def instance(self, proposal, call_step, status_):
        return factories.ProposalWorkflowStepInstanceFactory(
            proposal=proposal,
            call_step=call_step,
            step=call_step.key,
            order=call_step.order,
            status=status_,
            started_at=timezone.now(),
        )

    def contribute(self, instance, user, **values):
        return factories.StepContributionFactory(
            proposal_step=instance,
            contributor=user,
            status=StepContributionStatuses.SUBMITTED,
            submitted_at=timezone.now(),
            **values,
        )

    def answer(self, contribution, question, value):
        completion = contribution.ensure_checklist_completion(self.checklist)
        checklist_models.Answer.objects.create(
            completion=completion,
            question=question,
            user=contribution.contributor,
            answer_data=value,
        )

    # -- requesting ----------------------------------------------------------

    def get(self, user, action, **query):
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_url(self.call, action)
        return self.client.get(url, query)

    def rows(self, user, action, **query):
        response = self.get(user, action, **query)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response)
        return read_csv(response)


class EvaluationExportTest(PanelExportTestMixin, test.APITestCase):
    def test_lists_every_contribution_with_its_answers(self):
        header, *body = self.rows(self.fixture.call_manager, "export-evaluations")
        steps = [row[header.index("Step")] for row in body]
        self.assertEqual(
            sorted(steps),
            [
                "Individual reports",
                "Individual reports",
                "Technical assessment",
            ],
        )
        report = next(
            row for row in body if row[header.index("Comment")] == "Strong case"
        )
        self.assertEqual(report[header.index("Contributor")], self.p_member.full_name)
        self.assertEqual(report[header.index("Proposal ID")], "ROUND-001")
        self.assertEqual(report[header.index("Panel")], "Physics")
        self.assertEqual(
            report[header.index("Individual reports: =Scientific excellence")], "4"
        )
        self.assertEqual(
            report[header.index("Individual reports: Recommendation")], "Fund"
        )

    def test_technical_assessment_carries_outcome_and_conditions(self):
        header, *body = self.rows(self.fixture.call_manager, "export-evaluations")
        row = next(
            row for row in body if row[header.index("Step")] == "Technical assessment"
        )
        self.assertEqual(row[header.index("Offering")], "HPC")
        self.assertEqual(row[header.index("Outcome")], "Conditionally accepted")
        self.assertEqual(row[header.index("Conditions")], "GPU partition only")

    def test_cells_and_headers_are_formula_escaped(self):
        self.p_report.comment = "=HYPERLINK(1)"
        self.p_report.save()
        header, *body = self.rows(self.fixture.call_manager, "export-evaluations")
        self.assertIn("Individual reports: =Scientific excellence", header)
        self.assertIn("'=HYPERLINK(1)", [row[header.index("Comment")] for row in body])

    def test_xlsx_holds_the_same_rows(self):
        csv_rows = self.rows(self.fixture.call_manager, "export-evaluations")
        response = self.get(
            self.fixture.call_manager, "export-evaluations", file_format="xlsx"
        )
        self.assertEqual(response["Content-Type"], exports.XLSX_CONTENT_TYPE)
        (sheet,) = read_xlsx(response).values()
        self.assertEqual(
            [[str(cell) if cell is not None else "" for cell in row] for row in sheet],
            csv_rows,
        )

    def test_step_filter_keeps_the_step_and_its_questions(self):
        header, *body = self.rows(
            self.fixture.call_manager,
            "export-evaluations",
            call_step_uuid=self.reports_step.uuid.hex,
        )
        self.assertEqual(len(body), 2)
        # One step: its questions are headed by their text alone.
        self.assertIn("Recommendation", header)

    def test_panel_filter_keeps_the_panel_proposals(self):
        self.contribute(
            self.instance(self.other, self.reports_step, "active"), self.c_member
        )
        _, *body = self.rows(
            self.fixture.call_manager,
            "export-evaluations",
            panel_uuid=self.chemistry.uuid.hex,
        )
        self.assertEqual({row[0] for row in body}, {"ROUND-002"})

    def test_unknown_panel_is_refused(self):
        other_call_panel = models.CallPanel.objects.create(
            call=factories.CallFactory(), name="Elsewhere", key="elsewhere"
        )
        response = self.get(
            self.fixture.call_manager,
            "export-evaluations",
            panel_uuid=other_call_panel.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_panel_chair_exports_own_panel(self):
        response = self.get(
            self.p_chair, "export-evaluations", panel_uuid=self.physics.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_panel_chair_is_refused_another_panel(self):
        response = self.get(
            self.p_chair, "export-evaluations", panel_uuid=self.chemistry.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_panel_member_must_name_a_panel(self):
        response = self.get(self.p_member, "export-evaluations")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_reviewer_is_refused(self):
        response = self.get(
            self.fixture.reviewer_1,
            "export-evaluations",
            panel_uuid=self.physics.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_blind_step_names_no_contributor_to_the_panel_chair(self):
        # The chair reads the individual reports as the input of the panel
        # review they complete, by their anonymous labels.
        header, *body = self.rows(
            self.p_chair, "export-evaluations", panel_uuid=self.physics.uuid.hex
        )
        reports = [
            row for row in body if row[header.index("Step")] == "Individual reports"
        ]
        self.assertEqual(len(reports), 2)
        names = {row[header.index("Contributor")] for row in reports}
        self.assertEqual(names, {"Contributor 1", "Contributor 2"})
        flat = " ".join(cell for row in body for cell in row)
        self.assertNotIn(self.p_member.full_name, flat)
        self.assertNotIn(self.c_member.full_name, flat)

    def test_panel_member_reads_only_their_own_blind_report(self):
        header, *body = self.rows(
            self.p_member, "export-evaluations", panel_uuid=self.physics.uuid.hex
        )
        comments = [row[header.index("Comment")] for row in body]
        self.assertEqual(comments, ["Strong case"])
        # Their own: named.
        self.assertEqual(body[0][header.index("Contributor")], self.p_member.full_name)

    def test_panel_member_with_a_conflict_gets_no_row(self):
        profile = factories.ReviewerProfileFactory(user=self.p_chair)
        factories.ConflictOfInterestFactory(
            reviewer=profile,
            proposal=self.proposal,
            call=self.call,
            status=COIStatuses.RECUSED,
        )
        _, *body = self.rows(
            self.p_chair, "export-evaluations", panel_uuid=self.physics.uuid.hex
        )
        self.assertEqual(body, [])


class PanelSheetTest(PanelExportTestMixin, test.APITestCase):
    def test_sheet_lists_the_panel_proposals(self):
        header, *body = self.rows(
            self.fixture.call_manager,
            "export-panel-sheet",
            panel_uuid=self.physics.uuid.hex,
        )
        self.assertEqual([row[0] for row in body], ["ROUND-001"])
        (row,) = body
        self.assertEqual(row[header.index("HPC / CPU hours (h)")], "80000")
        self.assertEqual(row[header.index("Average score")], "4.0")
        self.assertEqual(row[header.index("Individual reports")], "Reviewed")
        self.assertEqual(row[header.index("Panel review")], "Active")
        self.assertEqual(row[header.index("HPC")], "Conditionally accepted")
        self.assertEqual(row[header.index("HPC: conditions")], "GPU partition only")

    def test_csv_needs_a_panel(self):
        response = self.get(self.fixture.call_manager, "export-panel-sheet")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_workbook_holds_a_sheet_per_panel(self):
        response = self.get(
            self.fixture.call_manager, "export-panel-sheet", file_format="xlsx"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        sheets = read_xlsx(response)
        self.assertEqual(list(sheets), ["Physics", "Chemistry"])
        self.assertEqual([row[0] for row in sheets["Physics"][1:]], ["ROUND-001"])
        self.assertEqual([row[0] for row in sheets["Chemistry"][1:]], ["ROUND-002"])

    def test_panel_chair_exports_own_panel_and_not_another(self):
        own = self.get(
            self.p_chair, "export-panel-sheet", panel_uuid=self.physics.uuid.hex
        )
        self.assertEqual(own.status_code, status.HTTP_200_OK)
        other = self.get(
            self.p_chair, "export-panel-sheet", panel_uuid=self.chemistry.uuid.hex
        )
        self.assertEqual(other.status_code, status.HTTP_403_FORBIDDEN)
        workbook = self.get(self.p_chair, "export-panel-sheet", file_format="xlsx")
        self.assertEqual(workbook.status_code, status.HTTP_403_FORBIDDEN)

    def test_panel_member_reads_no_more_than_on_screen(self):
        header, *body = self.rows(
            self.p_member, "export-panel-sheet", panel_uuid=self.physics.uuid.hex
        )
        (row,) = body
        # The review scores are the call team's; the technical assessment
        # is the offering team's and the applicant's.
        self.assertEqual(row[header.index("Average score")], "")
        self.assertEqual(row[header.index("HPC")], "")
        self.assertEqual(row[header.index("HPC: conditions")], "")

    def test_panel_member_with_a_conflict_gets_no_row(self):
        profile = factories.ReviewerProfileFactory(user=self.p_member)
        factories.ConflictOfInterestFactory(
            reviewer=profile,
            proposal=self.proposal,
            call=self.call,
            status=COIStatuses.PENDING,
        )
        _, *body = self.rows(
            self.p_member, "export-panel-sheet", panel_uuid=self.physics.uuid.hex
        )
        self.assertEqual(body, [])


class ExportFilterTest(PanelExportTestMixin, test.APITestCase):
    def test_proposal_export_filters_by_panel(self):
        _, *body = self.rows(
            self.fixture.call_manager,
            "export-proposals",
            panel_uuid=self.chemistry.uuid.hex,
        )
        self.assertEqual([row[0] for row in body], ["ROUND-002"])

    def test_proposal_export_filters_by_current_step(self):
        _, *body = self.rows(
            self.fixture.call_manager,
            "export-proposals",
            call_step_uuid=self.panel_step.uuid.hex,
        )
        self.assertEqual([row[0] for row in body], ["ROUND-001"])

    def test_review_export_filters_by_panel(self):
        _, *body = self.rows(
            self.fixture.call_manager,
            "export-reviews",
            panel_uuid=self.chemistry.uuid.hex,
        )
        self.assertEqual(body, [])
        _, *body = self.rows(
            self.fixture.call_manager,
            "export-reviews",
            panel_uuid=self.physics.uuid.hex,
        )
        self.assertEqual([row[0] for row in body], ["ROUND-001"])

    def test_review_export_filters_by_step(self):
        self.review.contribution = None
        attached = self.contribute(self.panel_review, self.p_chair)
        attached.review = self.review
        attached.save()
        _, *body = self.rows(
            self.fixture.call_manager,
            "export-reviews",
            call_step_uuid=self.panel_step.uuid.hex,
        )
        self.assertEqual(len(body), 1)
        _, *body = self.rows(
            self.fixture.call_manager,
            "export-reviews",
            call_step_uuid=self.technical_step.uuid.hex,
        )
        self.assertEqual(body, [])

    def test_panel_members_keep_no_access_to_the_existing_exports(self):
        response = self.get(
            self.p_chair, "export-proposals", panel_uuid=self.physics.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ReviewFindingsTest(PanelExportTestMixin, test.APITestCase):
    """What the export of a panel's chair and members must not tell more of
    than the screens: other reviewers' scores, outcomes held back, the steps
    they cannot read; and the call's managers' own proposals."""

    def attach_review(self, contribution, score):
        review = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=contribution.contributor,
            state=models.Review.States.SUBMITTED,
            summary_score=score,
        )
        contribution.review = review
        contribution.save(update_fields=["review"])
        return review

    def score_by_comment(self, user, comment):
        header, *body = self.rows(
            user, "export-evaluations", panel_uuid=self.physics.uuid.hex
        )
        for row in body:
            if row[header.index("Comment")] == comment:
                return row[header.index("Review score")]
        return None

    def test_chair_does_not_read_another_reviewer_score_on_a_blind_step(self):
        self.attach_review(self.p_report, 1)
        self.assertEqual(self.score_by_comment(self.p_chair, "Strong case"), "")

    def test_chair_does_not_read_another_reviewer_score_on_an_open_step(self):
        self.reports_step.blind_review = False
        self.reports_step.save(update_fields=["blind_review"])
        self.attach_review(self.p_report, 1)
        header, *body = self.rows(
            self.p_chair, "export-evaluations", panel_uuid=self.physics.uuid.hex
        )
        row = next(r for r in body if r[header.index("Comment")] == "Strong case")
        # Named on an open step, so a score beside the name would say whose.
        self.assertEqual(row[header.index("Contributor")], self.p_member.full_name)
        self.assertEqual(row[header.index("Review score")], "")

    def test_member_reads_own_review_score_only(self):
        self.attach_review(self.p_report, 2)
        self.assertEqual(self.score_by_comment(self.p_member, "Strong case"), "2")

    def test_manager_reads_every_review_score(self):
        self.attach_review(self.p_report, 1)
        header, *body = self.rows(self.fixture.call_manager, "export-evaluations")
        row = next(r for r in body if r[header.index("Comment")] == "Strong case")
        self.assertEqual(row[header.index("Review score")], "1")

    def test_manager_on_the_applicant_team_gets_no_row_for_that_proposal(self):
        manager = self.fixture.call_manager
        self.proposal.add_user(manager, ProposalRole.MEMBER)
        _, *sheet = self.rows(
            manager, "export-panel-sheet", panel_uuid=self.physics.uuid.hex
        )
        self.assertEqual(sheet, [])
        _, *evaluations = self.rows(manager, "export-evaluations")
        self.assertNotIn("ROUND-001", {row[0] for row in evaluations})

    def test_contributor_is_named_by_full_name_only(self):
        self.p_member.first_name = ""
        self.p_member.last_name = ""
        self.p_member.save()
        header, *body = self.rows(self.fixture.call_manager, "export-evaluations")
        row = next(r for r in body if r[header.index("Comment")] == "Strong case")
        self.assertEqual(row[header.index("Contributor")], "")
        self.assertNotIn(self.p_member.username, " ".join(row))

    def test_held_decision_reads_as_running_to_a_panel_member(self):
        decision_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="allocation_decision"
        )
        decision = self.instance(self.proposal, decision_step, "completed")
        decision.is_allocation_decision = True
        decision.outcome = WorkflowStepOutcomes.APPROVED
        decision.save()
        self.proposal.decision_held = True
        self.proposal.save(update_fields=["decision_held"])
        header, *body = self.rows(
            self.p_chair, "export-panel-sheet", panel_uuid=self.physics.uuid.hex
        )
        self.assertEqual(body[0][header.index("Allocation decision")], "Active")
        header, *body = self.rows(
            self.fixture.call_manager,
            "export-panel-sheet",
            panel_uuid=self.physics.uuid.hex,
        )
        self.assertEqual(body[0][header.index("Allocation decision")], "Approved")

    def test_member_gets_no_columns_of_steps_they_cannot_read(self):
        notes = checklist_factories.ChecklistFactory()
        checklist_factories.QuestionFactory(
            checklist=notes,
            description="Chair notes",
            question_type=QuestionTypes.TEXT_INPUT,
        )
        self.panel_step.checklist = notes
        self.panel_step.save(update_fields=["checklist"])
        self.contribute(self.panel_review, self.p_chair, comment="Chair's view")
        member_header, *_ = self.rows(
            self.p_member, "export-evaluations", panel_uuid=self.physics.uuid.hex
        )
        self.assertNotIn("Panel review: Chair notes", member_header)
        self.assertIn("Individual reports: Recommendation", member_header)
        manager_header, *_ = self.rows(self.fixture.call_manager, "export-evaluations")
        self.assertIn("Panel review: Chair notes", manager_header)

    def test_two_entries_of_one_offering_have_columns_of_their_own(self):
        second = factories.RequestedOfferingFactory(
            call=self.call,
            offering=self.offering,
            state=RequestedOfferingStates.ACCEPTED,
        )
        factories.StepContributionFactory(
            proposal_step=self.technical,
            requested_offering=second,
            contributor=structure_factories.UserFactory(),
            status=StepContributionStatuses.SUBMITTED,
            outcome=ContributionOutcomes.REJECTED,
        )
        header, *body = self.rows(
            self.fixture.call_manager,
            "export-panel-sheet",
            panel_uuid=self.physics.uuid.hex,
        )
        self.assertEqual(len(header), len(set(header)))
        self.assertEqual(body[0][header.index("HPC")], "Conditionally accepted")
        self.assertEqual(body[0][header.index("HPC (2)")], "Rejected")

    def test_repeated_question_text_is_told_apart(self):
        checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="Recommendation",
            question_type=QuestionTypes.TEXT_INPUT,
            order=3,
        )
        header, *_ = self.rows(self.fixture.call_manager, "export-evaluations")
        self.assertEqual(len(header), len(set(header)))
        self.assertIn("Individual reports: Recommendation (2)", header)


class PanelExportQueryCountTest(PanelExportTestMixin, test.APITestCase):
    """A panel member's export reads each proposal's visibility apart, but
    its cost per proposal stays bounded."""

    # Queries per extra proposal, each with a blind report, a panel review
    # and a technical assessment. Measured: 1 for the sheet, whose rules are
    # asked once per call; 11 for the evaluations, where Access asks of each
    # step instance who completes, contributes to and reads it.
    PER_PROPOSAL = {"export-panel-sheet": 3, "export-evaluations": 15}

    def add_proposals(self, count):
        for index in range(count):
            proposal = factories.ProposalFactory(
                round=self.proposal.round,
                state=ProposalStates.IN_REVIEW,
                slug=f"ROUND-1{index:02d}",
                panel=self.physics,
            )
            reports = self.instance(proposal, self.reports_step, "completed")
            self.contribute(reports, self.p_member, comment="Report")
            review = self.instance(proposal, self.panel_step, "active")
            review.input_steps.set([self.reports_step])
            technical = self.instance(proposal, self.technical_step, "completed")
            factories.StepContributionFactory(
                proposal_step=technical,
                requested_offering=self.requested_offering,
                contributor=structure_factories.UserFactory(),
                status=StepContributionStatuses.SUBMITTED,
                outcome=ContributionOutcomes.ACCEPTED,
            )

    def count(self, action):
        with CaptureQueriesContext(connection) as context:
            response = self.get(self.p_chair, action, panel_uuid=self.physics.uuid.hex)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            read_csv(response)
        return len(context.captured_queries)

    def assert_bounded(self, action):
        self.add_proposals(2)
        few = self.count(action)
        self.add_proposals(6)
        many = self.count(action)
        self.assertLessEqual(many - few, 6 * self.PER_PROPOSAL[action], (few, many))

    def test_panel_sheet(self):
        self.assert_bounded("export-panel-sheet")

    def test_evaluations(self):
        self.assert_bounded("export-evaluations")


class SheetTitleTest(test.APISimpleTestCase):
    def test_reserved_history_title_is_avoided(self):
        self.assertEqual(
            exports.xlsx_sheet_titles(["History", "history", "Physics"]),
            ["History (2)", "history (3)", "Physics"],
        )

    def test_a_cut_title_never_ends_with_an_apostrophe(self):
        name = "A" * 30 + "'s panel"
        titles = exports.xlsx_sheet_titles([name, name])
        self.assertEqual(titles, ["A" * 30, "A" * 27 + " (2)"])
        for title in titles:
            self.assertLessEqual(len(title), 31)
            self.assertFalse(title.endswith("'") or title.startswith("'"))
