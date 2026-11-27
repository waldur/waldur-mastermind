"""One decision step decided for many proposals of a call in one run."""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import DataError
from django.db import transaction as django_transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import bulk_decisions, models, tasks, workflow_service
from waldur_mastermind.proposal.enums import (
    BulkStepDecisionResults,
    BulkStepDecisionStates,
    ProposalStates,
    ResponsibleRoles,
    StepKinds,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures

DECIDED = BulkStepDecisionResults.DECIDED
REFUSED = BulkStepDecisionResults.REFUSED
DECISION_EMAIL = (
    "waldur_mastermind.proposal.tasks.notify_user_about_proposal_state_update.delay"
)


class BulkStepDecisionTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.manager = self.fixture.call_manager
        self.check = self.call.workflow_steps.get(key="administrative_check")
        self.allocation = self.call.workflow_steps.get(key="allocation_decision")
        self.proposals = [self.at_check(f"Proposal {i}") for i in range(3)]

    def at_check(self, name, call_round=None):
        """A submitted proposal standing on its call's administrative check."""
        proposal = factories.ProposalFactory(
            name=name,
            round=call_round or self.fixture.round,
            state=ProposalStates.SUBMITTED,
            project=None,
        )
        proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        workflow_service.create_step_instances(proposal)
        workflow_service.activate_first_step(proposal)
        proposal.refresh_from_db()
        self.assertEqual(proposal.workflow_step, "administrative_check")
        return proposal

    def url(self, action="decide_workflow_steps"):
        return factories.CallFactory.get_protected_url(self.call, action=action)

    def runs_url(self, run=None):
        if run is None:
            return "http://testserver" + reverse("proposal-bulk-step-decision-list")
        return "http://testserver" + reverse(
            "proposal-bulk-step-decision-detail", kwargs={"uuid": run}
        )

    def decide(self, proposals=None, outcome="eligible", user=None, **extra):
        self.client.force_authenticate(user or self.manager)
        proposals = self.proposals if proposals is None else proposals
        return self.client.post(
            self.url(),
            {
                "call_step_uuid": self.check.uuid.hex,
                "outcome": outcome,
                "outcome_reason": "Complete and in scope.",
                "proposal_uuids": [
                    p if isinstance(p, str) else p.uuid.hex for p in proposals
                ],
                **extra,
            },
            format="json",
        )

    def check_instance(self, proposal):
        return proposal.workflow_step_instances.get(call_step=self.check)

    def by_uuid(self, response):
        return {r["proposal_uuid"]: r for r in response.data["results"]}

    def events(self):
        return Event.objects.filter(event_type=EventType.PROPOSAL_STEPS_DECIDED_IN_BULK)

    def assert_still_at_check(self, *proposals):
        for proposal in proposals:
            self.assertEqual(
                self.check_instance(proposal).status,
                WorkflowStepInstanceStatuses.ACTIVE,
            )


class DecideTest(BulkStepDecisionTestBase):
    def test_every_proposal_at_the_step_is_decided(self):
        response = self.decide()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["state"], BulkStepDecisionStates.DONE)
        self.assertFalse(response.data["dry_run"])
        self.assertEqual(response.data["decided_count"], 3)
        self.assertEqual(response.data["refused_count"], 0)
        # In the order named.
        self.assertEqual(
            [r["proposal_uuid"] for r in response.data["results"]],
            [p.uuid.hex for p in self.proposals],
        )
        for proposal in self.proposals:
            proposal.refresh_from_db()
            instance = self.check_instance(proposal)
            self.assertEqual(instance.status, WorkflowStepInstanceStatuses.COMPLETED)
            self.assertEqual(instance.outcome, "eligible")
            self.assertEqual(instance.outcome_reason, "Complete and in scope.")
            self.assertEqual(instance.completed_by, self.manager)
            self.assertEqual(proposal.workflow_step, "allocation_decision")
        run = models.BulkStepDecision.objects.get()
        self.assertEqual(response.data["uuid"], run.uuid.hex)
        self.assertEqual(run.created_by, self.manager)
        self.assertEqual(run.processed, 3)
        self.assertEqual(run.result_rows.count(), 3)

    def test_a_refusal_does_not_undo_the_others(self):
        moved_on = self.proposals[0]
        self.decide(proposals=[moved_on])

        response = self.decide()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        results = self.by_uuid(response)
        self.assertEqual(results[moved_on.uuid.hex]["status"], REFUSED)
        self.assertEqual(
            results[moved_on.uuid.hex]["detail"], str(bulk_decisions.NOT_AT_STEP)
        )
        # The call's manager oversees it: where it stands is told.
        self.assertEqual(
            results[moved_on.uuid.hex]["current_step_name"], "Allocation decision"
        )
        for proposal in self.proposals[1:]:
            self.assertEqual(results[proposal.uuid.hex]["status"], DECIDED)

    def test_a_gate_refuses_only_its_proposal(self):
        gated = self.proposals[1]
        real = workflow_service._enforce_step_gates

        def gate(proposal, *args):
            if proposal.pk == gated.pk:
                raise ValueError("Cannot complete: gate holds.")
            return real(proposal, *args)

        with mock.patch.object(workflow_service, "_enforce_step_gates", gate):
            response = self.decide()

        results = self.by_uuid(response)
        self.assertEqual(results[gated.uuid.hex]["status"], REFUSED)
        self.assertEqual(
            results[gated.uuid.hex]["detail"], "Cannot complete: gate holds."
        )
        self.assert_still_at_check(gated)
        self.assertEqual(response.data["decided_count"], 2)

    def test_the_score_gate_is_told_without_the_score(self):
        self.check.min_score_threshold = Decimal("4.0")
        self.check.save(update_fields=["min_score_threshold"])

        response = self.decide(proposals=self.proposals[:1])

        entry = response.data["results"][0]
        self.assertEqual(entry["status"], REFUSED)
        self.assertEqual(
            entry["detail"],
            "Cannot complete Administrative check: its score is below the "
            "required minimum.",
        )
        stored = models.BulkStepDecisionResult.objects.get()
        self.assertNotIn("4.0", stored.detail)

    def test_a_negative_outcome_ends_the_workflow_and_tells_the_applicant(self):
        with (
            mock.patch(DECISION_EMAIL) as decision_email,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.decide(proposals=self.proposals[:1], outcome="ineligible")

        self.assertEqual(response.data["results"][0]["status"], DECIDED)
        self.proposals[0].refresh_from_db()
        self.assertEqual(self.proposals[0].state, ProposalStates.REJECTED)
        decision_email.assert_called_once_with(
            self.proposals[0].uuid, ProposalStates.IN_REVIEW, ProposalStates.REJECTED
        )

    def test_one_event_records_the_run(self):
        self.decide()

        event = self.events().get()
        self.assertEqual(event.context["outcome"], "eligible")
        self.assertEqual(event.context["outcome_reason"], "Complete and in scope.")
        self.assertEqual(event.context["decided_count"], 3)
        self.assertEqual(event.context["refused_count"], 0)
        self.assertEqual(event.context["step_name"], "Administrative check")
        self.assertEqual(event.context["actor_uuid"], self.manager.uuid.hex)
        self.assertIn("3 decided, 0 refused", event.message)

    def test_a_proposal_not_of_the_call_is_refused_as_not_found(self):
        other = factories.ProposalFactory()
        missing = uuid.uuid4().hex

        response = self.decide(proposals=[other, self.proposals[0], missing])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [(r["proposal_uuid"], r["detail"]) for r in response.data["results"]],
            [
                (other.uuid.hex, str(bulk_decisions.NOT_FOUND)),
                (self.proposals[0].uuid.hex, "Decided."),
                (missing, str(bulk_decisions.NOT_FOUND)),
            ],
        )
        self.assertEqual(response.data["results"][0]["proposal_name"], "")

    def test_a_failure_of_the_run_itself_marks_it_failed(self):
        with mock.patch.object(
            bulk_decisions, "_record", side_effect=RuntimeError("database gone")
        ):
            response = self.decide()

        self.assertEqual(response.data["state"], BulkStepDecisionStates.FAILED)
        self.assertEqual(response.data["error_message"], str(bulk_decisions.RUN_FAILED))
        self.assertNotIn("database gone", response.data["error_message"])
        self.assertFalse(self.events().exists())


