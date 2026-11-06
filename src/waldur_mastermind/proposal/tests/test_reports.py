import datetime
import importlib

from django.apps import apps as django_apps
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import activity, models
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    CallStates,
    ProposalStates,
    ResultsPublication,
    ReviewRejectionCauses,
    RoundLifecycleStates,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_resource_budgets import BudgetTestBase

ReviewStates = models.Review.States


class ReportTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.organisation = self.fixture.manager
        self.call = self.fixture.call
        self.other_call = self.fixture.new_call
        self.round = self.fixture.round

    def organisation_url(self, kind):
        return factories.CallManagingOrganisationFactory.get_url(
            self.organisation, action=f"reports/{kind}"
        )

    def call_url(self, kind, call=None):
        return factories.CallFactory.get_protected_url(
            call or self.call, action=f"reports/{kind}"
        )

    def get(self, url, user=None, **params):
        self.client.force_authenticate(user or self.fixture.staff)
        return self.client.get(url, params)

    def report(self, kind, user=None, **params):
        response = self.get(self.organisation_url(kind), user, **params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data


class ReportScopeTest(ReportTestBase):
    def called(self, user, **params):
        data = self.report("portfolio", user, **params)
        return {row["call_uuid"] for row in data["calls"]}

    def test_a_call_manager_sees_only_their_calls(self):
        self.assertEqual(self.called(self.fixture.call_manager), {self.call.uuid.hex})

    def test_an_organiser_sees_every_call_of_the_organisation(self):
        self.assertEqual(
            self.called(self.fixture.call_organizer_user),
            {self.call.uuid.hex, self.other_call.uuid.hex},
        )

    def test_support_reads_every_call(self):
        self.assertEqual(
            self.called(self.fixture.global_support),
            {self.call.uuid.hex, self.other_call.uuid.hex},
        )

    def test_a_call_filter_narrows_the_scope(self):
        self.assertEqual(
            self.called(self.fixture.staff, call_uuid=self.other_call.uuid.hex),
            {self.other_call.uuid.hex},
        )

    def test_a_call_outside_the_scope_is_not_found(self):
        foreign = factories.CallFactory()
        response = self.get(
            self.organisation_url("portfolio"),
            self.fixture.staff,
            call_uuid=foreign.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Nor a call of the organisation the call manager does not manage.
        response = self.get(
            self.organisation_url("portfolio"),
            self.fixture.call_manager,
            call_uuid=self.other_call.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_round_of_another_call_is_not_found(self):
        foreign_round = factories.RoundFactory(call=factories.CallFactory())
        response = self.get(
            self.organisation_url("portfolio"),
            round_uuid=foreign_round.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_outsider_is_refused(self):
        response = self.get(self.organisation_url("portfolio"), self.fixture.user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.get(self.call_url("portfolio"), self.fixture.user)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_a_reviewer_of_the_call_is_refused(self):
        response = self.get(self.call_url("portfolio"), self.fixture.reviewer_1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_call_report_covers_the_call_alone(self):
        response = self.get(self.call_url("portfolio"), self.fixture.call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [row["call_uuid"] for row in response.data["calls"]], [self.call.uuid.hex]
        )

    def test_dates_in_the_wrong_order_are_refused(self):
        response = self.get(
            self.organisation_url("portfolio"),
            submitted_from="2026-02-01",
            submitted_to="2026-01-01",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PortfolioReportTest(ReportTestBase):
    def add_proposal(self, state, call_round=None, submitted_at=None):
        if submitted_at is None and state != ProposalStates.DRAFT:
            submitted_at = timezone.now()
        return factories.ProposalFactory(
            round=call_round or self.round, state=state, submitted_at=submitted_at
        )

    def test_outcomes_and_success_rate(self):
        # The fixture holds a draft and a submitted proposal.
        models.Proposal.objects.filter(pk=self.fixture.proposal_submitted.pk).update(
            submitted_at=timezone.now()
        )
        accepted = self.add_proposal(ProposalStates.ACCEPTED)
        self.add_proposal(ProposalStates.ACCEPTED)
        self.add_proposal(ProposalStates.REJECTED)
        self.add_proposal(ProposalStates.IN_REVIEW)
        self.add_proposal(ProposalStates.CANCELED)
        factories.ReviewFactory(
            proposal=accepted, state=ReviewStates.SUBMITTED, summary_score=4
        )
        factories.ReviewFactory(
            proposal=accepted, state=ReviewStates.SUBMITTED, summary_score=2
        )
        # Unscored and unsubmitted reviews give no score.
        factories.ReviewFactory(
            proposal=accepted, state=ReviewStates.SUBMITTED, summary_score=0
        )
        factories.ReviewFactory(
            proposal=accepted, state=ReviewStates.IN_REVIEW, summary_score=5
        )

        data = self.report("portfolio", call_uuid=self.call.uuid.hex)
        call = data["calls"][0]
        self.assertEqual(call["proposals"], 7)
        self.assertEqual(call["submitted"], 6)
        self.assertEqual(call["draft"], 1)
        self.assertEqual(call["awaiting_review"], 1)
        self.assertEqual(call["in_review"], 1)
        self.assertEqual(call["accepted"], 2)
        self.assertEqual(call["rejected"], 1)
        self.assertEqual(call["canceled"], 1)
        self.assertEqual(call["decided"], 3)
        self.assertAlmostEqual(call["success_rate"], 0.667)
        self.assertEqual(call["scored_reviews"], 2)
        self.assertEqual(call["mean_score"], 3.0)
        round_row = next(
            row for row in call["rounds"] if row["round_uuid"] == self.round.uuid.hex
        )
        self.assertEqual(round_row["accepted"], 2)
        self.assertEqual(data["totals"]["decided"], 3)
        self.assertEqual(sum(month["submitted"] for month in data["months"]), 6)

    def test_active_rounds_are_open_rounds_of_active_calls(self):
        now = timezone.now()
        open_window = {
            "start_time": now - datetime.timedelta(days=1),
            "cutoff_time": now + datetime.timedelta(days=1),
        }
        models.Round.objects.filter(call=self.call).update(**open_window)
        archived = factories.CallFactory(
            manager=self.organisation, state=CallStates.ARCHIVED
        )
        factories.RoundFactory(call=archived, **open_window)

        open_rounds = models.Round.objects.filter(call=self.call).count()

        data = self.report("portfolio")
        by_call = {row["call_uuid"]: row for row in data["calls"]}
        self.assertEqual(by_call[self.call.uuid.hex]["active_rounds"], open_rounds)
        self.assertEqual(by_call[archived.uuid.hex]["active_rounds"], 0)
        self.assertEqual(by_call[archived.uuid.hex]["rounds"][0]["active_rounds"], 0)
        self.assertEqual(data["totals"]["active_rounds"], open_rounds)

    def test_submission_dates_narrow_the_proposals(self):
        long_ago = timezone.now() - datetime.timedelta(days=100)
        self.add_proposal(ProposalStates.ACCEPTED, submitted_at=long_ago)
        self.add_proposal(ProposalStates.REJECTED)
        data = self.report(
            "portfolio",
            submitted_from=(long_ago - datetime.timedelta(days=1)).date().isoformat(),
            submitted_to=(long_ago + datetime.timedelta(days=1)).date().isoformat(),
        )
        self.assertEqual(data["totals"]["proposals"], 1)
        self.assertEqual(data["totals"]["accepted"], 1)
        self.assertEqual(data["totals"]["draft"], 0)

    def test_queries_do_not_grow_with_the_calls(self):
        def queries():
            with CaptureQueriesContext(connection) as context:
                self.report("portfolio")
            return len(context)

        queries()  # Warms the role and content type caches.
        before = queries()
        for _ in range(3):
            call = factories.CallFactory(manager=self.organisation)
            call_round = factories.RoundFactory(call=call)
            proposal = self.add_proposal(ProposalStates.ACCEPTED, call_round)
            factories.ReviewFactory(
                proposal=proposal, state=ReviewStates.SUBMITTED, summary_score=3
            )
        self.assertEqual(queries(), before)


class WorkflowReportTest(ReportTestBase):
    def add_instance(self, call, status, deadline=None, **kwargs):
        call_round = factories.RoundFactory(call=call)
        proposal = factories.ProposalFactory(
            round=call_round, state=ProposalStates.IN_REVIEW
        )
        return factories.ProposalWorkflowStepInstanceFactory(
            proposal=proposal,
            step="technical_assessment",
            status=status,
            deadline=deadline,
            **kwargs,
        )

    def test_overdue_and_late_steps_are_summed_over_the_calls(self):
        now = timezone.now()
        past, future = now - datetime.timedelta(days=1), now + datetime.timedelta(1)
        Statuses = WorkflowStepInstanceStatuses
        self.add_instance(self.call, Statuses.ACTIVE, past)
        self.add_instance(self.call, Statuses.ACTIVE, future)
        self.add_instance(self.other_call, Statuses.ACTIVE, past)
        self.add_instance(
            self.other_call,
            Statuses.COMPLETED,
            past,
            started_at=now - datetime.timedelta(days=4),
            completed_at=now,
        )

        data = self.report("workflow")
        step = next(s for s in data["steps"] if s["key"] == "technical_assessment")
        self.assertEqual(step["waiting"], 3)
        self.assertEqual(step["overdue"], 2)
        self.assertEqual(step["completed_late"], 1)
        self.assertEqual(step["median_days"], 4.0)
        by_call = {row["call_uuid"]: row for row in step["calls"]}
        self.assertEqual(by_call[self.call.uuid.hex]["overdue"], 1)
        self.assertEqual(by_call[self.other_call.uuid.hex]["completed"], 1)

        data = self.get(self.call_url("workflow")).data
        step = next(s for s in data["steps"] if s["key"] == "technical_assessment")
        self.assertEqual(step["overdue"], 1)


class WorkflowOrderTest(ReportTestBase):
    def test_steps_are_ordered_by_phase_then_place_within_it(self):
        Phases = WorkflowPhases
        for call, steps in (
            (
                self.call,
                [
                    ("assess", Phases.EVALUATION),
                    ("final_word", Phases.CLOSING),
                    ("decide", Phases.EVALUATION),
                ],
            ),
            (
                self.other_call,
                [
                    ("prolong", Phases.EXTENSION),
                    ("assess", Phases.EVALUATION),
                    ("decide", Phases.EVALUATION),
                ],
            ),
        ):
            models.CallWorkflowStep.objects.filter(call=call).delete()
            for order, (key, phase) in enumerate(steps, start=1):
                factories.CallWorkflowStepFactory(
                    call=call, key=key, name=key, phase=phase, order=order
                )

        data = self.report("workflow")
        self.assertEqual(
            [step["key"] for step in data["steps"]],
            ["assess", "decide", "prolong", "final_word"],
        )


class ReviewerReportTest(ReportTestBase):
    def setUp(self):
        super().setUp()
        self.reviewer = self.fixture.reviewer_1
        self.proposal = self.fixture.proposal_submitted
        # The fixture's review of the submitted proposal: nothing entered.
        self.not_started = self.fixture.review
        profile = factories.ReviewerProfileFactory(user=self.reviewer)
        pool_entry = factories.CallReviewerPoolFactory(call=self.call, reviewer=profile)
        self.sent_at = timezone.now() - datetime.timedelta(days=10)
        self.batch = factories.AssignmentBatchFactory(
            call=self.call, reviewer_pool_entry=pool_entry, sent_at=self.sent_at
        )

    def add_review(self, **kwargs):
        proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.IN_REVIEW
        )
        return factories.ReviewFactory(
            proposal=proposal, reviewer=self.reviewer, **kwargs
        )

    def row(self, data):
        return next(
            row
            for row in data["reviewers"]
            if row["reviewer_uuid"] == self.reviewer.uuid.hex
        )

    def test_reviewer_figures(self):
        now = timezone.now()
        submitted = self.add_review(state=ReviewStates.SUBMITTED, summary_score=4)
        models.Review.objects.filter(pk=submitted.pk).update(
            created=now - datetime.timedelta(days=6), submitted_at=now
        )
        # Submitted before the time was recorded: no turnaround.
        self.add_review(state=ReviewStates.SUBMITTED, summary_score=2)
        self.add_review(summary_public_comment="Halfway through")
        self.add_review(criteria_scores={"x": 3})
        self.add_review(
            state=ReviewStates.REJECTED,
            rejection_cause=ReviewRejectionCauses.DECLINED,
        )
        self.add_review(
            state=ReviewStates.REJECTED,
            rejection_cause=ReviewRejectionCauses.WITHDRAWN,
        )
        factories.AssignmentItemFactory(
            batch=self.batch,
            proposal=self.proposal,
            status=AssignmentItemStatuses.DECLINED,
            responded_at=self.sent_at + datetime.timedelta(days=2),
        )
        factories.AssignmentItemFactory(
            batch=self.batch,
            proposal=self.fixture.proposal,
            status=AssignmentItemStatuses.EXPIRED,
        )
        # Never sent: no invitation.
        factories.AssignmentItemFactory(
            batch=factories.AssignmentBatchFactory(
                call=self.call, reviewer_pool_entry=self.batch.reviewer_pool_entry
            ),
            proposal=self.proposal,
        )

        row = self.row(self.report("reviewers"))
        self.assertEqual(row["reviews_assigned"], 7)
        self.assertEqual(row["reviews_submitted"], 2)
        self.assertEqual(row["reviews_in_progress"], 2)
        self.assertEqual(row["reviews_not_started"], 1)
        self.assertEqual(row["reviews_declined"], 1)
        self.assertEqual(row["reviews_withdrawn"], 1)
        self.assertEqual(row["median_turnaround_days"], 6.0)
        self.assertEqual(row["mean_score"], 3.0)
        self.assertEqual(row["invitations"], 2)
        self.assertEqual(row["invitations_declined"], 1)
        self.assertEqual(row["invitations_expired"], 1)
        self.assertEqual(row["median_response_days"], 2.0)

    def test_every_sent_invitation_lands_in_one_bucket(self):
        Statuses = AssignmentItemStatuses
        now = timezone.now()

        def item(batch, status, **kwargs):
            return factories.AssignmentItemFactory(
                batch=batch,
                proposal=factories.ProposalFactory(
                    round=self.round, state=ProposalStates.IN_REVIEW
                ),
                status=status,
                **kwargs,
            )

        pool_entry = self.batch.reviewer_pool_entry
        for item_status in (
            Statuses.ACCEPTED,
            Statuses.DECLINED,
            Statuses.EXPIRED,
            Statuses.COI_BLOCKED,
            Statuses.REASSIGNED,
            Statuses.PENDING,
        ):
            item(self.batch, item_status)
        overdue = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=pool_entry,
            sent_at=self.sent_at,
            expires_at=now - datetime.timedelta(days=1),
        )
        item(overdue, Statuses.PENDING)
        cancelled = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=pool_entry,
            sent_at=self.sent_at,
            status=AssignmentBatchStatuses.CANCELLED,
        )
        item(cancelled, Statuses.PENDING)
        # Never sent: not an invitation yet.
        item(
            factories.AssignmentBatchFactory(
                call=self.call, reviewer_pool_entry=pool_entry
            ),
            Statuses.PENDING,
        )
        # Invited by email, with no account yet.
        by_email = factories.CallReviewerPoolFactory(
            call=self.call, reviewer=None, invited_email="new@example.com"
        )
        item(
            factories.AssignmentBatchFactory(
                call=self.call, reviewer_pool_entry=by_email, sent_at=self.sent_at
            ),
            Statuses.EXPIRED,
        )

        data = self.report("reviewers")
        parts = [
            "invitations_accepted",
            "invitations_declined",
            "invitations_expired",
            "invitations_pending",
            "invitations_withdrawn",
            "invitations_coi_blocked",
            "invitations_reassigned",
        ]
        for row in [data["totals"], *data["reviewers"]]:
            self.assertEqual(sum(row[part] for part in parts), row["invitations"])
        row = self.row(data)
        self.assertEqual(row["invitations"], 8)
        self.assertEqual(row["invitations_expired"], 2)
        self.assertEqual(row["invitations_pending"], 1)
        self.assertEqual(row["invitations_withdrawn"], 1)
        self.assertEqual(row["invitations_coi_blocked"], 1)
        self.assertEqual(row["invitations_reassigned"], 1)
        invitee = next(row for row in data["reviewers"] if row["reviewer_uuid"] is None)
        self.assertEqual(invitee["reviewer_email"], "new@example.com")
        self.assertEqual(invitee["invitations_expired"], 1)
        self.assertIsNone(invitee["is_active"])
        self.assertEqual(data["totals"]["invitations"], 9)

    def test_a_deactivated_reviewer_is_listed(self):
        self.reviewer.is_active = False
        self.reviewer.save()
        row = self.row(self.report("reviewers"))
        self.assertFalse(row["is_active"])
        self.assertEqual(row["reviews_not_started"], 1)

    def test_reviews_of_calls_out_of_scope_are_not_counted(self):
        foreign = factories.ProposalFactory(round=factories.RoundFactory())
        factories.ReviewFactory(proposal=foreign, reviewer=self.reviewer)
        row = self.row(self.report("reviewers"))
        self.assertEqual(row["reviews_assigned"], 1)

    def test_queries_do_not_grow_with_the_reviewers(self):
        def queries():
            with CaptureQueriesContext(connection) as context:
                self.report("reviewers")
            return len(context)

        queries()  # Warms the role and content type caches.
        before = queries()
        for _ in range(3):
            factories.ReviewFactory(
                proposal=factories.ProposalFactory(
                    round=factories.RoundFactory(
                        call=factories.CallFactory(manager=self.organisation)
                    )
                ),
                state=ReviewStates.SUBMITTED,
                summary_score=3,
            )
        self.assertEqual(queries(), before)


class RoundReportTest(ReportTestBase):
    def test_day_spans_and_lateness(self):
        cutoff = timezone.now() - datetime.timedelta(days=30)
        models.Round.objects.filter(pk=self.round.pk).update(
            cutoff_time=cutoff,
            evaluation_started_at=cutoff + datetime.timedelta(days=1),
            deciding_started_at=cutoff + datetime.timedelta(days=11),
            results_published_at=cutoff + datetime.timedelta(days=20),
            allocation_date=cutoff + datetime.timedelta(days=15),
            adopted_at=(cutoff + datetime.timedelta(days=22)).date(),
        )
        unpublished = factories.RoundFactory(
            call=self.call,
            start_time=cutoff - datetime.timedelta(days=10),
            cutoff_time=cutoff,
            allocation_date=timezone.now() - datetime.timedelta(days=3),
        )

        data = self.report("rounds", call_uuid=self.call.uuid.hex)
        rows = {row["round_uuid"]: row for row in data["rounds"]}
        row = rows[self.round.uuid.hex]
        self.assertEqual(row["days_cutoff_to_evaluation"], 1.0)
        self.assertEqual(row["days_evaluating"], 10.0)
        self.assertEqual(row["days_deciding"], 9.0)
        self.assertEqual(row["days_cutoff_to_published"], 20.0)
        self.assertEqual(row["days_published_to_adopted"], 2.0)
        self.assertEqual(row["days_after_allocation_date"], 5.0)
        self.assertTrue(row["late"])
        late = rows[unpublished.uuid.hex]
        self.assertIsNone(late["days_cutoff_to_published"])
        self.assertEqual(late["days_after_allocation_date"], 3.0)
        self.assertTrue(late["late"])
        self.assertEqual(data["totals"]["late"], 2)
        self.assertEqual(data["totals"]["median_days_cutoff_to_published"], 20.0)


class RoundStageTest(ReportTestBase):
    def rows(self):
        data = self.report("rounds", call_uuid=self.call.uuid.hex)
        return {row["round_uuid"]: row for row in data["rounds"]}

    def test_an_unfinished_round_says_how_long_it_has_been_at_its_stage(self):
        now = timezone.now()
        models.Round.objects.filter(pk=self.round.pk).update(
            cutoff_time=now - datetime.timedelta(days=9),
            lifecycle_state=RoundLifecycleStates.EVALUATING,
            evaluation_started_at=now - datetime.timedelta(days=4),
        )
        waiting = factories.RoundFactory(
            call=self.call,
            start_time=now - datetime.timedelta(days=20),
            cutoff_time=now - datetime.timedelta(days=2),
        )
        closed = factories.RoundFactory(
            call=self.call,
            start_time=now - datetime.timedelta(days=90),
            cutoff_time=now - datetime.timedelta(days=60),
        )
        models.Round.objects.filter(pk=closed.pk).update(
            lifecycle_state=RoundLifecycleStates.CLOSED
        )

        rows = self.rows()
        evaluating = rows[self.round.uuid.hex]
        self.assertEqual(evaluating["current_stage"], "evaluating")
        self.assertEqual(evaluating["days_in_current_stage"], 4.0)
        # Completed spans stay empty until the stage ends.
        self.assertIsNone(evaluating["days_evaluating"])
        self.assertEqual(rows[waiting.uuid.hex]["current_stage"], "awaiting_evaluation")
        self.assertEqual(rows[waiting.uuid.hex]["days_in_current_stage"], 2.0)
        self.assertIsNone(rows[closed.uuid.hex]["current_stage"])
        self.assertIsNone(rows[closed.uuid.hex]["days_in_current_stage"])

    def test_no_round_of_an_archived_call_is_open(self):
        now = timezone.now()
        archived = factories.CallFactory(
            manager=self.organisation, state=CallStates.ARCHIVED
        )
        stopped = factories.RoundFactory(
            call=archived,
            start_time=now - datetime.timedelta(days=5),
            cutoff_time=now + datetime.timedelta(days=5),
            allocation_date=now - datetime.timedelta(days=1),
        )
        data = self.report("rounds", call_uuid=archived.uuid.hex)
        [row] = data["rounds"]
        self.assertEqual(row["round_uuid"], stopped.uuid.hex)
        self.assertEqual(row["status"], "call_archived")
        self.assertEqual(row["current_stage"], "call_archived")
        self.assertIsNone(row["days_in_current_stage"])
        self.assertFalse(row["late"])
        self.assertEqual(row["call_state"], CallStates.ARCHIVED)

        draft = factories.CallFactory(manager=self.organisation, state=CallStates.DRAFT)
        factories.RoundFactory(
            call=draft,
            start_time=now - datetime.timedelta(days=5),
            cutoff_time=now + datetime.timedelta(days=5),
            allocation_date=now - datetime.timedelta(days=1),
        )
        [row] = self.report("rounds", call_uuid=draft.uuid.hex)["rounds"]
        self.assertEqual(row["status"], "call_draft")
        self.assertEqual(row["current_stage"], "call_draft")
        self.assertIsNone(row["days_in_current_stage"])
        self.assertFalse(row["late"])
        portfolio = self.report("portfolio", call_uuid=draft.uuid.hex)
        self.assertEqual(portfolio["totals"]["active_rounds"], 0)

    def test_submission_dates_narrow_the_rounds_counts(self):
        now = timezone.now()
        long_ago = now - datetime.timedelta(days=100)
        models.Round.objects.filter(pk=self.round.pk).update(
            start_time=long_ago - datetime.timedelta(days=10), cutoff_time=None
        )
        for submitted_at in (long_ago, now):
            factories.ProposalFactory(
                round=self.round,
                state=ProposalStates.SUBMITTED,
                submitted_at=submitted_at,
            )
        dates = {
            "submitted_from": (long_ago - datetime.timedelta(days=1)).date(),
            "submitted_to": (long_ago + datetime.timedelta(days=1)).date(),
        }
        rounds = self.report("rounds", call_uuid=self.call.uuid.hex, **dates)
        [row] = [
            row for row in rounds["rounds"] if row["round_uuid"] == self.round.uuid.hex
        ]
        portfolio = self.report("portfolio", call_uuid=self.call.uuid.hex, **dates)
        self.assertEqual(row["proposals_submitted"], 1)
        self.assertEqual(portfolio["totals"]["submitted"], 1)

    def test_a_round_closed_without_publishing_is_not_late(self):
        # As the lifecycle migration left rounds from before it was recorded.
        now = timezone.now()
        models.Round.objects.filter(pk=self.round.pk).update(
            cutoff_time=now - datetime.timedelta(days=60),
            lifecycle_state=RoundLifecycleStates.CLOSED,
            results_published_at=None,
            allocation_date=now - datetime.timedelta(days=30),
        )
        row = self.rows()[self.round.uuid.hex]
        self.assertIsNone(row["days_after_allocation_date"])
        self.assertFalse(row["late"])
        self.assertIsNone(row["current_stage"])
        self.assertIsNone(row["days_in_current_stage"])

    def test_adoption_and_allocation_are_counted_in_whole_days(self):
        published = timezone.now().replace(hour=15, minute=0) - datetime.timedelta(
            days=10
        )
        models.Round.objects.filter(pk=self.round.pk).update(
            cutoff_time=published - datetime.timedelta(days=30),
            lifecycle_state=RoundLifecycleStates.CLOSED,
            results_published_at=published,
            # A call that requires adoption publishes only adopted results.
            adopted_at=(published - datetime.timedelta(days=1)).date(),
            # Earlier the same day: not late.
            allocation_date=published.replace(hour=9),
        )
        row = self.rows()[self.round.uuid.hex]
        self.assertEqual(row["days_published_to_adopted"], -1.0)
        self.assertEqual(row["days_after_allocation_date"], 0)
        self.assertFalse(row["late"])


class BudgetReportTest(BudgetTestBase):
    def test_awarded_comes_from_the_award_not_the_request(self):
        self.budget(offered_total=1000)
        accepted = self.make_decidable(60)
        models.Proposal.objects.filter(pk=accepted.pk).update(
            state=ProposalStates.ACCEPTED
        )
        factories.RequestedResourceFactory(
            proposal=accepted,
            requested_offering=self.requested_offering,
            limits={"cpu": 80},
        )
        waiting = factories.ProposalFactory(
            round=self.round,
            state=ProposalStates.SUBMITTED,
            submitted_at=timezone.now(),
        )
        factories.RequestedResourceFactory(
            proposal=waiting,
            requested_offering=self.requested_offering,
            limits={"cpu": 25},
        )

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            factories.CallFactory.get_protected_url(self.call, action="reports/budgets")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        call_wide = next(
            row for row in response.data["pools"] if row["round_uuid"] is None
        )
        self.assertIsNotNone(call_wide["pool_uuid"])
        self.assertEqual(call_wide["offered_total"], 1000)
        self.assertEqual(call_wide["awarded"], 60)
        self.assertEqual(call_wide["requested"], 105)
        self.assertEqual(call_wide["undecided"], 25)
        self.assertEqual(call_wide["remaining"], 940)
        self.assertEqual(call_wide["awarded_share"], 0.06)
        self.assertEqual(response.data["totals"]["pools"], 1)

    def test_a_call_without_a_call_wide_pool_lists_its_rounds_alone(self):
        earlier = self.other_round()
        for call_round, amount in ((self.round, 60), (earlier, 30)):
            proposal = self.make_decidable(amount, call_round=call_round)
            models.Proposal.objects.filter(pk=proposal.pk).update(
                state=ProposalStates.ACCEPTED
            )

        rows = self.pools()
        self.assertTrue(all(row["round_uuid"] is not None for row in rows))
        self.assertEqual(sorted(row["awarded"] for row in rows), [30, 60])
        self.assertFalse(any(row["within_call_pool"] for row in rows))

    def test_round_pools_within_a_call_wide_pool_are_marked(self):
        earlier = self.other_round()
        self.budget(offered_total=1000)
        self.budget(offered_total=100, call_round=self.round)
        for call_round, amount in ((self.round, 60), (earlier, 30)):
            proposal = self.make_decidable(amount, call_round=call_round)
            models.Proposal.objects.filter(pk=proposal.pk).update(
                state=ProposalStates.ACCEPTED
            )

        rows = self.pools()
        # The call-wide pool, and the round's own pool within it; the
        # earlier round has no pool of its own and draws on the call's.
        self.assertEqual(len(rows), 2)
        call_wide = next(row for row in rows if row["round_uuid"] is None)
        in_round = next(row for row in rows if row["round_uuid"] is not None)
        self.assertEqual(call_wide["awarded"], 90)
        self.assertFalse(call_wide["within_call_pool"])
        self.assertEqual(in_round["awarded"], 60)
        self.assertTrue(in_round["within_call_pool"])

    def pools(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            factories.CallFactory.get_protected_url(self.call, action="reports/budgets")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data["pools"]

    def test_a_round_filter_keeps_the_call_wide_pool(self):
        earlier = self.other_round()
        self.budget(offered_total=1000)
        self.budget(offered_total=100, call_round=self.round)
        for call_round, amount in ((self.round, 60), (earlier, 30)):
            proposal = self.make_decidable(amount, call_round=call_round)
            models.Proposal.objects.filter(pk=proposal.pk).update(
                state=ProposalStates.ACCEPTED
            )

        for call_round, expected in ((self.round, 2), (earlier, 1)):
            self.client.force_authenticate(self.call_manager)
            response = self.client.get(
                factories.CallFactory.get_protected_url(
                    self.call, action="reports/budgets"
                ),
                {"round_uuid": call_round.uuid.hex},
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            rows = response.data["pools"]
            self.assertEqual(len(rows), expected)
            call_wide = next(row for row in rows if row["round_uuid"] is None)
            # The whole call's pool, with the whole call's figures.
            self.assertEqual(call_wide["offered_total"], 1000)
            self.assertEqual(call_wide["awarded"], 90)
            self.assertEqual(call_wide["remaining"], 910)
            for row in rows:
                if row["round_uuid"] is not None:
                    self.assertEqual(row["round_uuid"], call_round.uuid.hex)
                    self.assertTrue(row["within_call_pool"])

    def test_queries_do_not_grow_with_the_calls(self):
        organisation = self.call.manager

        def queries():
            self.client.force_authenticate(self.staff)
            url = factories.CallManagingOrganisationFactory.get_url(
                organisation, action="reports/budgets"
            )
            with CaptureQueriesContext(connection) as context:
                response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            return len(context)

        def add_call():
            call = factories.CallFactory(manager=organisation)
            call_round = factories.RoundFactory(call=call)
            offering = factories.RequestedOfferingFactory(
                call=call, offering=self.offering
            )
            marketplace_factories.PlanComponentFactory(
                plan=offering.plan, component=self.component
            )
            models.CallResourcePool.objects.create(
                offering_component=models.CallOfferingComponent.objects.create(
                    requested_offering=offering, component_type="cpu"
                ),
                offered_total=100,
            )
            proposal = factories.ProposalFactory(
                round=call_round, state=ProposalStates.SUBMITTED
            )
            factories.RequestedResourceFactory(
                proposal=proposal, requested_offering=offering, limits={"cpu": 5}
            )

        self.budget(offered_total=1000)
        waiting = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        factories.RequestedResourceFactory(
            proposal=waiting,
            requested_offering=self.requested_offering,
            limits={"cpu": 5},
        )
        queries()  # Warms the role and content type caches.
        before = queries()
        for _ in range(2):
            add_call()
        self.assertEqual(queries(), before)

    def held_decision(self, outcome):
        """A proposal asking for 40 whose allocation decision, ``outcome``,
        is held for the round's publication; an approval awards 60."""
        proposal = self.make_decidable(60)
        factories.RequestedResourceFactory(
            proposal=proposal,
            requested_offering=self.requested_offering,
            limits={"cpu": 40},
        )
        models.ProposalWorkflowStepInstance.objects.filter(
            pk=self.decision(proposal).pk
        ).update(
            status=WorkflowStepInstanceStatuses.COMPLETED,
            outcome=outcome,
            is_allocation_decision=True,
        )
        models.Proposal.objects.filter(pk=proposal.pk).update(decision_held=True)
        return proposal

    def call_wide(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.CallFactory.get_protected_url(self.call, action="reports/budgets")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return next(row for row in response.data["pools"] if row["round_uuid"] is None)

    def test_held_decisions_are_counted_as_their_reader_may_know_them(self):
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save(update_fields=["publish_results"])
        self.budget(offered_total=1000)
        approved = self.held_decision(WorkflowStepOutcomes.APPROVED)
        rejected = self.held_decision(WorkflowStepOutcomes.REJECTED)

        # The call team learns held decisions: the rejection is decided.
        row = self.call_wide(self.call_manager)
        self.assertEqual(row["awarded"], 60)
        self.assertEqual(row["undecided"], 0)

        # A call manager on both proposals' teams may not learn them: the
        # approval and the rejection read alike, both undecided.
        applicant_manager = structure_factories.UserFactory()
        self.call.add_user(applicant_manager, CallRole.MANAGER)
        for proposal in (approved, rejected):
            proposal.add_user(applicant_manager, ProposalRole.MANAGER)
        row = self.call_wide(applicant_manager)
        self.assertEqual(row["awarded"], 0)
        self.assertEqual(row["undecided"], 80)


class BackfillReviewSubmittedAtTest(ReportTestBase):
    migration = importlib.import_module(
        "waldur_mastermind.proposal.migrations.0097_backfill_review_submitted_at"
    )

    def test_submitted_reviews_are_dated_once(self):
        logged_at = timezone.now() - datetime.timedelta(days=3)
        review = self.fixture.review
        models.Review.objects.filter(pk=review.pk).update(
            state=ReviewStates.SUBMITTED, submitted_at=None
        )
        before = Event.objects.order_by("-id").values_list("id", flat=True).first()
        activity.log_review_event(review, EventType.REVIEW_SUBMITTED)
        Event.objects.filter(id__gt=before or 0).update(created=logged_at)
        unlogged = factories.ReviewFactory(
            proposal=self.fixture.proposal_submitted, state=ReviewStates.SUBMITTED
        )
        dated_at = timezone.now() - datetime.timedelta(days=9)
        dated = factories.ReviewFactory(
            proposal=self.fixture.proposal_submitted,
            state=ReviewStates.SUBMITTED,
        )
        models.Review.objects.filter(pk=dated.pk).update(submitted_at=dated_at)
        in_review = factories.ReviewFactory(proposal=self.fixture.proposal_submitted)

        self.migration.backfill_submitted_at(django_apps, None)
        for row in (review, unlogged, dated, in_review):
            row.refresh_from_db()
        self.assertEqual(review.submitted_at, logged_at)
        self.assertEqual(unlogged.submitted_at, unlogged.modified)
        self.assertEqual(dated.submitted_at, dated_at)
        self.assertIsNone(in_review.submitted_at)

        # Again: nothing changes.
        self.migration.backfill_submitted_at(django_apps, None)
        review.refresh_from_db()
        self.assertEqual(review.submitted_at, logged_at)


class GlobalReportTest(test.APITestCase):
    """The reports over every call, for staff and support."""

    KINDS = ("portfolio", "workflow", "reviewers", "rounds", "budgets")

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.organisation = self.fixture.manager
        self.call = self.fixture.call
        self.other = factories.CallManagingOrganisationFactory()
        self.other_call = factories.CallFactory(manager=self.other)

    def url(self, kind):
        return factories.CallManagingOrganisationFactory.get_list_url(
            action=f"reports/{kind}"
        )

    def get(self, kind, user=None, **params):
        self.client.force_authenticate(user or self.fixture.staff)
        return self.client.get(self.url(kind), params)

    def report(self, kind, user=None, **params):
        response = self.get(kind, user, **params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def called(self, **params):
        return {row["call_uuid"] for row in self.report("portfolio", **params)["calls"]}

    def test_staff_and_support_report_on_every_organisation(self):
        expected = {self.call.uuid.hex, self.other_call.uuid.hex}
        self.assertTrue(expected <= self.called())
        data = self.report("portfolio", self.fixture.global_support)
        self.assertTrue(expected <= {row["call_uuid"] for row in data["calls"]})

    def test_anyone_else_is_refused(self):
        for user in (self.fixture.call_organizer_user, self.fixture.call_manager):
            for kind in self.KINDS:
                self.assertEqual(
                    self.get(kind, user).status_code, status.HTTP_403_FORBIDDEN
                )

    def test_organisations_narrow_the_scope(self):
        self.assertEqual(
            self.called(organisation_uuid=self.other.uuid.hex),
            {self.other_call.uuid.hex},
        )
        response = self.get(
            "portfolio",
            organisation_uuid=self.other.uuid.hex,
            call_uuid=self.call.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_unknown_organisation_call_or_round_is_not_found(self):
        unknown = "0" * 32
        for params in (
            {"organisation_uuid": unknown},
            {"call_uuid": unknown},
            {"round_uuid": unknown},
        ):
            self.assertEqual(
                self.get("portfolio", **params).status_code,
                status.HTTP_404_NOT_FOUND,
            )

    def test_rows_naming_a_call_name_its_organisation(self):
        populate(self.other_call)
        params = {"organisation_uuid": self.other.uuid.hex}
        expected = {
            "organisation_uuid": self.other.uuid.hex,
            "organisation_name": self.other.customer.name,
        }
        rows = {
            "portfolio": self.report("portfolio", **params)["calls"],
            "workflow": [
                row
                for step in self.report("workflow", **params)["steps"]
                for row in step["calls"]
            ],
            "rounds": self.report("rounds", **params)["rounds"],
            "budgets": self.report("budgets", **params)["pools"],
        }
        for kind, kind_rows in rows.items():
            self.assertTrue(kind_rows, kind)
            for row in kind_rows:
                self.assertEqual({key: row[key] for key in expected}, expected, kind)

    def test_queries_do_not_grow_with_the_organisations(self):
        populate(self.call)

        def queries(kind):
            with CaptureQueriesContext(connection) as context:
                self.report(kind)
            return len(context)

        for kind in self.KINDS:
            queries(kind)  # Warms the role and content type caches.
        before = {kind: queries(kind) for kind in self.KINDS}
        populate(self.other_call)
        third = factories.CallManagingOrganisationFactory()
        populate(factories.CallFactory(manager=third))
        self.assertEqual({kind: queries(kind) for kind in self.KINDS}, before)


def populate(call):
    """A round of ``call`` with a submitted proposal asking for a pooled
    component, a submitted review, a workflow step under way and an
    invitation sent: something for every report to count."""
    call_round = factories.RoundFactory(call=call)
    offering = marketplace_factories.OfferingFactory()
    component = marketplace_factories.OfferingComponentFactory(
        offering=offering,
        type="cpu",
        billing_type=BillingTypes.LIMIT,
        limit_period=LimitPeriods.TOTAL,
    )
    requested_offering = factories.RequestedOfferingFactory(
        call=call, offering=offering
    )
    marketplace_factories.PlanComponentFactory(
        plan=requested_offering.plan, component=component
    )
    models.CallResourcePool.objects.create(
        offering_component=models.CallOfferingComponent.objects.create(
            requested_offering=requested_offering, component_type="cpu"
        ),
        offered_total=100,
    )
    proposal = factories.ProposalFactory(
        round=call_round,
        state=ProposalStates.IN_REVIEW,
        submitted_at=timezone.now(),
    )
    factories.RequestedResourceFactory(
        proposal=proposal, requested_offering=requested_offering, limits={"cpu": 5}
    )
    reviewer = structure_factories.UserFactory()
    factories.ReviewFactory(
        proposal=proposal,
        reviewer=reviewer,
        state=ReviewStates.SUBMITTED,
        summary_score=4,
    )
    factories.ProposalWorkflowStepInstanceFactory(
        proposal=proposal,
        step="technical_assessment",
        status=WorkflowStepInstanceStatuses.ACTIVE,
    )
    batch = factories.AssignmentBatchFactory(
        call=call,
        reviewer_pool_entry=factories.CallReviewerPoolFactory(
            call=call,
            reviewer=factories.ReviewerProfileFactory(user=reviewer),
        ),
        sent_at=timezone.now(),
    )
    factories.AssignmentItemFactory(
        batch=batch, proposal=proposal, status=AssignmentItemStatuses.ACCEPTED
    )
