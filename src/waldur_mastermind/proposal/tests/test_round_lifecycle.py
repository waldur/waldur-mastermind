"""A round's lifecycle after its cut-off, and publishing all its results at once."""

from datetime import date, timedelta
from importlib import import_module
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.contenttypes.models import ContentType
from django.db import connection as db_connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging.models import Event, Feed
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.media import models as media_models
from waldur_core.media.utils import get_image_hash
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, CustomerRole, ProposalRole
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_transfer,
    models,
    tasks,
    utils,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    WORKFLOW_STEPS,
    CallStates,
    EvaluationStart,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    ResultsPublication,
    RoundLifecycleStates,
    TransitionModes,
    UndecidedAtRoundCompletion,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories

backfill = import_module(
    "waldur_mastermind.proposal.migrations.0088_round_lifecycle_publish_results"
)

DECISION_EMAIL = (
    "waldur_mastermind.proposal.tasks.notify_user_about_proposal_state_update.delay"
)
STEP_EVENT = "waldur_mastermind.proposal.tasks.notify_workflow_step_event.delay"
EVENTS_URL = "http://testserver" + reverse("event-list")


class LifecycleTestBase(test.APITestCase):
    publish_results = ResultsPublication.WITH_ROUND

    def setUp(self):
        # The call manager's permissions, as permissions.yaml grants them.
        for permission in (
            PermissionEnum.CLOSE_ROUNDS,
            PermissionEnum.LIST_CALLS,
            PermissionEnum.LIST_PROPOSALS,
            PermissionEnum.LIST_ROUNDS,
            PermissionEnum.UPDATE_CALL,
        ):
            CallRole.MANAGER.add_permission(permission)
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call = factories.CallFactory(
            state=CallStates.ACTIVE, publish_results=self.publish_results
        )
        self.call_manager = structure_factories.UserFactory()
        self.call.add_user(self.call_manager, CallRole.MANAGER)
        now = timezone.now()
        self.round = factories.RoundFactory(
            call=self.call,
            start_time=now - timedelta(days=30),
            cutoff_time=now - timedelta(minutes=1),
        )
        self.round.lifecycle_state = RoundLifecycleStates.EVALUATING
        self.round.save(update_fields=["lifecycle_state"])

    def make_decidable(self, award_response=False):
        """A proposal of the round whose allocation decision is the active step."""
        proposal = factories.ProposalFactory(
            round=self.round,
            state=ProposalStates.IN_REVIEW,
            workflow_step="allocation_decision",
            project=None,
        )
        proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        for step_def in WORKFLOW_STEPS:
            if step_def.id == "administrative_check":
                step_status = WorkflowStepInstanceStatuses.COMPLETED
            elif step_def.id == "allocation_decision":
                step_status = WorkflowStepInstanceStatuses.ACTIVE
            elif step_def.id == "award_response" and award_response:
                step_status = WorkflowStepInstanceStatuses.PENDING
            else:
                step_status = WorkflowStepInstanceStatuses.SKIPPED
            models.ProposalWorkflowStepInstance.objects.create(
                proposal=proposal,
                step=step_def.id,
                status=step_status,
                started_at=timezone.now()
                if step_status == WorkflowStepInstanceStatuses.ACTIVE
                else None,
            )
        return proposal

    def allocation_instance(self, proposal):
        return proposal.workflow_step_instances.get(step="allocation_decision")

    def decide(self, proposal, outcome="approved", user=None):
        self.client.force_authenticate(user or self.call_manager)
        url = factories.ProposalFactory.get_url(
            proposal, action="complete_workflow_step"
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                url,
                {
                    "step_uuid": self.allocation_instance(proposal).uuid.hex,
                    "outcome": outcome,
                    "outcome_reason": "Panel ranking",
                },
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        proposal.refresh_from_db()
        return response

    def round_action(self, action, data=None, user=None):
        self.client.force_authenticate(user or self.call_manager)
        url = factories.RoundFactory.get_url(self.call, self.round, action)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, data or {})
        self.round.refresh_from_db()
        return response

    def publish(self, data=None, user=None):
        return self.round_action("publish_results", data, user)

    def events(self, event_type):
        return Event.objects.filter(event_type=event_type)

    def reopen(self, proposal, data=None, user=None):
        self.client.force_authenticate(user or self.call_manager)
        url = factories.ProposalFactory.get_url(proposal, action="reopen_decision")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                url, {"reason": "Board changed the outcome"} if data is None else data
            )
        proposal.refresh_from_db()
        return response


class ImmediatePublicationTest(LifecycleTestBase):
    publish_results = ResultsPublication.IMMEDIATELY

    def test_new_calls_publish_immediately(self):
        self.assertEqual(
            factories.CallFactory().publish_results, ResultsPublication.IMMEDIATELY
        )

    @mock.patch(DECISION_EMAIL)
    def test_decision_is_announced_at_once(self, decision_email):
        proposal = self.make_decidable()

        self.decide(proposal)

        self.assertEqual(proposal.state, ProposalStates.ACCEPTED)
        self.assertFalse(proposal.decision_held)
        self.assertIsNotNone(proposal.project)
        decision_email.assert_called_once_with(
            proposal.uuid, ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED
        )

    @mock.patch(DECISION_EMAIL)
    def test_publishing_only_records_the_lifecycle(self, decision_email):
        proposal = self.make_decidable()
        self.decide(proposal)
        decision_email.reset_mock()

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )
        decision_email.assert_not_called()

    def test_publishing_needs_no_force_for_proposals_still_in_review(self):
        self.make_decidable()

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )
        self.assertEqual(self.round.results_forced_reason, "")


