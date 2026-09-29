"""When a call's evaluation starts: at submission, or for the whole batch at the cut-off."""

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import ProposalRole
from waldur_mastermind.proposal import call_transfer, tasks, utils, workflow_service
from waldur_mastermind.proposal.enums import (
    EvaluationStart,
    ProposalStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.models import (
    CallWorkflowStepNotificationRule,
    Proposal,
    ProposalWorkflowStepInstance,
)
from waldur_mastermind.proposal.tasks import start_evaluation_for_closed_rounds
from waldur_mastermind.proposal.tests import factories, fixtures


class EvaluationStartTestBase(test.APITestCase):
    def setUp(self):
        # Delivery itself is covered by the notification rule tests; here it
        # matters that the step-started event is dispatched, and how often.
        patcher = mock.patch.object(tasks.notify_workflow_step_event, "delay")
        self.dispatch = patcher.start()
        self.addCleanup(patcher.stop)
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.round = self.fixture.round
        self.proposal = self.fixture.proposal
        # Production auto-adds the creator to the team; the factory does not.
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)
        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", duration_in_days=5
        )
        factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", duration_in_days=10
        )
        factories.CallWorkflowStepFactory(
            call=self.call, step="expert_review", is_enabled=False
        )
        # Only the rule under test is live.
        CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.call.workflow_steps.get(step="administrative_check"),
            trigger="step_started",
            recipient="call_managers",
        )
        self.manager = self.fixture.call_manager

    def set_evaluation_start(self, value):
        self.call.evaluation_start = value
        self.call.save(update_fields=["evaluation_start"])

    def submit(self, proposal=None):
        proposal = proposal or self.proposal
        self.client.force_authenticate(proposal.created_by)
        url = factories.ProposalFactory.get_url(proposal, action="submit")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        proposal.refresh_from_db()
        return proposal

    def pass_cutoff(self):
        self.round.cutoff_time = timezone.now() - timedelta(minutes=1)
        self.round.save(update_fields=["cutoff_time"])

    def run_task(self):
        with self.captureOnCommitCallbacks(execute=True):
            return start_evaluation_for_closed_rounds()

    def step_started_dispatches(self):
        return [c for c in self.dispatch.call_args_list if c.args[1] == "step_started"]

    def instances(self, proposal=None):
        return ProposalWorkflowStepInstance.objects.filter(
            proposal=proposal or self.proposal
        )


class OnSubmissionTest(EvaluationStartTestBase):
    def test_default_is_on_submission(self):
        self.assertEqual(self.call.evaluation_start, EvaluationStart.ON_SUBMISSION)

    def test_submit_starts_first_step_at_once(self):
        proposal = self.submit()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(proposal.workflow_step, "administrative_check")
        active = self.instances().get(step="administrative_check")
        self.assertEqual(active.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNotNone(active.deadline)
        self.assertEqual(len(self.step_started_dispatches()), 1)

    def test_submit_updates_modified(self):
        before = self.proposal.modified
        proposal = self.submit()
        self.assertGreater(proposal.modified, before)

    def test_submit_records_submitted_at(self):
        # activate_first_step() saves with update_fields; the timestamp must
        # survive it.
        before = timezone.now()
        proposal = self.submit()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertIsNotNone(proposal.submitted_at)
        self.assertGreaterEqual(proposal.submitted_at, before)

    def test_task_leaves_on_submission_proposals_alone(self):
        self.submit()
        self.pass_cutoff()
        self.dispatch.reset_mock()
        self.assertEqual(self.run_task(), 0)
        self.assertEqual(self.step_started_dispatches(), [])


class AtCutoffSubmitTest(EvaluationStartTestBase):
    def setUp(self):
        super().setUp()
        self.set_evaluation_start(EvaluationStart.AT_CUTOFF)

    def test_submit_leaves_proposal_submitted_with_pending_steps(self):
        proposal = self.submit()
        self.assertEqual(proposal.state, ProposalStates.SUBMITTED)
        self.assertIsNone(proposal.workflow_step)
        instances = self.instances()
        self.assertEqual(
            instances.get(step="administrative_check").status,
            WorkflowStepInstanceStatuses.PENDING,
        )
        self.assertEqual(
            instances.get(step="allocation_decision").status,
            WorkflowStepInstanceStatuses.PENDING,
        )
        self.assertEqual(
            instances.get(step="expert_review").status,
            WorkflowStepInstanceStatuses.SKIPPED,
        )
        self.assertFalse(
            instances.filter(status=WorkflowStepInstanceStatuses.ACTIVE).exists()
        )
        self.assertEqual(self.step_started_dispatches(), [])

    def test_submit_records_submitted_at(self):
        before = timezone.now()
        proposal = self.submit()
        self.assertEqual(proposal.state, ProposalStates.SUBMITTED)
        self.assertIsNotNone(proposal.submitted_at)
        self.assertGreaterEqual(proposal.submitted_at, before)

    def test_task_does_nothing_before_the_cutoff(self):
        self.submit()
        self.assertEqual(self.run_task(), 0)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.SUBMITTED)