class SelectionTest(BulkStepDecisionTestBase):
    def test_a_selection_off_the_step_is_refused_and_nothing_recorded(self):
        self.decide()
        models.BulkStepDecision.objects.all().delete()

        response = self.decide()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("proposal_uuids", response.data)
        self.assertFalse(models.BulkStepDecision.objects.exists())

    def test_a_step_of_another_call_is_refused(self):
        other_step = factories.CallFactory().workflow_steps.get(
            key="administrative_check"
        )
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            self.url(),
            {
                "call_step_uuid": other_step.uuid.hex,
                "outcome": "eligible",
                "proposal_uuids": [p.uuid.hex for p in self.proposals],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("call_step_uuid", response.data)


class DryRunTest(BulkStepDecisionTestBase):
    def setUp(self):
        super().setUp()
        # One moved on, and one the manager applies with.
        self.decide(proposals=[self.proposals[0]])
        self.proposals[1].add_user(self.manager, ProposalRole.MEMBER)
        models.BulkStepDecision.objects.all().delete()
        self.events().delete()

    def test_writes_nothing(self):
        response = self.decide(dry_run=True)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["dry_run"])
        self.assertIsNone(response.data["uuid"])
        self.assertEqual(response.data["decided_count"], 1)
        self.assertEqual(response.data["refused_count"], 2)
        self.assert_still_at_check(self.proposals[2])
        self.assertFalse(models.BulkStepDecision.objects.exists())
        self.assertFalse(self.events().exists())

    def test_tells_what_the_run_does(self):
        preview = self.by_uuid(self.decide(dry_run=True))
        run = self.by_uuid(self.decide())

        self.assertEqual(
            {key: entry["status"] for key, entry in preview.items()},
            {key: entry["status"] for key, entry in run.items()},
        )
        applying = self.proposals[1].uuid.hex
        self.assertEqual(preview[applying]["detail"], run[applying]["detail"])
        self.assertEqual(preview[applying]["detail"], str(bulk_decisions.MAY_NOT_ACT))
        # Of a proposal the user may not act on, only its id and why.
        self.assertEqual(run[applying]["proposal_name"], "")
        self.assertEqual(run[applying]["proposal_state"], "")
        self.assertEqual(run[applying]["current_step_name"], "")

    @mock.patch.object(bulk_decisions, "BACKGROUND_THRESHOLD", 2)
    def test_a_large_preview_runs_in_the_background(self):
        with (
            mock.patch.object(tasks.run_bulk_step_decision, "delay") as delay,
            mock.patch.object(
                bulk_decisions, "preview_one", wraps=bulk_decisions.preview_one
            ) as preview_one,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.decide(dry_run=True)
        # Bounded within the request: no proposal is checked there.
        preview_one.assert_not_called()

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        run = models.BulkStepDecision.objects.get()
        self.assertTrue(run.dry_run)
        delay.assert_called_once_with(run.uuid.hex)

        tasks.run_bulk_step_decision(run.uuid.hex)

        run.refresh_from_db()
        self.assertEqual(run.state, BulkStepDecisionStates.DONE)
        self.assertEqual((run.decided_count, run.refused_count), (1, 2))
        self.assert_still_at_check(self.proposals[2])
        self.assertFalse(self.events().exists())


class PermissionTest(BulkStepDecisionTestBase):
    def test_a_user_who_may_not_decide_in_bulk_creates_nothing(self):
        response = self.decide(user=self.fixture.reviewer_1)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.BulkStepDecision.objects.exists())
        self.assertFalse(self.events().exists())
        self.assert_still_at_check(*self.proposals)

    def test_a_manager_who_may_act_on_none_of_the_selection_creates_nothing(self):
        for proposal in self.proposals:
            proposal.add_user(self.manager, ProposalRole.MEMBER)

        response = self.decide()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.BulkStepDecision.objects.exists())
        self.assertFalse(self.events().exists())

    def test_a_user_who_does_not_see_the_call_gets_not_found(self):
        response = self.decide(user=structure_factories.UserFactory())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_decide(self):
        response = self.decide(user=self.fixture.staff)
        self.assertEqual(response.data["decided_count"], 3)


