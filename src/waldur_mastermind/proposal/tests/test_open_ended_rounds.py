"""Rounds without a cut-off: open until a call manager closes them.

A continuously open call (a playground or fast lane access) takes proposals
at any time and evaluates each on submission, so its round has no cut-off.
"""

import datetime

from django.core import mail
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_mastermind.proposal import call_transfer, models, tasks, utils
from waldur_mastermind.proposal.enums import (
    AllocationTimes,
    CallStates,
    EvaluationStart,
    ProposalStates,
    ResultsPublication,
    RoundLifecycleStates,
    RoundStatuses,
)
from waldur_mastermind.proposal.managers import round_open_q
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_call_steps import CallStepsTestMixin


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S")


class OpenEndedRoundTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.add_user(self.fixture.call_manager, CallRole.MANAGER)
        # The fixture's round ended: an open-ended round may follow it.
        self.dated = self.fixture.round
        self.dated.start_time = timezone.now() - datetime.timedelta(days=20)
        self.dated.cutoff_time = timezone.now() - datetime.timedelta(days=10)
        self.dated.save()
        self.url = factories.RoundFactory.get_list_url(self.call)

    def create_round(self, start=None, user="call_manager", **extra):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {"start_time": _iso(start or timezone.now()), **extra}
        return self.client.post(self.url, payload, format="json")

    def open_ended(self, start_days=-1):
        return factories.RoundFactory(
            call=self.call,
            start_time=timezone.now() + datetime.timedelta(days=start_days),
            cutoff_time=None,
        )