@mock.patch(STEP_EVENT)
@mock.patch(DECISION_EMAIL)
class HeldDecisionTest(LifecycleTestBase):
    def test_approval_is_recorded_but_held(self, decision_email, step_event):
        proposal = self.make_decidable()

        self.decide(proposal)

        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertTrue(proposal.decision_held)
        self.assertIsNone(proposal.project)
        instance = self.allocation_instance(proposal)
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(instance.outcome, "approved")
        decision_email.assert_not_called()
        step_event.assert_not_called()

    def test_decline_is_recorded_but_held(self, decision_email, step_event):
        proposal = self.make_decidable()

        self.decide(proposal, outcome="declined")

        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertTrue(proposal.decision_held)
        self.assertEqual(self.allocation_instance(proposal).outcome, "declined")
        decision_email.assert_not_called()

    def test_rejection_at_the_decision_is_held(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.client.force_authenticate(self.call_manager)
        url = factories.ProposalFactory.get_url(proposal, action="reject_workflow_step")

        response = self.client.post(
            url,
            {
                "step_uuid": self.allocation_instance(proposal).uuid.hex,
                "reason": "Below the line",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertTrue(proposal.decision_held)

    def test_applicant_sees_the_decision_as_under_evaluation(
        self, decision_email, step_event
    ):
        proposal = self.make_decidable()
        self.decide(proposal)

        self.client.force_authenticate(proposal.created_by)
        response = self.client.get(
            factories.ProposalFactory.get_url(proposal, action="workflow_states")
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        step = next(s for s in response.data if s["step"] == "allocation_decision")
        self.assertEqual(step["status"], WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNone(step["outcome"])
        self.assertEqual(step["outcome_reason"], "")
        self.assertIsNone(step["completed_at"])
        self.assertEqual(
            self.client.get(factories.ProposalFactory.get_url(proposal)).data["state"],
            ProposalStates.IN_REVIEW,
        )

    def test_call_manager_sees_the_tentative_outcome(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            factories.ProposalFactory.get_url(proposal, action="workflow_states")
        )

        step = next(s for s in response.data if s["step"] == "allocation_decision")
        self.assertEqual(step["status"], WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(step["outcome"], "approved")

    def test_reviewer_does_not_see_the_tentative_outcome(
        self, decision_email, step_event
    ):
        proposal = self.make_decidable()
        reviewer = structure_factories.UserFactory()
        self.call.add_user(reviewer, CallRole.REVIEWER)
        factories.ReviewFactory(proposal=proposal, reviewer=reviewer)
        self.decide(proposal)

        self.client.force_authenticate(reviewer)
        response = self.client.get(
            factories.ProposalFactory.get_url(proposal, action="workflow_states")
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        step = next(s for s in response.data if s["step"] == "allocation_decision")
        self.assertEqual(step["status"], WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNone(step["outcome"])

    def test_only_the_call_manager_is_told_the_decision_is_held(
        self, decision_email, step_event
    ):
        proposal = self.make_decidable()
        self.decide(proposal)
        url = factories.ProposalFactory.get_url(proposal)

        self.client.force_authenticate(self.call_manager)
        self.assertTrue(self.client.get(url).data["decision_held"])
        self.client.force_authenticate(proposal.created_by)
        self.assertIsNone(self.client.get(url).data["decision_held"])

    def test_held_decision_cannot_be_advanced_by_hand(self, decision_email, step_event):
        models.CallWorkflowStep.objects.filter(
            call=self.call, step="allocation_decision"
        ).update(transition_mode=TransitionModes.MANUAL)
        proposal = self.make_decidable()
        self.decide(proposal)
        self.client.force_authenticate(self.call_manager)

        response = self.client.post(
            factories.ProposalFactory.get_url(proposal, action="advance_workflow_step")
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertFalse(
            self.client.get(factories.ProposalFactory.get_url(proposal)).data[
                "awaiting_manual_advance"
            ]
        )

    def test_publish_sends_one_email_per_decided_proposal(
        self, decision_email, step_event
    ):
        awarded = self.make_decidable()
        declined = self.make_decidable()
        self.decide(awarded)
        self.decide(declined, outcome="declined")

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        awarded.refresh_from_db()
        declined.refresh_from_db()
        self.assertEqual(awarded.state, ProposalStates.ACCEPTED)
        self.assertIsNotNone(awarded.project)
        self.assertEqual(declined.state, ProposalStates.REJECTED)
        self.assertFalse(awarded.decision_held or declined.decision_held)
        self.assertCountEqual(
            [c.args for c in decision_email.call_args_list],
            [
                (awarded.uuid, ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED),
                (declined.uuid, ProposalStates.IN_REVIEW, ProposalStates.REJECTED),
            ],
        )
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )
        self.assertIsNotNone(self.round.results_published_at)
        self.assertEqual(self.round.results_published_by, self.call_manager)
        self.assertEqual(self.events("round_results_published").count(), 1)

        # Publishing twice sends nothing more.
        decision_email.reset_mock()
        response = self.publish()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        decision_email.assert_not_called()

    def test_publish_continues_awards_to_the_award_response(
        self, decision_email, step_event
    ):
        proposal = self.make_decidable(award_response=True)
        self.decide(proposal)

        self.publish()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(proposal.workflow_step, "award_response")
        self.assertEqual(
            proposal.workflow_step_instances.get(step="award_response").status,
            WorkflowStepInstanceStatuses.ACTIVE,
        )
        decision_email.assert_not_called()

    def test_publish_advances_a_manual_decision(self, decision_email, step_event):
        models.CallWorkflowStep.objects.filter(
            call=self.call, step="allocation_decision"
        ).update(transition_mode=TransitionModes.MANUAL)
        proposal = self.make_decidable()
        self.decide(proposal)

        self.publish()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.ACCEPTED)
        decision_email.assert_called_once()

    def decided_before_the_switch(self):
        """A proposal whose approval was announced at once and now awaits the
        applicant's award response: decided, though still in review."""
        proposal = self.make_decidable(award_response=True)
        decision = self.allocation_instance(proposal)
        decision.status = WorkflowStepInstanceStatuses.COMPLETED
        decision.outcome = "approved"
        decision.completed_at = timezone.now()
        decision.save()
        award = proposal.workflow_step_instances.get(step="award_response")
        award.status = WorkflowStepInstanceStatuses.ACTIVE
        award.started_at = timezone.now()
        award.save()
        proposal.workflow_step = "award_response"
        proposal.save(update_fields=["workflow_step"])
        return proposal

    def test_proposal_past_its_decision_is_not_undecided(
        self, decision_email, step_event
    ):
        self.decided_before_the_switch()
        held = self.make_decidable()
        self.decide(held)
        self.make_decidable()

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["undecided_count"], 1)

    def test_publish_needs_no_force_when_the_rest_is_past_its_decision(
        self, decision_email, step_event
    ):
        awaiting = self.decided_before_the_switch()
        held = self.make_decidable()
        self.decide(held)

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.round.results_forced_reason, "")
        awaiting.refresh_from_db()
        self.assertEqual(awaiting.state, ProposalStates.IN_REVIEW)
        self.assertEqual(awaiting.workflow_step, "award_response")
        held.refresh_from_db()
        self.assertEqual(held.state, ProposalStates.ACCEPTED)

    def test_publish_is_refused_while_proposals_are_undecided(
        self, decision_email, step_event
    ):
        decided = self.make_decidable()
        self.decide(decided)
        self.make_decidable()

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["undecided_count"], 1)
        self.assertIn("no decision yet", response.data["detail"])
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.EVALUATING)
        decided.refresh_from_db()
        self.assertTrue(decided.decision_held)
        decision_email.assert_not_called()

    def test_forcing_needs_a_reason(self, decision_email, step_event):
        self.make_decidable()

        response = self.publish({"force": True})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.EVALUATING)

    def test_forced_publish_releases_decided_and_leaves_the_rest(
        self, decision_email, step_event
    ):
        decided = self.make_decidable()
        self.decide(decided)
        undecided = self.make_decidable()

        response = self.publish({"force": True, "reason": "Board adopted the list"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )
        self.assertEqual(self.round.results_forced_reason, "Board adopted the list")
        decided.refresh_from_db()
        self.assertEqual(decided.state, ProposalStates.ACCEPTED)
        undecided.refresh_from_db()
        self.assertEqual(undecided.state, ProposalStates.IN_REVIEW)

        # Decided after publication: announced at once.
        decision_email.reset_mock()
        self.decide(undecided)
        self.assertEqual(undecided.state, ProposalStates.ACCEPTED)
        decision_email.assert_called_once()

    def test_publish_before_the_cutoff_is_refused(self, decision_email, step_event):
        self.round.cutoff_time = timezone.now() + timedelta(days=1)
        self.round.lifecycle_state = None
        self.round.save(update_fields=["cutoff_time", "lifecycle_state"])

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(self.round.lifecycle_state)

    def test_only_round_closers_may_publish(self, decision_email, step_event):
        outsider = structure_factories.UserFactory()

        response = self.publish(user=outsider)

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.EVALUATING)

    def test_setting_is_locked_while_decisions_are_held(
        self, decision_email, step_event
    ):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.client.force_authenticate(self.call_manager)

        response = self.client.patch(
            factories.CallFactory.get_protected_url(self.call),
            {"publish_results": ResultsPublication.IMMEDIATELY},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.call.refresh_from_db()
        self.assertEqual(self.call.publish_results, ResultsPublication.WITH_ROUND)

    def test_a_failing_release_does_not_hold_back_the_rest(
        self, decision_email, step_event
    ):
        failing = self.make_decidable()
        released = self.make_decidable()
        self.decide(failing)
        self.decide(released)
        allocate = utils.allocate_proposal

        def allocate_or_fail(proposal, *args, **kwargs):
            if proposal.pk == failing.pk:
                raise RuntimeError("Provisioning failed")
            return allocate(proposal, *args, **kwargs)

        with mock.patch.object(
            utils, "allocate_proposal", side_effect=allocate_or_fail
        ):
            response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [item["uuid"] for item in response.data["failed_proposals"]],
            [failing.uuid.hex],
        )
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )
        released.refresh_from_db()
        failing.refresh_from_db()
        self.assertEqual(released.state, ProposalStates.ACCEPTED)
        self.assertEqual(failing.state, ProposalStates.IN_REVIEW)
        self.assertTrue(failing.decision_held)
        self.assertIsNone(failing.project)
        decision_email.assert_called_once_with(
            released.uuid, ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED
        )

        # Publishing again releases what is still held, and only that.
        decision_email.reset_mock()
        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["failed_proposals"], [])
        failing.refresh_from_db()
        self.assertEqual(failing.state, ProposalStates.ACCEPTED)
        decision_email.assert_called_once_with(
            failing.uuid, ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED
        )
        self.assertEqual(self.events("round_results_published").count(), 1)