class StepKindTest(BulkStepDecisionTestBase):
    def test_the_allocation_decision_is_refused(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            self.url(),
            {
                "call_step_uuid": self.allocation.uuid.hex,
                "outcome": "approved",
                "proposal_uuids": [p.uuid.hex for p in self.proposals],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("call_step_uuid", response.data)

    def test_a_round_step_is_refused(self):
        self.check.round_scoped = True
        self.check.save(update_fields=["round_scoped"])
        response = self.decide()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_outcome_of_another_step_is_refused(self):
        response = self.decide(outcome="approved")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("outcome", response.data)


@mock.patch.object(bulk_decisions, "BACKGROUND_THRESHOLD", 2)
class BackgroundRunTest(BulkStepDecisionTestBase):
    def start_in_background(self):
        with mock.patch.object(tasks.run_bulk_step_decision, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.decide()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertEqual(response.data["state"], BulkStepDecisionStates.PENDING)
        run = models.BulkStepDecision.objects.get(uuid=response.data["uuid"])
        delay.assert_called_once_with(run.uuid.hex)
        return run

    def test_a_large_selection_runs_in_the_background(self):
        run = self.start_in_background()
        self.assert_still_at_check(*self.proposals)

        tasks.run_bulk_step_decision(run.uuid.hex)

        response = self.client.get(self.runs_url(run.uuid.hex))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["state"], BulkStepDecisionStates.DONE)
        self.assertEqual(response.data["processed"], 3)
        self.assertEqual(response.data["decided_count"], 3)
        self.assertEqual(len(response.data["results"]), 3)
        self.assertEqual(self.events().count(), 1)
        run.refresh_from_db()
        self.assertIsNotNone(run.celery_task_id)

    def test_a_run_that_cannot_be_dispatched_fails(self):
        with mock.patch.object(
            tasks.run_bulk_step_decision, "delay", side_effect=OSError("broker down")
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.decide()
        run = models.BulkStepDecision.objects.get(uuid=response.data["uuid"])
        self.assertEqual(run.state, BulkStepDecisionStates.FAILED)
        self.assertEqual(run.error_message, str(bulk_decisions.RUN_FAILED))

    def test_an_interrupted_run_goes_on_from_where_it_stopped(self):
        run = self.start_in_background()
        real = bulk_decisions._decide_locked
        calls = []

        def stops_on_the_second(decision, proposal_uuid):
            calls.append(proposal_uuid)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(decision, proposal_uuid)

        with mock.patch.object(
            bulk_decisions, "_decide_locked", side_effect=stops_on_the_second
        ):
            with self.assertRaises(KeyboardInterrupt):
                bulk_decisions.execute(run)
        run.refresh_from_db()
        self.assertEqual(run.state, BulkStepDecisionStates.RUNNING)
        self.assertEqual(run.processed, 1)

        tasks.run_bulk_step_decision(run.uuid.hex)

        run.refresh_from_db()
        self.assertEqual(run.state, BulkStepDecisionStates.DONE)
        self.assertEqual(run.decided_count, 3)
        self.assertEqual(
            list(run.result_rows.values_list("proposal_uuid", flat=True)),
            [p.uuid.hex for p in self.proposals],
        )

    def test_a_second_execution_records_nothing_twice(self):
        run = self.start_in_background()
        tasks.run_bulk_step_decision(run.uuid.hex)
        tasks.run_bulk_step_decision(run.uuid.hex)

        run.refresh_from_db()
        self.assertEqual(run.state, BulkStepDecisionStates.DONE)
        self.assertEqual(run.result_rows.count(), 3)
        self.assertEqual((run.processed, run.decided_count), (3, 3))
        self.assertEqual(self.events().count(), 1)

    def test_an_executor_goes_on_from_the_first_unrecorded_proposal(self):
        run = self.start_in_background()
        # Another executor holds the run and has recorded the first proposal.
        models.BulkStepDecision.objects.filter(pk=run.pk).update(
            state=BulkStepDecisionStates.RUNNING
        )
        with self.captureOnCommitCallbacks(execute=True):
            entry = bulk_decisions._decide_locked(
                bulk_decisions.decision_of(run), self.proposals[0].uuid.hex
            )
        run.refresh_from_db()
        bulk_decisions._record(run, 0, entry)

        bulk_decisions.execute(run)

        run.refresh_from_db()
        self.assertEqual(
            list(run.result_rows.values_list("position", flat=True)), [0, 1, 2]
        )
        self.assertEqual((run.decided_count, run.refused_count), (3, 0))

    def test_a_user_deactivated_after_the_start_decides_nothing(self):
        run = self.start_in_background()
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"])

        tasks.run_bulk_step_decision(run.uuid.hex)

        run.refresh_from_db()
        self.assertEqual(run.state, BulkStepDecisionStates.FAILED)
        self.assertEqual(run.error_message, str(bulk_decisions.USER_GONE))
        self.assert_still_at_check(*self.proposals)
        self.assertFalse(self.events().exists())

    def test_a_role_revoked_after_the_start_decides_nothing(self):
        run = self.start_in_background()
        self.call.remove_user(self.manager, CallRole.MANAGER)

        tasks.run_bulk_step_decision(run.uuid.hex)

        run.refresh_from_db()
        self.assertEqual(run.state, BulkStepDecisionStates.FAILED)
        self.assert_still_at_check(*self.proposals)

    def test_a_step_changed_after_the_start_decides_nothing(self):
        run = self.start_in_background()
        self.check.kind = StepKinds.ASSESSMENT
        self.check.save(update_fields=["kind"])

        tasks.run_bulk_step_decision(run.uuid.hex)

        run.refresh_from_db()
        self.assertEqual(run.state, BulkStepDecisionStates.FAILED)
        self.assertIn("Only a decision step", run.error_message)
        self.assert_still_at_check(*self.proposals)

    def test_the_sweeper_dispatches_a_stalled_run_again(self):
        run = self.start_in_background()
        stale = timezone.now() - bulk_decisions.STALE_AFTER - timedelta(minutes=1)
        # A worker had claimed it.
        models.BulkStepDecision.objects.filter(pk=run.pk).update(
            modified=stale, state=BulkStepDecisionStates.RUNNING
        )

        with mock.patch.object(tasks.run_bulk_step_decision, "delay") as delay:
            self.assertEqual(tasks.resume_stale_bulk_step_decisions(), 1)
            # Touched: the next sweep leaves it to the dispatched task.
            self.assertEqual(tasks.resume_stale_bulk_step_decisions(), 0)
        delay.assert_called_once_with(run.uuid.hex)
        run.refresh_from_db()
        self.assertEqual(run.attempts, 1)

    def test_the_sweeper_gives_up_a_run_interrupted_too_often(self):
        run = self.start_in_background()
        stale = timezone.now() - bulk_decisions.STALE_AFTER - timedelta(minutes=1)
        models.BulkStepDecision.objects.filter(pk=run.pk).update(
            modified=stale,
            attempts=bulk_decisions.MAX_ATTEMPTS,
            state=BulkStepDecisionStates.RUNNING,
        )

        with mock.patch.object(tasks.run_bulk_step_decision, "delay") as delay:
            tasks.resume_stale_bulk_step_decisions()
        delay.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.state, BulkStepDecisionStates.FAILED)

    def test_the_call_team_and_the_starter_read_a_run(self):
        run = self.start_in_background()
        tasks.run_bulk_step_decision(run.uuid.hex)

        self.client.force_authenticate(self.fixture.call_organizer_user)
        self.assertEqual(
            self.client.get(self.runs_url(run.uuid.hex)).status_code,
            status.HTTP_200_OK,
        )
        response = self.client.get(self.runs_url(), {"call_uuid": self.call.uuid.hex})
        self.assertEqual(len(response.data), 1)
        # A list is polled: counts, not results.
        self.assertNotIn("results", response.data[0])
        self.assertEqual(response.data[0]["decided_count"], 3)

        self.client.force_authenticate(self.fixture.reviewer_1)
        self.assertEqual(
            self.client.get(self.runs_url(run.uuid.hex)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(len(self.client.get(self.runs_url()).data), 0)


class DecidableStepsTest(BulkStepDecisionTestBase):
    def steps(self, user=None):
        self.client.force_authenticate(user or self.manager)
        return self.client.post(
            self.url("decidable_workflow_steps"),
            {"proposal_uuids": [p.uuid.hex for p in self.proposals]},
            format="json",
        )

    def test_groups_the_selection_by_the_step_it_stands_on(self):
        self.decide(proposals=[self.proposals[0]])

        response = self.steps()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # The allocation decision the first one moved on to is not one.
        self.assertEqual(len(response.data), 1)
        group = response.data[0]
        self.assertEqual(group["call_step_uuid"], self.check.uuid.hex)
        self.assertEqual(group["step_name"], "Administrative check")
        self.assertEqual(sorted(group["allowed_outcomes"]), ["eligible", "ineligible"])
        self.assertEqual(
            sorted(group["proposal_uuids"]),
            sorted(p.uuid.hex for p in self.proposals[1:]),
        )

    def test_leaves_out_what_the_user_may_not_act_on(self):
        self.proposals[0].add_user(self.manager, ProposalRole.MEMBER)
        self.assertEqual(
            sorted(self.steps().data[0]["proposal_uuids"]),
            sorted(p.uuid.hex for p in self.proposals[1:]),
        )
        self.assertEqual(self.steps(user=self.fixture.reviewer_1).data, [])


class RecordingTest(BulkStepDecisionTestBase):
    def test_long_parallel_step_names_are_recorded_cut_to_fit(self):
        with mock.patch.object(
            workflow_service, "standing_label", return_value=" · ".join(["x" * 200] * 3)
        ):
            response = self.decide()

        self.assertEqual(response.data["state"], BulkStepDecisionStates.DONE)
        self.assertEqual(response.data["decided_count"], 3)
        for row in models.BulkStepDecisionResult.objects.all():
            self.assertLessEqual(len(row.current_step_name), 255)

    def test_a_proposal_that_cannot_be_recorded_does_not_stop_the_run(self):
        real = bulk_decisions._record
        calls = []

        def fails_once(run, position, entry):
            calls.append(position)
            if len(calls) == 1:
                raise DataError("value too long")
            return real(run, position, entry)

        with mock.patch.object(bulk_decisions, "_record", side_effect=fails_once):
            response = self.decide()

        self.assertEqual(response.data["state"], BulkStepDecisionStates.DONE)
        self.assertEqual(
            [(r["proposal_uuid"], r["status"]) for r in response.data["results"]],
            [
                (self.proposals[0].uuid.hex, REFUSED),
                (self.proposals[1].uuid.hex, DECIDED),
                (self.proposals[2].uuid.hex, DECIDED),
            ],
        )
        # The refused proposal's decision was rolled back with its record.
        self.assert_still_at_check(self.proposals[0])

    def test_the_applicant_notification_is_queued_robustly(self):
        with mock.patch.object(bulk_decisions.transaction, "on_commit") as on_commit:
            self.decide(proposals=self.proposals[:1], outcome="ineligible")
        self.assertTrue(on_commit.call_args.kwargs.get("robust"))

    def test_a_step_the_applicant_completes_is_refused(self):
        self.check.completer = ResponsibleRoles.APPLICANT
        self.check.save(update_fields=["completer"])
        response = self.decide()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("call_step_uuid", response.data)


class ReaderTest(BulkStepDecisionTestBase):
    def setUp(self):
        super().setUp()
        self.response = self.decide()
        self.run = models.BulkStepDecision.objects.get()
        self.other_manager = structure_factories.UserFactory()
        self.call.add_user(self.other_manager, CallRole.MANAGER)

    def read(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.runs_url(self.run.uuid.hex))

    def test_a_reader_does_not_see_the_proposal_they_apply_with(self):
        self.proposals[1].add_user(self.other_manager, ProposalRole.MEMBER)

        response = self.read(self.other_manager)

        self.assertEqual(
            [r["proposal_uuid"] for r in response.data["results"]],
            [self.proposals[0].uuid.hex, self.proposals[2].uuid.hex],
        )
        self.assertEqual(response.data["outcome_reason"], "Complete and in scope.")

    def test_nor_the_reason_while_its_decision_is_held(self):
        self.proposals[1].add_user(self.other_manager, ProposalRole.MEMBER)
        models.Proposal.objects.filter(pk=self.proposals[1].pk).update(
            decision_held=True
        )

        response = self.read(self.other_manager)

        self.assertEqual(response.data["outcome_reason"], "")
        self.client.force_authenticate(self.other_manager)
        listed = self.client.get(self.runs_url()).data
        self.assertEqual(listed[0]["outcome_reason"], "")

    def test_the_starter_reads_the_run_only_while_on_the_call(self):
        self.assertEqual(self.read(self.manager).status_code, status.HTTP_200_OK)
        self.call.remove_user(self.manager, CallRole.MANAGER)
        self.assertEqual(self.read(self.manager).status_code, status.HTTP_404_NOT_FOUND)


@mock.patch.object(bulk_decisions, "BACKGROUND_THRESHOLD", 2)
class HousekeepingTest(BulkStepDecisionTestBase):
    def background(self, **extra):
        with mock.patch.object(tasks.run_bulk_step_decision, "delay"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.decide(**extra)
        return models.BulkStepDecision.objects.get(uuid=response.data["uuid"])

    def test_an_unclaimed_run_is_dispatched_again_without_counting(self):
        run = self.background()
        stale = timezone.now() - bulk_decisions.STALE_AFTER - timedelta(minutes=1)
        models.BulkStepDecision.objects.filter(pk=run.pk).update(modified=stale)

        with mock.patch.object(tasks.run_bulk_step_decision, "delay") as delay:
            tasks.resume_stale_bulk_step_decisions()
        delay.assert_called_once_with(run.uuid.hex)
        run.refresh_from_db()
        self.assertEqual(run.attempts, 0)
        self.assertEqual(run.state, BulkStepDecisionStates.PENDING)

    def test_a_run_never_claimed_for_a_day_is_given_up(self):
        run = self.background()
        old = timezone.now() - bulk_decisions.PENDING_GIVE_UP - timedelta(hours=1)
        models.BulkStepDecision.objects.filter(pk=run.pk).update(
            created=old, modified=old
        )
        with mock.patch.object(tasks.run_bulk_step_decision, "delay") as delay:
            tasks.resume_stale_bulk_step_decisions()
        delay.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.state, BulkStepDecisionStates.FAILED)

    def test_old_previews_are_deleted(self):
        preview = self.background(dry_run=True)
        run = self.background()
        old = timezone.now() - bulk_decisions.PREVIEW_KEPT - timedelta(hours=1)
        models.BulkStepDecision.objects.filter(pk__in=[preview.pk, run.pk]).update(
            created=old
        )
        with mock.patch.object(tasks.run_bulk_step_decision, "delay"):
            tasks.resume_stale_bulk_step_decisions()
        self.assertFalse(models.BulkStepDecision.objects.filter(pk=preview.pk).exists())
        self.assertTrue(models.BulkStepDecision.objects.filter(pk=run.pk).exists())

    def test_a_failed_dispatch_is_answered_as_failed(self):
        with mock.patch.object(
            tasks.run_bulk_step_decision, "delay", side_effect=OSError("broker down")
        ):
            response = self.decide()
        self.assertEqual(response.data["state"], BulkStepDecisionStates.FAILED)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class CommitCallbackTest(test.APITransactionTestCase):
    """A callback run once a proposal has committed fails: the proposal is
    decided and recorded, and the run goes on.

    APITransactionTestCase required: the callback must run after a real
    commit of the run's per-proposal transaction, which a TestCase's
    wrapping transaction never makes.
    """

    def test_the_run_goes_on(self):
        base = BulkStepDecisionTestBase()
        base.client = self.client
        base.assertEqual = self.assertEqual
        base.setUp()
        real = bulk_decisions._decide_and_record

        def with_failing_callback(locked, position):
            real(locked, position)
            django_transaction.on_commit(raise_error)

        def raise_error():
            raise RuntimeError("callback failed")

        with (
            # As Django runs it, after the commit; not the suite's immediate one.
            mock.patch.object(
                django_transaction,
                "on_commit",
                lambda func, using=None, robust=False: (
                    django_transaction.get_connection(using).on_commit(func, robust)
                ),
            ),
            mock.patch.object(
                bulk_decisions,
                "_decide_and_record",
                side_effect=with_failing_callback,
            ),
        ):
            response = base.decide()

        self.assertEqual(response.data["state"], BulkStepDecisionStates.DONE)
        self.assertEqual(response.data["decided_count"], 3)