class AtCutoffTaskTest(EvaluationStartTestBase):
    def setUp(self):
        super().setUp()
        self.set_evaluation_start(EvaluationStart.AT_CUTOFF)
        self.other = factories.ProposalFactory(round=self.round)
        self.other.add_user(self.other.created_by, ProposalRole.MANAGER)
        self.submit()
        self.submit(self.other)
        self.pass_cutoff()
        self.dispatch.reset_mock()

    def test_cutoff_starts_every_submitted_proposal(self):
        before = timezone.now()
        self.assertEqual(self.run_task(), 2)
        for proposal in (self.proposal, self.other):
            proposal.refresh_from_db()
            self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
            self.assertEqual(proposal.workflow_step, "administrative_check")
            active = self.instances(proposal).get(step="administrative_check")
            self.assertEqual(active.status, WorkflowStepInstanceStatuses.ACTIVE)
            # The deadline runs from activation, not from submission.
            self.assertGreaterEqual(active.started_at, before)
            self.assertEqual(active.deadline, active.started_at + timedelta(days=5))
            self.assertEqual(
                self.instances(proposal).get(step="allocation_decision").status,
                WorkflowStepInstanceStatuses.PENDING,
            )
        self.assertEqual(len(self.step_started_dispatches()), 2)

    def test_cutoff_activation_updates_modified(self):
        self.proposal.refresh_from_db()
        before = self.proposal.modified
        self.run_task()
        self.proposal.refresh_from_db()
        self.assertGreater(self.proposal.modified, before)

    def test_rerun_does_not_reactivate_or_renotify(self):
        self.run_task()
        started_at = self.instances().get(step="administrative_check").started_at
        self.dispatch.reset_mock()

        self.assertEqual(self.run_task(), 0)

        self.assertEqual(self.step_started_dispatches(), [])
        active = self.instances().get(step="administrative_check")
        self.assertEqual(active.started_at, started_at)
        self.assertEqual(
            self.instances().filter(status=WorkflowStepInstanceStatuses.ACTIVE).count(),
            1,
        )

    def test_one_failing_proposal_does_not_stop_the_batch(self):
        original = workflow_service.activate_first_step

        def fail_for_first(proposal):
            if proposal.pk == self.proposal.pk:
                raise RuntimeError("boom")
            return original(proposal)

        with mock.patch.object(
            workflow_service, "activate_first_step", side_effect=fail_for_first
        ):
            self.assertEqual(self.run_task(), 1)

        self.proposal.refresh_from_db()
        self.other.refresh_from_db()
        # The failed proposal is rolled back untouched and retried next run.
        self.assertEqual(self.proposal.state, ProposalStates.SUBMITTED)
        self.assertFalse(
            self.instances().filter(status=WorkflowStepInstanceStatuses.ACTIVE).exists()
        )
        self.assertEqual(self.other.state, ProposalStates.IN_REVIEW)

        self.assertEqual(self.run_task(), 1)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    def test_deferred_proposal_starts_even_if_the_setting_flipped_since(self):
        # A proposal deferred just before the manager switched back must not be
        # stranded in submitted.
        self.set_evaluation_start(EvaluationStart.ON_SUBMISSION)
        self.assertEqual(self.run_task(), 2)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    def test_applicant_is_told_the_proposal_is_in_review(self):
        with mock.patch(
            "waldur_mastermind.proposal.tasks.notify_user_about_proposal_state_update.delay"
        ) as notify:
            self.run_task()
        notify.assert_any_call(
            self.proposal.uuid, ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW
        )
        self.assertEqual(notify.call_count, 2)


class EvaluationStartSettingTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = factories.CallFactory.get_protected_url(self.call)
        self.client.force_authenticate(self.fixture.staff)
        # The fixture seeds a submitted proposal; start with none under evaluation.
        Proposal.objects.filter(round__call=self.call).update(
            state=ProposalStates.DRAFT
        )

    def patch(self, value):
        return self.client.patch(self.url, {"evaluation_start": value}, format="json")

    def test_setting_is_exposed_and_writable(self):
        response = self.client.get(self.url)
        self.assertEqual(response.data["evaluation_start"], "on_submission")

        response = self.patch("at_cutoff")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.call.refresh_from_db()
        self.assertEqual(self.call.evaluation_start, EvaluationStart.AT_CUTOFF)

    def test_applicants_see_the_setting_on_the_public_call(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(factories.CallFactory.get_public_url(self.call))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["evaluation_start"], "on_submission")

    def test_invalid_value_is_rejected(self):
        response = self.patch("whenever")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_refused_while_proposals_are_submitted_or_in_review(self):
        for state in (ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW):
            proposal = factories.ProposalFactory(round=self.fixture.round, state=state)
            response = self.patch("at_cutoff")
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, response.data
            )
            self.assertIn("evaluation_start", response.data)
            self.call.refresh_from_db()
            self.assertEqual(self.call.evaluation_start, EvaluationStart.ON_SUBMISSION)
            proposal.delete()

    def test_change_allowed_with_only_drafts_or_decided_proposals(self):
        for state in (
            ProposalStates.DRAFT,
            ProposalStates.ACCEPTED,
            ProposalStates.REJECTED,
            ProposalStates.CANCELED,
        ):
            factories.ProposalFactory(round=self.fixture.round, state=state)
        response = self.patch("at_cutoff")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_resending_the_current_value_is_not_a_change(self):
        factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.IN_REVIEW
        )
        response = self.patch("on_submission")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_setting_travels_with_export_import_and_duplicate(self):
        self.call.evaluation_start = EvaluationStart.AT_CUTOFF
        self.call.save(update_fields=["evaluation_start"])

        document, _ = call_transfer.export_call(self.call)
        self.assertEqual(document["call"]["evaluation_start"], "at_cutoff")
        imported, _, _ = call_transfer.import_call(
            document, self.fixture.manager, self.fixture.staff, name="Imported"
        )
        self.assertEqual(imported.evaluation_start, EvaluationStart.AT_CUTOFF)

        duplicate = utils.duplicate_call(self.call, "Copy", self.fixture.staff)
        self.assertEqual(duplicate.evaluation_start, EvaluationStart.AT_CUTOFF)