class OpenEndedRoundCreateTest(OpenEndedRoundTestBase):
    def test_round_can_be_created_without_cutoff(self):
        response = self.create_round(cutoff_time=None)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data["cutoff_time"])
        self.assertEqual(response.data["status"], RoundStatuses.OPEN)
        call_round = models.Round.objects.get(uuid=response.data["uuid"])
        self.assertTrue(call_round.is_open_ended)
        self.assertTrue(call_round.name.endswith("– open"))

    def test_omitted_cutoff_means_open_ended(self):
        response = self.create_round()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data["cutoff_time"])

    def test_dated_round_can_be_edited_into_an_open_ended_one(self):
        call_round = factories.RoundFactory(call=self.call, opened=True)
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            factories.RoundFactory.get_url(self.call, call_round),
            {"start_time": _iso(call_round.start_time), "cutoff_time": None},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        call_round.refresh_from_db()
        self.assertIsNone(call_round.cutoff_time)

    def test_second_open_ended_round_is_refused(self):
        self.open_ended(start_days=-5)
        response = self.create_round(cutoff_time=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_round_after_an_open_ended_one_is_refused(self):
        self.open_ended(start_days=-5)
        response = self.create_round(
            start=timezone.now() + datetime.timedelta(days=30),
            cutoff_time=_iso(timezone.now() + datetime.timedelta(days=40)),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("overlapping", str(response.data))

    def test_open_ended_round_before_a_later_dated_round_is_refused(self):
        factories.RoundFactory(call=self.call)  # scheduled, starts in 5 days
        response = self.create_round(cutoff_time=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_dated_round_before_an_open_ended_one_is_accepted(self):
        self.open_ended(start_days=30)
        response = self.create_round(
            cutoff_time=_iso(timezone.now() + datetime.timedelta(days=10))
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_refused_when_evaluation_waits_for_the_cutoff(self):
        self.call.evaluation_start = EvaluationStart.AT_CUTOFF
        self.call.save()
        response = self.create_round(cutoff_time=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cutoff_time", response.data)

    def test_refused_when_results_are_published_with_the_round(self):
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save()
        response = self.create_round(cutoff_time=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refused_when_allocation_is_on_a_fixed_date(self):
        models.CallWorkflowStep.objects.filter(
            call=self.call, is_allocation_decision=True
        ).update(allocation_time=AllocationTimes.FIXED_DATE)
        response = self.create_round(cutoff_time=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_ended_round_cannot_be_reopened(self):
        call_round = self.open_ended()
        call_round.cutoff_time = timezone.now()
        call_round.lifecycle_state = RoundLifecycleStates.EVALUATING
        call_round.save()
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            factories.RoundFactory.get_url(self.call, call_round),
            {"start_time": _iso(call_round.start_time), "cutoff_time": None},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_repeated_rounds_need_a_cutoff(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(
            self.url + "?repeat=true&count=3",
            {"start_time": timezone.now().strftime("%Y-%m-%dT%H:%M")},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_rounds_overlapping_an_open_ended_round_are_refused(self):
        self.open_ended(start_days=-5)
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(
            factories.CallFactory.get_protected_url(self.call, "rounds-bulk-set"),
            {
                "start_time": _iso(timezone.now() + datetime.timedelta(days=30)),
                "cadence": "monthly",
                "submission_window_days": 10,
                "number_of_rounds": 2,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OpenEndedRoundLocksCallSettingsTest(OpenEndedRoundTestBase):
    def setUp(self):
        super().setUp()
        self.open_ended()
        self.client.force_authenticate(self.fixture.call_manager)

    def patch_call(self, **values):
        return self.client.patch(
            factories.CallFactory.get_protected_url(self.call), values, format="json"
        )

    def test_call_cannot_switch_to_evaluation_at_cutoff(self):
        response = self.patch_call(evaluation_start=EvaluationStart.AT_CUTOFF)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("evaluation_start", response.data)

    def test_call_cannot_switch_to_publishing_with_the_round(self):
        response = self.patch_call(publish_results=ResultsPublication.WITH_ROUND)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("publish_results", response.data)

    def test_allocation_decision_cannot_switch_to_a_fixed_date(self):
        step = models.CallWorkflowStep.allocation_decision_for(self.call)
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, step),
            {"allocation_time": AllocationTimes.FIXED_DATE},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allocation_time", response.data)

    def test_model_validation_refuses_the_switch(self):
        self.call.evaluation_start = EvaluationStart.AT_CUTOFF
        with self.assertRaises(Exception):
            self.call.clean()

    def test_unrelated_call_edit_is_accepted(self):
        response = self.patch_call(description="Still open")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class OpenEndedRoundIsOpenTest(OpenEndedRoundTestBase):
    def setUp(self):
        super().setUp()
        self.round = self.open_ended()

    def test_status_is_open(self):
        self.assertEqual(self.round.status, RoundStatuses.OPEN)

    def test_round_is_found_by_the_open_round_helper(self):
        self.assertIn(self.round, models.Round.objects.open_at())
        self.assertIn(self.call, models.Call.objects.filter(round_open_q("round")))

    def test_scheduled_open_ended_round_is_not_open_yet(self):
        self.round.start_time = timezone.now() + datetime.timedelta(days=1)
        self.round.save()
        self.assertNotIn(self.round, models.Round.objects.open_at())

    def test_offering_is_open_for_proposals(self):
        requested = factories.RequestedOfferingFactory(call=self.call, state="accepted")
        self.assertIn(requested, models.RequestedOffering.objects.open_for_proposals())

    def test_public_call_has_no_end_date_and_lists_the_round(self):
        response = self.client.get(factories.CallFactory.get_public_url(self.call))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["end_date"])
        self.assertEqual(response.data["rounds"][0]["uuid"], self.round.uuid.hex)

    def test_call_is_listed_as_having_an_active_round(self):
        other = factories.CallFactory(state=CallStates.ACTIVE)
        response = self.client.get(
            factories.CallFactory.get_public_list_url(), {"has_active_round": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.call.uuid.hex, uuids)
        self.assertNotIn(other.uuid.hex, uuids)

    def test_dashboard_lists_the_round_last_without_a_due_date(self):
        dated = factories.RoundFactory(call=factories.CallFactory())
        dated.call.add_user(self.fixture.call_manager, CallRole.MANAGER)
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(
            "http://testserver" + reverse("call-round-list") + "dashboard-deadlines/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = [(row["uuid"], row["due_date"]) for row in response.data]
        self.assertEqual(rows[-1], (self.round.uuid.hex, None))
        self.assertIn(dated.uuid.hex, [uuid for uuid, _ in rows])

    def test_organisation_stats_count_the_round_as_active(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            "http://testserver"
            + reverse(
                "call-managing-organisation-detail",
                kwargs={"uuid": self.call.manager.uuid.hex},
            )
            + "stats/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["active_rounds"], 1)

    def test_proposal_can_be_created(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(
            factories.ProposalFactory.get_list_url(),
            {"name": "Rolling", "round_uuid": self.round.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_hourly_sweeps_leave_its_proposals_alone(self):
        draft = factories.ProposalFactory(round=self.round, state=ProposalStates.DRAFT)
        submitted = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        with self.captureOnCommitCallbacks(execute=True):
            tasks.proposals_for_ended_rounds_should_be_cancelled()
        draft.refresh_from_db()
        submitted.refresh_from_db()
        self.assertEqual(draft.state, ProposalStates.DRAFT)
        self.assertEqual(submitted.state, ProposalStates.SUBMITTED)
        self.round.refresh_from_db()
        self.assertIsNone(self.round.lifecycle_state)

    def test_no_cutoff_notification_or_deadline_reminder(self):
        draft = factories.ProposalFactory(round=self.round, state=ProposalStates.DRAFT)
        draft.add_user(draft.created_by, ProposalRole.MANAGER)
        tasks.notify_manager_on_round_cutoff()
        tasks.notify_proposal_creator_on_submission_deadline_approaching()
        self.assertEqual(mail.outbox, [])


class OpenEndedRoundSubmissionTest(CallStepsTestMixin, test.APITestCase):
    def test_submission_starts_the_first_step_at_once(self):
        call_round = self.proposal.round
        call_round.start_time = timezone.now() - datetime.timedelta(days=1)
        call_round.cutoff_time = None
        call_round.save()
        self.fixture.new_round.delete()
        proposal = self.submit()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(proposal.workflow_step, "administrative_check")


class OpenEndedRoundCloseTest(OpenEndedRoundTestBase):
    def setUp(self):
        super().setUp()
        self.round = self.open_ended(start_days=-30)
        self.draft = factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT
        )
        self.in_review = factories.ProposalFactory(
            round=self.round, state=ProposalStates.IN_REVIEW
        )

    def close(self):
        self.client.force_authenticate(self.fixture.call_manager)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                factories.RoundFactory.get_url(self.call, self.round, "close")
            )

    def test_closing_sets_the_cutoff_and_starts_the_lifecycle(self):
        before = timezone.now()
        response = self.close()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.round.refresh_from_db()
        self.assertGreaterEqual(self.round.cutoff_time, before)
        self.assertEqual(self.round.status, RoundStatuses.ENDED)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.EVALUATING)
        self.in_review.refresh_from_db()
        self.assertEqual(self.in_review.state, ProposalStates.IN_REVIEW)

    def test_drafts_are_cancelled_without_a_later_round(self):
        self.close()
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.state, ProposalStates.CANCELED)

    def test_drafts_move_to_a_new_open_ended_round(self):
        self.close()
        # Reopening the call: a new round without a cut-off.
        self.round.refresh_from_db()
        next_round = factories.RoundFactory(
            call=self.call, start_time=self.round.cutoff_time, cutoff_time=None
        )
        draft = factories.ProposalFactory(round=self.dated, state=ProposalStates.DRAFT)
        self.assertEqual(utils.next_round_for_drafts(self.dated), next_round)
        self.assertEqual(draft.state, ProposalStates.DRAFT)


class OpenEndedRoundTransferTest(OpenEndedRoundTestBase):
    def setUp(self):
        super().setUp()
        self.round = self.open_ended()

    def test_export_and_import_keep_the_round_open_ended(self):
        data = call_transfer.export_call(self.call)[0]
        cutoffs = [r["cutoff_time"] for r in data["rounds"]]
        self.assertIn(None, cutoffs)
        call, _, _ = call_transfer.import_call(
            data, self.call.manager, self.fixture.staff
        )
        self.assertTrue(call.has_open_ended_round())

    def test_import_refuses_an_open_ended_round_waiting_for_a_cutoff(self):
        data = call_transfer.export_call(self.call)[0]
        data["call"]["evaluation_start"] = EvaluationStart.AT_CUTOFF
        with self.assertRaisesRegex(Exception, "cut-off"):
            call_transfer.import_call(data, self.call.manager, self.fixture.staff)

    def test_import_refuses_an_open_ended_round_with_fixed_date_allocation(self):
        data = call_transfer.export_call(self.call)[0]
        for step in data["workflow_steps"]:
            if step.get("is_allocation_decision"):
                step["allocation_time"] = AllocationTimes.FIXED_DATE
        with self.assertRaisesRegex(Exception, "fixed date"):
            call_transfer.import_call(data, self.call.manager, self.fixture.staff)

    def test_import_refuses_two_open_ended_rounds(self):
        data = call_transfer.export_call(self.call)[0]
        data["rounds"].append(dict(data["rounds"][-1]))
        with self.assertRaisesRegex(Exception, "overlapping"):
            call_transfer.import_call(data, self.call.manager, self.fixture.staff)

    def test_duplicate_keeps_the_round_open_ended(self):
        copy = utils.duplicate_call(
            self.call, "Copy", self.fixture.staff, {"copy_rounds": True}
        )
        self.assertTrue(copy.has_open_ended_round())