@mock.patch(STEP_EVENT)
@mock.patch(DECISION_EMAIL)
class LapsedDecisionTest(LifecycleTestBase):
    """A decision step whose deadline passes is held like any other decision."""

    def setUp(self):
        super().setUp()
        call_step = models.CallWorkflowStep.objects.filter(
            call=self.call, step="allocation_decision"
        ).first() or factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision"
        )
        # The applicant is told when the decision lapses.
        models.CallWorkflowStepNotificationRule.objects.get_or_create(
            workflow_step=call_step,
            trigger=NotificationRuleTriggers.STEP_EXPIRED,
            recipient=NotificationRuleRecipients.APPLICANT,
        )

    def lapse(self, proposal):
        instance = self.allocation_instance(proposal)
        instance.deadline = timezone.now() - timedelta(minutes=1)
        instance.save(update_fields=["deadline"])
        with self.captureOnCommitCallbacks(execute=True):
            tasks.mark_expired_workflow_steps()
        proposal.refresh_from_db()

    def expiry_notices(self, step_event, proposal):
        return [
            c
            for c in step_event.call_args_list
            if c.args
            == (
                self.allocation_instance(proposal).uuid.hex,
                NotificationRuleTriggers.STEP_EXPIRED,
            )
        ]

    def test_lapse_before_the_award_response_is_held(self, decision_email, step_event):
        proposal = self.make_decidable(award_response=True)

        self.lapse(proposal)

        self.assertTrue(proposal.decision_held)
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(proposal.workflow_step, "allocation_decision")
        self.assertEqual(
            proposal.workflow_step_instances.get(step="award_response").status,
            WorkflowStepInstanceStatuses.PENDING,
        )
        self.assertEqual(self.expiry_notices(step_event, proposal), [])

    def test_publish_carries_a_lapse_on_to_the_award_response(
        self, decision_email, step_event
    ):
        proposal = self.make_decidable(award_response=True)
        self.lapse(proposal)

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        proposal.refresh_from_db()
        self.assertFalse(proposal.decision_held)
        self.assertEqual(proposal.workflow_step, "award_response")
        self.assertEqual(
            proposal.workflow_step_instances.get(step="award_response").status,
            WorkflowStepInstanceStatuses.ACTIVE,
        )
        self.assertEqual(len(self.expiry_notices(step_event, proposal)), 1)

    def test_lapse_at_the_end_is_told_on_publication(self, decision_email, step_event):
        proposal = self.make_decidable()

        self.lapse(proposal)

        self.assertTrue(proposal.decision_held)
        self.assertEqual(self.expiry_notices(step_event, proposal), [])

        self.publish()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.REJECTED)
        self.assertEqual(len(self.expiry_notices(step_event, proposal)), 1)
        decision_email.assert_called_once_with(
            proposal.uuid, ProposalStates.IN_REVIEW, ProposalStates.REJECTED
        )

    def test_held_lapse_reopened_with_a_new_deadline(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.lapse(proposal)
        deadline = timezone.now() + timedelta(days=7)

        response = self.reopen(
            proposal,
            {"reason": "Board decides after all", "deadline": deadline.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(proposal.decision_held)
        instance = self.allocation_instance(proposal)
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNone(instance.outcome)
        self.assertIsNone(instance.completed_at)
        self.assertEqual(instance.deadline, deadline)
        self.assertNotIn(
            "previous_outcome",
            self.events("proposal_decision_reopened").get().context,
        )

        # The sweep leaves it alone until the new deadline.
        with self.captureOnCommitCallbacks(execute=True):
            tasks.mark_expired_workflow_steps()
        self.assertEqual(
            self.allocation_instance(proposal).status,
            WorkflowStepInstanceStatuses.ACTIVE,
        )
        self.assertEqual(self.expiry_notices(step_event, proposal), [])

    def test_held_lapse_reopened_without_a_deadline_has_none(
        self, decision_email, step_event
    ):
        proposal = self.make_decidable()
        self.lapse(proposal)

        response = self.reopen(proposal)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        instance = self.allocation_instance(proposal)
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNone(instance.deadline)


@mock.patch(STEP_EVENT)
@mock.patch(DECISION_EMAIL)
class ReopenHeldDecisionTest(LifecycleTestBase):
    """A call manager takes a held decision back before the round publishes."""

    def test_call_manager_reopens_a_held_approval(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)

        response = self.reopen(proposal)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(proposal.decision_held)
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(proposal.workflow_step, "allocation_decision")
        instance = self.allocation_instance(proposal)
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNone(instance.outcome)
        self.assertEqual(instance.outcome_reason, "")
        self.assertIsNone(instance.completed_at)
        self.assertIsNone(instance.completed_by)
        self.assertEqual(response.data["status"], WorkflowStepInstanceStatuses.ACTIVE)
        decision_email.assert_not_called()
        step_event.assert_not_called()

        (event,) = self.events("proposal_decision_reopened")
        self.assertEqual(event.context["actor"], self.call_manager.full_name)
        self.assertEqual(event.context["proposal_uuid"], proposal.uuid.hex)
        # The proposal's feed is read by its applicant team too: neither the
        # outcome taken back nor the free-text reason may be in the event.
        self.assertNotIn("previous_outcome", event.context)
        self.assertNotIn("reason", event.context)
        self.assertNotIn("Board changed the outcome", event.message)
        self.assertNotIn("approved", event.message)
        self.assertEqual(
            [feed.scope for feed in Feed.objects.filter(event=event)],
            [proposal],
        )

    def test_reopened_decision_counts_as_undecided(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.reopen(proposal)

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.EVALUATING)

    def test_decision_made_again_is_held_and_published_alone(
        self, decision_email, step_event
    ):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.reopen(proposal)

        self.decide(proposal, outcome="declined")

        self.assertTrue(proposal.decision_held)
        self.assertEqual(proposal.state, ProposalStates.IN_REVIEW)
        decision_email.assert_not_called()

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.REJECTED)
        self.assertIsNone(proposal.project)
        decision_email.assert_called_once_with(
            proposal.uuid, ProposalStates.IN_REVIEW, ProposalStates.REJECTED
        )

    def test_reopening_a_held_rejection(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.client.force_authenticate(self.call_manager)
        self.client.post(
            factories.ProposalFactory.get_url(proposal, action="reject_workflow_step"),
            {
                "step_uuid": self.allocation_instance(proposal).uuid.hex,
                "reason": "Below the line",
            },
        )

        response = self.reopen(proposal)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(proposal.decision_held)
        self.assertNotIn(
            "previous_outcome",
            self.events("proposal_decision_reopened").get().context,
        )

    def test_deadline_still_ahead_is_kept(self, decision_email, step_event):
        proposal = self.make_decidable()
        deadline = timezone.now() + timedelta(days=5)
        instance = self.allocation_instance(proposal)
        instance.deadline = deadline
        instance.save(update_fields=["deadline"])
        self.decide(proposal)

        self.reopen(proposal)

        self.assertEqual(self.allocation_instance(proposal).deadline, deadline)

    def test_a_reason_is_required(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)

        for data in ({}, {"reason": "  "}):
            response = self.reopen(proposal, data)

            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn("reason", response.data)
            self.assertTrue(proposal.decision_held)

    def test_a_new_deadline_must_be_ahead(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)

        response = self.reopen(
            proposal,
            {
                "reason": "Board changed the outcome",
                "deadline": (timezone.now() - timedelta(days=1)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("deadline", response.data)
        self.assertTrue(proposal.decision_held)

    def test_organiser_of_the_managing_organisation_may_reopen(
        self, decision_email, step_event
    ):
        organiser_role = Role.objects.get_system_role(
            "CUSTOMER.CALL_ORGANIZER",
            content_type=ContentType.objects.get_for_model(
                models.CallManagingOrganisation
            ),
        )
        organiser_role.add_permission(PermissionEnum.UPDATE_CALL)
        organiser = structure_factories.UserFactory()
        self.call.manager.add_user(organiser, organiser_role)
        proposal = self.make_decidable()
        self.decide(proposal)

        response = self.reopen(proposal, user=organiser)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(proposal.decision_held)

    def test_staff_may_reopen(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)

        response = self.reopen(proposal, user=self.staff)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def assert_refused(self, proposal, user):
        response = self.reopen(proposal, user=user)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(proposal.decision_held)
        self.assertFalse(self.events("proposal_decision_reopened").exists())

    def test_reviewer_may_not_reopen(self, decision_email, step_event):
        proposal = self.make_decidable()
        reviewer = structure_factories.UserFactory()
        self.call.add_user(reviewer, CallRole.REVIEWER)
        factories.ReviewFactory(proposal=proposal, reviewer=reviewer)
        self.decide(proposal)

        self.assert_refused(proposal, reviewer)

    def test_support_may_not_reopen(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)

        self.assert_refused(proposal, structure_factories.UserFactory(is_support=True))

    def test_applicant_who_manages_the_call_may_not_reopen(
        self, decision_email, step_event
    ):
        proposal = self.make_decidable()
        self.call.add_user(proposal.created_by, CallRole.MANAGER)
        self.decide(proposal)

        self.assert_refused(proposal, proposal.created_by)

    def test_decision_not_held_cannot_be_reopened(self, decision_email, step_event):
        proposal = self.make_decidable()

        response = self.reopen(proposal)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            self.allocation_instance(proposal).status,
            WorkflowStepInstanceStatuses.ACTIVE,
        )
        self.assertFalse(self.events("proposal_decision_reopened").exists())

    def test_published_decision_cannot_be_reopened(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.publish()

        response = self.reopen(proposal)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.ACCEPTED)

    def test_decision_still_held_after_publication_cannot_be_reopened(
        self, decision_email, step_event
    ):
        # A release that failed leaves the decision held after publication:
        # the round has spoken, so publishing again is the only way on.
        proposal = self.make_decidable()
        self.decide(proposal)
        with mock.patch.object(
            utils, "allocate_proposal", side_effect=RuntimeError("Provisioning failed")
        ):
            self.publish()
        proposal.refresh_from_db()
        self.assertTrue(proposal.decision_held)

        response = self.reopen(proposal)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        proposal.refresh_from_db()
        self.assertTrue(proposal.decision_held)
        self.assertEqual(
            self.allocation_instance(proposal).status,
            WorkflowStepInstanceStatuses.COMPLETED,
        )


@mock.patch(STEP_EVENT)
@mock.patch(DECISION_EMAIL)
class ReopenedDecisionEventVisibilityTest(LifecycleTestBase):
    """Who reads the event a reopened held decision leaves behind.

    That a decision was reopened tells that one had been made, so until the
    round publishes its results the event reaches only those who may see held
    decisions: not the managing organisation's owners, not the call's
    reviewers, not the applicant team -- by the events API or by a hook. Once
    the results are out the applicant team reads it on the proposal's feed.
    """

    def setUp(self):
        super().setUp()
        self.proposal = self.make_decidable()
        self.decide(self.proposal)
        self.reopen(self.proposal)
        self.event = self.events("proposal_decision_reopened").get()
        self.customer = self.call.manager.customer

    def publish_the_round(self):
        self.decide(self.proposal)
        response = self.publish()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def feed(self, user, scope_url):
        self.client.force_authenticate(user)
        response = self.client.get(
            EVENTS_URL,
            {"scope": scope_url, "event_type": "proposal_decision_reopened"},
        )
        # A scope the user cannot see at all is refused as an invalid link.
        if response.status_code == status.HTTP_400_BAD_REQUEST:
            return []
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        count = self.client.get(
            EVENTS_URL + "count/",
            {"scope": scope_url, "event_type": "proposal_decision_reopened"},
        )
        self.assertEqual(count.status_code, status.HTTP_200_OK)
        self.assertEqual(count.data["count"], len(response.data))
        return response.data

    def proposal_feed(self, user):
        return self.feed(user, factories.ProposalFactory.get_url(self.proposal))

    def unfiltered_proposal_feed(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            EVENTS_URL, {"scope": factories.ProposalFactory.get_url(self.proposal)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [e for e in response.data if e["event_type"] == self.event.event_type]

    def visible_events(self, user):
        found = []
        for scope_url in (
            factories.ProposalFactory.get_url(self.proposal),
            structure_factories.CustomerFactory.get_url(self.customer),
        ):
            found.extend(self.feed(user, scope_url))
        return found

    def hook_matches(self, user):
        hook = logging_factories.WebHookFactory(
            user=user, event_types=["proposal_decision_reopened"]
        )
        return logging_tasks.check_event(self.event, hook)

    def assert_reads_it(self, user):
        (event,) = self.proposal_feed(user)
        self.assertEqual(event["uuid"], self.event.uuid.hex)
        self.assertTrue(self.hook_matches(user))

    def assert_does_not_read_it(self, user):
        self.assertEqual(self.visible_events(user), [])
        self.assertFalse(self.hook_matches(user))

    def assert_outcome_hidden(self, user):
        for event in self.visible_events(user):
            self.assertNotIn("previous_outcome", event["context"])
            self.assertNotIn("reason", event["context"])
            self.assertNotIn("approved", event["message"])
            self.assertNotIn("Board changed the outcome", event["message"])

    def applicant_team(self):
        member = structure_factories.UserFactory()
        self.proposal.add_user(member, ProposalRole.MEMBER)
        return [self.proposal.created_by, member]

    def call_organiser(self):
        organiser_role = Role.objects.get_system_role(
            "CUSTOMER.CALL_ORGANIZER",
            content_type=ContentType.objects.get_for_model(
                models.CallManagingOrganisation
            ),
        )
        organiser_role.add_permission(PermissionEnum.UPDATE_CALL)
        organiser = structure_factories.UserFactory()
        self.call.manager.add_user(organiser, organiser_role)
        return organiser

    def test_owner_of_the_managing_organisation_does_not_read_it(self, *_):
        owner = structure_factories.UserFactory()
        self.customer.add_user(owner, CustomerRole.OWNER)

        self.assert_does_not_read_it(owner)

    def test_applicant_team_does_not_read_it_until_the_round_publishes(self, *_):
        team = self.applicant_team()

        for user in team:
            with self.subTest(user=user.username):
                self.assert_does_not_read_it(user)
                self.assertEqual(self.unfiltered_proposal_feed(user), [])

        self.publish_the_round()

        for user in team:
            with self.subTest(user=user.username):
                self.assert_reads_it(user)
                self.assert_outcome_hidden(user)

    def test_applicant_owning_the_managing_organisation_reads_it_only_once_published(
        self, *_
    ):
        applicant = self.proposal.created_by
        self.customer.add_user(applicant, CustomerRole.OWNER)

        self.assert_does_not_read_it(applicant)

        self.publish_the_round()

        self.assert_reads_it(applicant)
        self.assertEqual(
            self.feed(
                applicant, structure_factories.CustomerFactory.get_url(self.customer)
            ),
            [],
        )
        self.assert_outcome_hidden(applicant)

    def test_applicant_who_manages_the_call_does_not_read_it_until_published(self, *_):
        applicant = self.proposal.created_by
        self.call.add_user(applicant, CallRole.MANAGER)

        self.assert_does_not_read_it(applicant)

        self.publish_the_round()

        self.assert_reads_it(applicant)

    def test_reviewer_never_reads_it(self, *_):
        reviewer = structure_factories.UserFactory()
        self.call.add_user(reviewer, CallRole.REVIEWER)
        factories.ReviewFactory(proposal=self.proposal, reviewer=reviewer)

        self.assert_does_not_read_it(reviewer)

        self.publish_the_round()

        self.assert_does_not_read_it(reviewer)

    def test_applicant_team_never_reads_the_outcome(self, *_):
        self.publish_the_round()

        for user in self.applicant_team():
            self.assert_outcome_hidden(user)
        self.assertNotIn("previous_outcome", self.event.context)
        self.assertNotIn("reason", self.event.context)

    def test_call_managers_staff_and_support_always_read_it(self, *_):
        organiser = self.call_organiser()
        support = structure_factories.UserFactory(is_support=True)

        for published in (False, True):
            if published:
                self.publish_the_round()
            for user in (self.call_manager, organiser, self.staff, support):
                with self.subTest(user=user.username, published=published):
                    self.assert_reads_it(user)

            self.client.force_authenticate(self.staff)
            response = self.client.get(
                EVENTS_URL, {"event_type": "proposal_decision_reopened"}
            )
            self.assertEqual(len(response.data), 1)


@mock.patch(STEP_EVENT)
@mock.patch(DECISION_EMAIL)
class HeldDecisionsCountTest(LifecycleTestBase):
    """Round managers see how many decisions of a round are still held."""

    def rounds_url(self):
        return factories.CallFactory.get_protected_url(self.call, action="rounds")

    def held_count(self):
        self.client.force_authenticate(self.call_manager)
        detail = self.client.get(factories.RoundFactory.get_url(self.call, self.round))
        self.assertEqual(detail.status_code, status.HTTP_200_OK, detail.data)
        listed = self.client.get(self.rounds_url())
        self.assertEqual(listed.status_code, status.HTTP_200_OK, listed.data)
        (row,) = [r for r in listed.data if r["uuid"] == self.round.uuid.hex]
        self.assertEqual(
            row["held_decisions_count"], detail.data["held_decisions_count"]
        )
        return detail.data["held_decisions_count"]

    def test_counts_held_decisions_until_they_are_published(
        self, decision_email, step_event
    ):
        self.assertEqual(self.held_count(), 0)
        first = self.make_decidable()
        second = self.make_decidable()
        self.decide(first)
        self.decide(second)

        self.assertEqual(self.held_count(), 2)

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["held_decisions_count"], 0)
        self.assertEqual(self.held_count(), 0)

    def test_a_reopened_decision_is_no_longer_held(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.reopen(proposal)

        self.assertEqual(self.held_count(), 0)

    def test_failed_releases_stay_counted_after_publication(
        self, decision_email, step_event
    ):
        failing = self.make_decidable()
        released = self.make_decidable()
        self.decide(failing)
        self.decide(released)
        allocate = utils.allocate_proposal

        def allocate_or_fail(proposal, *args, **kwargs):
            if proposal.pk == failing.pk:
                raise RuntimeError("Provisioning failed")
            return allocate(proposal, *args, **kwargs)

        with mock.patch.object(
            utils, "allocate_proposal", side_effect=allocate_or_fail
        ):
            response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )
        self.assertEqual(response.data["held_decisions_count"], 1)
        self.assertEqual(self.held_count(), 1)

        self.publish()

        self.assertEqual(self.held_count(), 0)

    def test_evaluator_is_not_told_how_many_decisions_are_held(
        self, decision_email, step_event
    ):
        # To anyone but the call managers a held decision is still being made,
        # so the round does not tell them how many are held -- not even zero.
        proposal = self.make_decidable()
        reviewer = structure_factories.UserFactory()
        self.call.add_user(reviewer, CallRole.REVIEWER)
        factories.ReviewFactory(proposal=proposal, reviewer=reviewer)
        self.decide(proposal)

        self.client.force_authenticate(reviewer)
        detail = self.client.get(factories.RoundFactory.get_url(self.call, self.round))
        listed = self.client.get(self.rounds_url())

        self.assertEqual(detail.status_code, status.HTTP_200_OK, detail.data)
        self.assertIsNone(detail.data["held_decisions_count"])
        self.assertEqual(listed.status_code, status.HTTP_200_OK, listed.data)
        (row,) = [r for r in listed.data if r["uuid"] == self.round.uuid.hex]
        self.assertIsNone(row["held_decisions_count"])

    def test_not_shown_on_public_rounds(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.client.force_authenticate(proposal.created_by)

        response = self.client.get(factories.CallFactory.get_public_url(self.call))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["rounds"])
        for call_round in response.data["rounds"]:
            self.assertNotIn("held_decisions_count", call_round)

        response = self.client.get(factories.ProposalFactory.get_url(proposal))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn("held_decisions_count", response.data["round"])

    def test_round_list_does_not_run_a_query_per_round(
        self, decision_email, step_event
    ):
        self.decide(self.make_decidable())
        self.client.force_authenticate(self.call_manager)
        url = self.rounds_url()

        # Warm up one-off lookups so the measurements differ only in row count.
        self.client.get(url)
        with CaptureQueriesContext(db_connection) as ctx_one:
            self.client.get(url)

        now = timezone.now()
        for offset in range(1, 4):
            factories.RoundFactory(
                call=self.call,
                start_time=now - timedelta(days=60 * offset + 30),
                cutoff_time=now - timedelta(days=60 * offset),
            )

        with CaptureQueriesContext(db_connection) as ctx_many:
            response = self.client.get(url)

        self.assertEqual(len(response.data), 4)
        self.assertEqual(
            [r["held_decisions_count"] for r in response.data], [1, 0, 0, 0]
        )
        self.assertEqual(len(ctx_one), len(ctx_many))


@mock.patch(STEP_EVENT)
@mock.patch(DECISION_EMAIL)
class RoundTransitionsTest(LifecycleTestBase):
    def test_evaluating_to_deciding(self, decision_email, step_event):
        response = self.round_action("start_deciding")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.DECIDING)
        self.assertIsNotNone(self.round.deciding_started_at)
        self.assertEqual(self.events("round_decision_started").count(), 1)
        self.assertEqual(
            response.data["lifecycle_state"], RoundLifecycleStates.DECIDING
        )

    def test_publish_from_deciding(self, decision_email, step_event):
        self.round_action("start_deciding")

        response = self.publish()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )

    def test_complete_after_publication(self, decision_email, step_event):
        self.publish()

        response = self.round_action("complete")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.CLOSED)
        self.assertIsNotNone(self.round.closed_at)
        self.assertEqual(self.events("round_closed").count(), 1)

    def test_complete_before_publication_is_refused(self, decision_email, step_event):
        response = self.round_action("complete")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.EVALUATING)

    def test_deciding_after_publication_is_refused(self, decision_email, step_event):
        self.publish()

        response = self.round_action("start_deciding")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )

    def test_round_exposes_its_lifecycle(self, decision_email, step_event):
        self.client.force_authenticate(self.call_manager)

        response = self.client.get(
            factories.RoundFactory.get_url(self.call, self.round)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ended")
        self.assertEqual(
            response.data["lifecycle_state"], RoundLifecycleStates.EVALUATING
        )
        for field in (
            "evaluation_started_at",
            "deciding_started_at",
            "results_published_at",
            "closed_at",
            "adopted_at",
            "adoption_note",
        ):
            self.assertIn(field, response.data)

    def test_adoption_is_recorded_on_the_round(self, decision_email, step_event):
        response = self.round_action(
            "record_adoption",
            {"adopted_at": "2026-09-15", "adoption_note": "Governing Board decision"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(str(response.data["adopted_at"]), "2026-09-15")
        self.assertEqual(self.round.adopted_at, date(2026, 9, 15))
        self.assertEqual(self.round.adoption_note, "Governing Board decision")

    def test_adoption_needs_its_date(self, decision_email, step_event):
        response = self.round_action(
            "record_adoption", {"adoption_note": "Governing Board decision"}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("adopted_at", response.data)
        self.assertEqual(self.round.adoption_note, "")


class AdoptionRecordVisibilityTest(LifecycleTestBase):
    """The adoption record states the round's outcome: until the results are
    published only those who may see held decisions read it."""

    FIELDS = ("adoption_note", "adoption_document", "results_forced_reason")

    def setUp(self):
        super().setUp()
        self.reviewer = structure_factories.UserFactory()
        self.call.add_user(self.reviewer, CallRole.REVIEWER)
        content = b"%PDF-1.4\n"
        self.document = media_models.File.objects.create(
            name="proposal_round_adoption/board-decision.pdf",
            content=content,
            size=len(content),
            mime_type="application/pdf",
            hash=get_image_hash(content),
        )
        self.round.adoption_note = "Governing Board adopted the list"
        self.round.adoption_document = self.document.name
        self.round.results_forced_reason = "Two proposals left undecided"
        self.round.save(
            update_fields=[
                "adoption_note",
                "adoption_document",
                "results_forced_reason",
            ]
        )

    def round_rows(self, user):
        self.client.force_authenticate(user)
        detail = self.client.get(factories.RoundFactory.get_url(self.call, self.round))
        self.assertEqual(detail.status_code, status.HTTP_200_OK, detail.data)
        listed = self.client.get(
            factories.CallFactory.get_protected_url(self.call, action="rounds")
        )
        self.assertEqual(listed.status_code, status.HTTP_200_OK, listed.data)
        (row,) = [r for r in listed.data if r["uuid"] == self.round.uuid.hex]
        return detail.data, row

    def download(self, user):
        self.client.force_authenticate(user)
        return self.client.get(reverse("media", kwargs={"uuid": self.document.uuid}))

    def assert_hidden(self, user):
        for data in self.round_rows(user):
            for field in self.FIELDS:
                self.assertIsNone(data[field], field)
        self.assertIn(
            self.download(user).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def assert_shown(self, user):
        for data in self.round_rows(user):
            self.assertEqual(data["adoption_note"], "Governing Board adopted the list")
            self.assertEqual(
                data["results_forced_reason"], "Two proposals left undecided"
            )
            self.assertTrue(data["adoption_document"])
        self.assertEqual(self.download(user).status_code, status.HTTP_200_OK)

    def test_reviewer_does_not_read_it_before_publication(self):
        for state in (RoundLifecycleStates.EVALUATING, RoundLifecycleStates.DECIDING):
            with self.subTest(state=state):
                self.round.lifecycle_state = state
                self.round.save(update_fields=["lifecycle_state"])
                self.assert_hidden(self.reviewer)

    def test_reviewer_reads_it_once_results_are_published(self):
        for state in RoundLifecycleStates.PUBLISHED:
            with self.subTest(state=state):
                self.round.lifecycle_state = state
                self.round.save(update_fields=["lifecycle_state"])
                self.assert_shown(self.reviewer)

    def test_call_manager_always_reads_it(self):
        for state in (
            RoundLifecycleStates.EVALUATING,
            RoundLifecycleStates.DECIDING,
            RoundLifecycleStates.RESULTS_PUBLISHED,
        ):
            with self.subTest(state=state):
                self.round.lifecycle_state = state
                self.round.save(update_fields=["lifecycle_state"])
                self.assert_shown(self.call_manager)


class EvaluationStartsAtTheCutoffTest(test.APITestCase):
    def setUp(self):
        self.call = factories.CallFactory(state=CallStates.ACTIVE)
        now = timezone.now()
        self.round = factories.RoundFactory(
            call=self.call,
            start_time=now - timedelta(days=30),
            cutoff_time=now + timedelta(days=1),
        )

    def end_round(self):
        self.round.cutoff_time = timezone.now() - timedelta(minutes=1)
        self.round.save(update_fields=["cutoff_time"])

    def test_open_round_has_no_lifecycle_yet(self):
        tasks.proposals_for_ended_rounds_should_be_cancelled()
        self.round.refresh_from_db()
        self.assertIsNone(self.round.lifecycle_state)

    def test_round_end_sweep_starts_the_evaluation(self):
        self.end_round()

        tasks.proposals_for_ended_rounds_should_be_cancelled()
        tasks.proposals_for_ended_rounds_should_be_cancelled()

        self.round.refresh_from_db()
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.EVALUATING)
        self.assertIsNotNone(self.round.evaluation_started_at)
        self.assertEqual(
            Event.objects.filter(event_type="round_evaluation_started").count(), 1
        )

    def test_closing_the_round_starts_the_evaluation(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

        response = self.client.post(
            factories.RoundFactory.get_url(self.call, self.round, "close")
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.round.refresh_from_db()
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.EVALUATING)

    @mock.patch(STEP_EVENT)
    @mock.patch(DECISION_EMAIL)
    def test_evaluation_at_the_cutoff_moves_the_round_to_evaluating(
        self, decision_email, step_event
    ):
        self.call.evaluation_start = EvaluationStart.AT_CUTOFF
        self.call.save(update_fields=["evaluation_start"])
        proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        models.ProposalWorkflowStepInstance.objects.create(
            proposal=proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        self.end_round()

        tasks.start_evaluation_for_closed_rounds()

        self.round.refresh_from_db()
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.EVALUATING)


class BackfillTest(test.APITestCase):
    def test_ended_rounds_get_a_lifecycle(self):
        call = factories.CallFactory(state=CallStates.ACTIVE)
        now = timezone.now()
        busy = factories.RoundFactory(
            call=call,
            start_time=now - timedelta(days=90),
            cutoff_time=now - timedelta(days=60),
        )
        factories.ProposalFactory(round=busy, state=ProposalStates.IN_REVIEW)
        done = factories.RoundFactory(
            call=call,
            start_time=now - timedelta(days=59),
            cutoff_time=now - timedelta(days=30),
        )
        factories.ProposalFactory(round=done, state=ProposalStates.ACCEPTED)
        running = factories.RoundFactory(
            call=call,
            start_time=now - timedelta(days=1),
            cutoff_time=now + timedelta(days=30),
        )

        backfill.backfill_round_lifecycle(django_apps, None)

        for call_round in (busy, done, running):
            call_round.refresh_from_db()
        self.assertEqual(busy.lifecycle_state, RoundLifecycleStates.EVALUATING)
        self.assertEqual(done.lifecycle_state, RoundLifecycleStates.CLOSED)
        self.assertIsNone(running.lifecycle_state)
        self.assertFalse(Event.objects.filter(event_type__startswith="round_").exists())


class CopiesStartWithoutALifecycleTest(test.APITestCase):
    def setUp(self):
        self.call = factories.CallFactory(state=CallStates.ACTIVE)
        now = timezone.now()
        self.round = factories.RoundFactory(
            call=self.call,
            start_time=now - timedelta(days=30),
            cutoff_time=now - timedelta(days=1),
            lifecycle_state=RoundLifecycleStates.RESULTS_PUBLISHED,
            results_published_at=now,
            results_forced_reason="Board adopted the list",
            adopted_at=date(2026, 9, 15),
            adoption_note="Governing Board decision",
        )

    def test_duplicated_rounds_have_no_lifecycle(self):
        copy = utils.duplicate_call(self.call, "Copy", self.call.created_by)

        copied = copy.round_set.get()
        self.assertIsNone(copied.lifecycle_state)
        self.assertIsNone(copied.results_published_at)
        self.assertEqual(copied.results_forced_reason, "")
        self.assertIsNone(copied.adopted_at)
        self.assertEqual(copied.adoption_note, "")

    def test_exported_rounds_carry_no_lifecycle(self):
        document, _ = call_transfer.export_call(self.call)

        (exported,) = document["rounds"]
        for name in models.Round.LIFECYCLE_FIELDS:
            self.assertNotIn(name, exported)


@mock.patch(STEP_EVENT)
@mock.patch(DECISION_EMAIL)
class RoundCompletionTest(LifecycleTestBase):
    """Completing a round needs every proposal of it decided, or a rule that
    rejects the rest."""

    def set_rule(self, call_rule=None, round_rule=None):
        if call_rule is not None:
            self.call.undecided_at_round_completion = call_rule
            self.call.save(update_fields=["undecided_at_round_completion"])
        self.round.undecided_at_round_completion = round_rule
        self.round.save(update_fields=["undecided_at_round_completion"])

    def force_publish(self):
        response = self.publish({"force": True, "reason": "Board adopted the list"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def in_earlier_step(self):
        """A proposal still in its technical assessment."""
        proposal = self.make_decidable()
        instances = {i.step: i for i in proposal.workflow_step_instances.all()}
        instances["allocation_decision"].status = WorkflowStepInstanceStatuses.PENDING
        instances["allocation_decision"].started_at = None
        instances["allocation_decision"].save()
        instances["technical_assessment"].status = WorkflowStepInstanceStatuses.ACTIVE
        instances["technical_assessment"].started_at = timezone.now()
        instances["technical_assessment"].save()
        proposal.workflow_step = "technical_assessment"
        proposal.save(update_fields=["workflow_step"])
        return proposal

    def complete(self, user=None):
        return self.round_action("complete", user=user)

    def test_new_calls_refuse(self, decision_email, step_event):
        self.assertEqual(
            factories.CallFactory().undecided_at_round_completion,
            UndecidedAtRoundCompletion.REFUSE,
        )
        self.assertIsNone(self.round.undecided_at_round_completion)

    def test_complete_is_refused_while_proposals_are_undecided(
        self, decision_email, step_event
    ):
        undecided = self.make_decidable()
        self.in_earlier_step()
        self.force_publish()
        decision_email.reset_mock()

        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["undecided_count"], 2)
        self.assertIn("no decision yet", response.data["detail"])
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )
        undecided.refresh_from_db()
        self.assertEqual(undecided.state, ProposalStates.IN_REVIEW)
        decision_email.assert_not_called()
        self.assertFalse(self.events("round_closed").exists())

    def test_complete_when_everything_is_decided(self, decision_email, step_event):
        proposal = self.make_decidable()
        self.decide(proposal)
        self.publish()

        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.CLOSED)
        self.assertEqual(response.data["failed_proposals"], [])
        self.assertEqual(response.data["rejected_proposals"], [])

    def test_reject_rejects_the_undecided_and_leaves_the_decided(
        self, decision_email, step_event
    ):
        self.set_rule(call_rule=UndecidedAtRoundCompletion.REJECT)
        awarded = self.make_decidable()
        self.decide(awarded)
        # Past its decision, awaiting the applicant's award response.
        awaiting = self.make_decidable(award_response=True)
        self.decide(awaiting)
        undecided = self.make_decidable()
        earlier = self.in_earlier_step()
        self.force_publish()
        decision_email.reset_mock()

        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.CLOSED)
        self.assertEqual(response.data["failed_proposals"], [])
        self.assertCountEqual(
            [item["uuid"] for item in response.data["rejected_proposals"]],
            [undecided.uuid.hex, earlier.uuid.hex],
        )
        for proposal in (awarded, awaiting, undecided, earlier):
            proposal.refresh_from_db()
        self.assertEqual(awarded.state, ProposalStates.ACCEPTED)
        self.assertEqual(awaiting.state, ProposalStates.IN_REVIEW)
        self.assertEqual(awaiting.workflow_step, "award_response")
        self.assertEqual(undecided.state, ProposalStates.REJECTED)
        self.assertEqual(earlier.state, ProposalStates.REJECTED)
        self.assertIsNone(earlier.workflow_step)

        assessment = earlier.workflow_step_instances.get(step="technical_assessment")
        self.assertEqual(assessment.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(assessment.outcome, "rejected")
        self.assertIn("completed", assessment.outcome_reason)
        decision = self.allocation_instance(undecided)
        self.assertEqual(decision.outcome, "rejected")
        self.assertEqual(decision.completed_by, self.call_manager)

        self.assertCountEqual(
            [c.args for c in decision_email.call_args_list],
            [
                (undecided.uuid, ProposalStates.IN_REVIEW, ProposalStates.REJECTED),
                (earlier.uuid, ProposalStates.IN_REVIEW, ProposalStates.REJECTED),
            ],
        )
        self.assertEqual(
            self.events("proposal_rejected_at_round_completion").count(), 2
        )
        (round_event,) = self.events("round_undecided_proposals_rejected")
        self.assertEqual(round_event.context["rejected_count"], 2)
        self.assertEqual(round_event.context["actor"], self.call_manager.full_name)
        self.assertEqual(self.events("round_closed").count(), 1)

    def parked_after_assessment(self):
        """A proposal whose technical assessment is done, waiting for a call
        manager to move it on to the decision by hand."""
        proposal = self.in_earlier_step()
        assessment = proposal.workflow_step_instances.get(step="technical_assessment")
        assessment.status = WorkflowStepInstanceStatuses.COMPLETED
        assessment.outcome = "feasible"
        assessment.completed_at = timezone.now()
        assessment.save()
        call_step = models.CallWorkflowStep.objects.filter(
            call=self.call, step="technical_assessment"
        ).first() or factories.CallWorkflowStepFactory(
            call=self.call, step="technical_assessment"
        )
        call_step.transition_mode = TransitionModes.MANUAL
        call_step.save(update_fields=["transition_mode"])
        self.assertTrue(workflow_service.is_awaiting_manual_advance(proposal))
        return proposal

    def test_reject_records_the_rejection_at_the_parked_step(
        self, decision_email, step_event
    ):
        self.set_rule(call_rule=UndecidedAtRoundCompletion.REJECT)
        parked = self.parked_after_assessment()
        decision_step = models.CallWorkflowStep.objects.filter(
            call=self.call, step="allocation_decision"
        ).first() or factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision"
        )
        models.CallWorkflowStepNotificationRule.objects.get_or_create(
            workflow_step=decision_step,
            trigger=NotificationRuleTriggers.STEP_REJECTED,
            recipient=NotificationRuleRecipients.APPLICANT,
        )
        self.force_publish()
        step_event.reset_mock()

        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        parked.refresh_from_db()
        self.assertEqual(parked.state, ProposalStates.REJECTED)
        self.assertIsNone(parked.workflow_step)
        # The assessment keeps its own outcome; the rejection is recorded on
        # the step the proposal was waiting to enter.
        assessment = parked.workflow_step_instances.get(step="technical_assessment")
        self.assertEqual(assessment.outcome, "feasible")
        decision = self.allocation_instance(parked)
        self.assertEqual(decision.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(decision.outcome, "rejected")
        self.assertIn("completed", decision.outcome_reason)
        self.assertEqual(decision.completed_by, self.call_manager)
        self.assertIsNotNone(decision.started_at)
        step_event.assert_called_once_with(
            decision.uuid.hex, NotificationRuleTriggers.STEP_REJECTED
        )
        event = self.events("proposal_rejected_at_round_completion").get()
        self.assertEqual(event.context["rejected_at_step"], "allocation_decision")

    def fail_release_of(self, held):
        allocate = utils.allocate_proposal

        def allocate_or_fail(proposal, *args, **kwargs):
            if proposal.pk == held.pk:
                raise RuntimeError("Provisioning failed")
            return allocate(proposal, *args, **kwargs)

        with mock.patch.object(
            utils, "allocate_proposal", side_effect=allocate_or_fail
        ):
            self.force_publish()

    def test_complete_is_refused_while_a_decision_is_held(
        self, decision_email, step_event
    ):
        # Whatever the rule: a held decision is not final until it is told.
        self.set_rule(call_rule=UndecidedAtRoundCompletion.REJECT)
        held = self.make_decidable()
        self.decide(held)
        undecided = self.make_decidable()
        self.fail_release_of(held)
        decision_email.reset_mock()

        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["held_decisions_count"], 1)
        self.assertIn("publish", response.data["detail"].lower())
        self.assertNotIn("undecided_count", response.data)
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )
        undecided.refresh_from_db()
        self.assertEqual(undecided.state, ProposalStates.IN_REVIEW)
        self.assertFalse(self.events("proposal_rejected_at_round_completion").exists())
        decision_email.assert_not_called()

        # Publishing again releases it; then the round completes.
        self.assertEqual(self.publish().status_code, status.HTTP_200_OK)
        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.CLOSED)
        held.refresh_from_db()
        self.assertEqual(held.state, ProposalStates.ACCEPTED)

    def test_held_decision_refusal_under_the_refuse_rule(
        self, decision_email, step_event
    ):
        held = self.make_decidable()
        self.decide(held)
        self.make_decidable()
        self.fail_release_of(held)

        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["held_decisions_count"], 1)

    def test_reject_under_immediate_publication(self, decision_email, step_event):
        self.call.publish_results = ResultsPublication.IMMEDIATELY
        self.call.undecided_at_round_completion = UndecidedAtRoundCompletion.REJECT
        self.call.save(
            update_fields=["publish_results", "undecided_at_round_completion"]
        )
        undecided = self.make_decidable()
        self.publish()
        decision_email.reset_mock()

        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        undecided.refresh_from_db()
        self.assertEqual(undecided.state, ProposalStates.REJECTED)
        decision_email.assert_called_once_with(
            undecided.uuid, ProposalStates.IN_REVIEW, ProposalStates.REJECTED
        )

    def test_refused_under_immediate_publication(self, decision_email, step_event):
        self.call.publish_results = ResultsPublication.IMMEDIATELY
        self.call.save(update_fields=["publish_results"])
        self.make_decidable()
        self.publish()

        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["undecided_count"], 1)

    def test_round_refusal_overrides_the_call(self, decision_email, step_event):
        self.set_rule(
            call_rule=UndecidedAtRoundCompletion.REJECT,
            round_rule=UndecidedAtRoundCompletion.REFUSE,
        )
        undecided = self.make_decidable()
        self.force_publish()

        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["undecided_count"], 1)
        undecided.refresh_from_db()
        self.assertEqual(undecided.state, ProposalStates.IN_REVIEW)

    def test_round_rejection_overrides_the_call(self, decision_email, step_event):
        self.set_rule(round_rule=UndecidedAtRoundCompletion.REJECT)
        undecided = self.make_decidable()
        self.force_publish()

        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        undecided.refresh_from_db()
        self.assertEqual(undecided.state, ProposalStates.REJECTED)

    def test_a_failing_rejection_does_not_hold_back_the_rest(
        self, decision_email, step_event
    ):
        self.set_rule(call_rule=UndecidedAtRoundCompletion.REJECT)
        failing = self.make_decidable()
        rejected = self.make_decidable()
        self.force_publish()
        reject = workflow_service.reject_undecided

        def reject_or_fail(proposal, *args, **kwargs):
            if proposal.pk == failing.pk:
                raise RuntimeError("Could not reject")
            return reject(proposal, *args, **kwargs)

        with mock.patch.object(
            workflow_service, "reject_undecided", side_effect=reject_or_fail
        ):
            response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [item["uuid"] for item in response.data["failed_proposals"]],
            [failing.uuid.hex],
        )
        self.assertEqual(
            [item["uuid"] for item in response.data["rejected_proposals"]],
            [rejected.uuid.hex],
        )
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )
        self.assertIsNone(self.round.closed_at)
        self.assertFalse(self.events("round_closed").exists())
        failing.refresh_from_db()
        rejected.refresh_from_db()
        self.assertEqual(failing.state, ProposalStates.IN_REVIEW)
        self.assertEqual(rejected.state, ProposalStates.REJECTED)

        # Completing again rejects what is left and closes the round.
        response = self.complete()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.round.lifecycle_state, RoundLifecycleStates.CLOSED)
        failing.refresh_from_db()
        self.assertEqual(failing.state, ProposalStates.REJECTED)

    def test_only_round_closers_may_complete(self, decision_email, step_event):
        self.set_rule(call_rule=UndecidedAtRoundCompletion.REJECT)
        undecided = self.make_decidable()
        self.force_publish()

        response = self.complete(user=structure_factories.UserFactory())

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        undecided.refresh_from_db()
        self.assertEqual(undecided.state, ProposalStates.IN_REVIEW)
        self.assertEqual(
            self.round.lifecycle_state, RoundLifecycleStates.RESULTS_PUBLISHED
        )

    def test_call_manager_sets_the_rule_on_call_and_round(
        self, decision_email, step_event
    ):
        self.client.force_authenticate(self.call_manager)

        response = self.client.patch(
            factories.CallFactory.get_protected_url(self.call),
            {"undecided_at_round_completion": UndecidedAtRoundCompletion.REJECT},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["undecided_at_round_completion"],
            UndecidedAtRoundCompletion.REJECT,
        )

        # A round is saved whole.
        url = factories.RoundFactory.get_url(self.call, self.round)
        payload = {
            "name": self.round.name,
            "start_time": self.round.start_time.isoformat(),
            "cutoff_time": self.round.cutoff_time.isoformat(),
        }
        response = self.client.put(
            url,
            {
                **payload,
                "undecided_at_round_completion": UndecidedAtRoundCompletion.REFUSE,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.round.refresh_from_db()
        self.assertEqual(
            self.round.undecided_at_round_completion,
            UndecidedAtRoundCompletion.REFUSE,
        )

        response = self.client.put(
            url, {**payload, "undecided_at_round_completion": None}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data["undecided_at_round_completion"])

    def test_rule_travels_with_duplicate_and_export(self, decision_email, step_event):
        self.set_rule(
            call_rule=UndecidedAtRoundCompletion.REJECT,
            round_rule=UndecidedAtRoundCompletion.REFUSE,
        )

        duplicate = utils.duplicate_call(self.call, "Copy", self.staff)
        self.assertEqual(
            duplicate.undecided_at_round_completion, UndecidedAtRoundCompletion.REJECT
        )
        self.assertEqual(
            duplicate.round_set.get().undecided_at_round_completion,
            UndecidedAtRoundCompletion.REFUSE,
        )

        document, _ = call_transfer.export_call(self.call)
        self.assertEqual(document["call"]["undecided_at_round_completion"], "reject")
        self.assertEqual(
            document["rounds"][0]["undecided_at_round_completion"], "refuse"
        )
        imported, _, _ = call_transfer.import_call(
            document, self.call.manager, self.staff, name="Imported"
        )
        self.assertEqual(
            imported.undecided_at_round_completion, UndecidedAtRoundCompletion.REJECT
        )
        self.assertEqual(
            imported.round_set.get().undecided_at_round_completion,
            UndecidedAtRoundCompletion.REFUSE,
        )
