"""An allocation decision the system takes on its own.

A call whose decision decides automatically awards a proposal, as requested,
the moment it reaches the decision: right after the last requested offering
accepts it in a per-offering technical assessment, say. When the approval is
refused -- a gate, missing amounts, no decided step before it that could
reject -- the decision waits for the call managers, as any other would.
"""

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.core.utils import get_system_robot
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import activity, models, workflow_service
from waldur_mastermind.proposal.enums import (
    AWARD_RESPONSE_KEY,
    CallStates,
    ContributionOutcomes,
    ContributorSources,
    OnNegativeOutcomes,
    OutcomeSets,
    ProposalStates,
    ResponsibleRoles,
    ResultsPublication,
    StepKinds,
    TransitionModes,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tasks import mark_expired_workflow_steps
from waldur_mastermind.proposal.tests import factories, fixtures

NOTIFY = "waldur_mastermind.proposal.tasks.notify_proposal_decision"


class AutomaticDecisionBase(test.APITestCase):
    """A proposal requesting two offerings, at a technical assessment split
    per requested offering, followed by an automatic allocation decision."""

    decides_automatically = True

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call_manager = self.fixture.call_manager

        self.checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.WORKFLOW_STEP
        )
        self.question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            required=True,
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
        )

        models.CallWorkflowStep.objects.filter(call=self.call).exclude(
            key__in=("technical_assessment", "allocation_decision")
        ).update(is_enabled=False)
        self.assessment = models.CallWorkflowStep.objects.get(
            call=self.call, key="technical_assessment"
        )
        self.assessment.is_enabled = True
        self.assessment.contributor_source = ContributorSources.REQUESTED_OFFERING_TEAM
        self.assessment.per_requested_offering = True
        self.assessment.outcome_set = OutcomeSets.PER_OFFERING
        self.assessment.on_negative = OnNegativeOutcomes.REJECT
        self.assessment.checklist = self.checklist
        self.assessment.duration_in_days = 5
        self.assessment.save()
        self.decision = models.CallWorkflowStep.objects.get(
            call=self.call, key="allocation_decision"
        )
        self.decision.decides_automatically = self.decides_automatically
        self.decision.save()
        self.configure()

        self.proposal = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.SUBMITTED, project=None
        )
        fixtures.author_as_manager(self.proposal)
        self.manager_a, self.requested_a = self._request_offering()
        self.manager_b, self.requested_b = self._request_offering()

        workflow_service.create_step_instances(self.proposal)
        workflow_service.activate_first_step(self.proposal)
        self.proposal.refresh_from_db()
        self.instance = self.proposal.current_step

    def configure(self):
        """Change the call's steps before the proposal is submitted."""

    def _request_offering(self):
        manager = structure_factories.UserFactory()
        offering = marketplace_factories.OfferingFactory()
        offering.add_user(manager, OfferingRole.MANAGER)
        requested = factories.RequestedOfferingFactory(
            call=self.call, offering=offering, created_by=self.fixture.owner
        )
        factories.RequestedResourceFactory(
            proposal=self.proposal, requested_offering=requested
        )
        return manager, requested

    def _url(self, action):
        return factories.ProposalFactory.get_url(self.proposal, action=action)

    def _assess(self, user, requested, outcome):
        slot = self.instance.contributions.get(requested_offering=requested)
        self.client.force_authenticate(user)
        self.client.post(
            self._url("submit-step-checklist-answers")
            + f"?step_uuid={self.assessment.uuid.hex}"
            + f"&contribution_uuid={slot.uuid.hex}",
            [{"question_uuid": str(self.question.uuid), "answer_data": "Fits"}],
            format="json",
        )
        return self.client.post(
            self._url("submit_contribution"),
            {"contribution_uuid": slot.uuid.hex, "outcome": outcome},
            format="json",
        )

    def _assess_both(self, outcome_b=ContributionOutcomes.ACCEPTED):
        self._assess(self.manager_a, self.requested_a, ContributionOutcomes.ACCEPTED)
        return self._assess(self.manager_b, self.requested_b, outcome_b)

    def _decision_instance(self):
        return self.proposal.workflow_step_instances.get(call_step=self.decision)

    def _deferred_events(self):
        return Event.objects.filter(
            event_type=EventType.PROPOSAL_AUTOMATIC_DECISION_DEFERRED
        )


class AutomaticAwardTest(AutomaticDecisionBase):
    def test_last_acceptance_awards_the_proposal_in_the_same_request(self):
        with mock.patch(NOTIFY) as notify:
            response = self._assess_both()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.proposal.refresh_from_db()
        robot = get_system_robot()
        decision = self._decision_instance()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertIsNotNone(self.proposal.project)
        self.assertEqual(self.proposal.approved_by, robot)
        self.assertEqual(decision.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(decision.outcome, WorkflowStepOutcomes.APPROVED)
        self.assertEqual(decision.completed_by, robot)
        notify.assert_called_once_with(
            self.proposal.uuid, ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED
        )
        self.assertFalse(self._deferred_events().exists())

    def test_steps_tell_the_decision_was_automatic(self):
        self._assess_both()
        for user in (self.call_manager, self.proposal.created_by):
            self.client.force_authenticate(user)
            response = self.client.get(self._url("workflow_states"))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            flags = {row["step"]: row["decided_automatically"] for row in response.data}
            self.assertTrue(flags["allocation_decision"])
            self.assertFalse(flags["technical_assessment"])

    def test_award_is_the_request(self):
        self._assess_both()
        requested = {
            (r.requested_offering_id, str(r.attributes), str(r.limits))
            for r in self.proposal.requestedresource_set.all()
        }
        awarded = {
            (r.requested_offering_id, str(r.attributes), str(r.limits))
            for r in self.proposal.awarded_resources.all()
        }
        self.assertEqual(awarded, requested)

    def test_a_rejecting_offering_rejects_and_no_decision_runs(self):
        with mock.patch(NOTIFY):
            self._assess_both(ContributionOutcomes.REJECTED)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)
        self.assertEqual(
            self._decision_instance().status, WorkflowStepInstanceStatuses.PENDING
        )
        self.assertIsNone(self.proposal.approved_by)

    def test_the_decision_names_no_actor_on_the_feed(self):
        self._assess_both()
        completed = Event.objects.filter(
            event_type=EventType.PROPOSAL_STEP_COMPLETED,
            context__step_name=self.decision.name,
        )
        self.assertTrue(completed.exists())
        self.assertNotIn(
            self.manager_b.uuid.hex,
            {event.context.get("user_uuid") for event in completed},
        )


class AwardResponseAfterAutomaticDecisionTest(AutomaticDecisionBase):
    def configure(self):
        models.CallWorkflowStep.objects.filter(
            call=self.call, key=AWARD_RESPONSE_KEY
        ).delete()
        models.CallWorkflowStep.create_default(
            self.call, AWARD_RESPONSE_KEY, order=self.decision.order + 1
        )

    def test_award_response_starts_after_the_automatic_decision(self):
        self._assess_both()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(self.proposal.current_step.step, AWARD_RESPONSE_KEY)
        self.assertEqual(
            self.proposal.current_step.status, WorkflowStepInstanceStatuses.ACTIVE
        )
        decision = self._decision_instance()
        self.assertEqual(decision.outcome, WorkflowStepOutcomes.APPROVED)
        self.assertEqual(decision.completed_by, get_system_robot())


class GateRefusalFallsBackTest(AutomaticDecisionBase):
    def configure(self):
        self.decision.min_score_threshold = 4
        self.decision.save()

    def test_refused_approval_waits_for_the_call_managers(self):
        with mock.patch(NOTIFY) as notify:
            response = self._assess_both()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        notify.assert_not_called()

        self.proposal.refresh_from_db()
        decision = self._decision_instance()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(self.proposal.current_step_id, decision.pk)
        self.assertEqual(decision.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNone(decision.outcome)
        self.assertIsNone(self.proposal.project)
        self.assertEqual(decision.call_step.completer, ResponsibleRoles.CALL_MANAGER)

        event = self._deferred_events().get()
        self.assertIn("below the required minimum", event.context["reason"])
        self.assertNotIn("user_uuid", event.context)

    def test_a_decision_by_hand_is_not_automatic(self):
        self._assess_both()
        self.decision.min_score_threshold = None
        self.decision.save()
        workflow_service.complete_step(
            self.proposal,
            self._decision_instance(),
            WorkflowStepOutcomes.APPROVED,
            "",
            self.call_manager,
        )
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self._url("workflow_states"))
        flags = {row["step"]: row["decided_automatically"] for row in response.data}
        self.assertFalse(flags["allocation_decision"])

    def test_call_manager_completes_it_by_hand(self):
        self._assess_both()
        self.decision.min_score_threshold = None
        self.decision.save()
        decision = self._decision_instance()
        self.client.force_authenticate(self.call_manager)
        with mock.patch(NOTIFY):
            response = self.client.post(
                self._url("complete_workflow_step"),
                {"step_uuid": decision.uuid.hex, "outcome": "approved"},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(self.proposal.approved_by, self.call_manager)

    def test_the_applicant_team_does_not_read_the_reason(self):
        self._assess_both()
        rows = activity.proposal_event_rows(self.proposal.created_by, self.proposal)
        self.assertTrue(self._deferred_events().exists())
        self.assertFalse(self._deferred_events().filter(rows).exists())
        rows = activity.proposal_event_rows(self.manager_b, self.proposal)
        self.assertFalse(self._deferred_events().filter(rows).exists())


class HeldAutomaticDecisionTest(AutomaticDecisionBase):
    def configure(self):
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save()

    def test_the_applicant_is_not_told_of_a_held_automatic_decision(self):
        self._assess_both()
        self.client.force_authenticate(self.proposal.created_by)
        response = self.client.get(self._url("workflow_states"))
        row = next(r for r in response.data if r["step"] == "allocation_decision")
        self.assertFalse(row["decided_automatically"])
        self.assertIsNone(row["outcome"])

    def test_automatic_decision_is_held_for_the_round(self):
        with mock.patch(NOTIFY) as notify:
            self._assess_both()
        notify.assert_not_called()
        self.proposal.refresh_from_db()
        decision = self._decision_instance()
        self.assertTrue(self.proposal.decision_held)
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(decision.outcome, WorkflowStepOutcomes.APPROVED)

        workflow_service.release_held_decision(self.proposal)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(self.proposal.approved_by, get_system_robot())


class LapsedAssessmentTest(AutomaticDecisionBase):
    def test_a_lapsed_assessment_never_awards(self):
        self.instance.deadline = timezone.now() - timedelta(hours=1)
        self.instance.save()
        with mock.patch(NOTIFY) as notify:
            self.assertEqual(mark_expired_workflow_steps(), 1)
        notify.assert_not_called()
        self.proposal.refresh_from_db()
        decision = self._decision_instance()
        self.assertEqual(decision.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertIn(
            "ended without a result", self._deferred_events().get().context["reason"]
        )


class AwardAfterLapsedNonGateTest(AutomaticDecisionBase):
    """A step that cannot reject lapses after the assessment accepted: the
    decision is taken by the expiry sweep, and announced."""

    def configure(self):
        models.CallWorkflowStep.objects.filter(
            call=self.call, order__gt=self.decision.order
        ).delete()
        order = self.decision.order
        models.CallWorkflowStep.make_room(self.call.pk, order)
        self.feedback = models.CallWorkflowStep.objects.create(
            call=self.call,
            key="feedback",
            kind=StepKinds.DECISION,
            name="Feedback",
            outcome_set=OutcomeSets.APPROVAL,
            on_negative=OnNegativeOutcomes.CONTINUE,
            completer=ResponsibleRoles.CALL_MANAGER,
            order=order,
        )
        self.decision.refresh_from_db()

    def test_expiry_sweep_awards_and_announces(self):
        self._assess_both()
        feedback = self.proposal.workflow_step_instances.get(call_step=self.feedback)
        self.assertEqual(feedback.status, WorkflowStepInstanceStatuses.ACTIVE)
        feedback.deadline = timezone.now() - timedelta(hours=1)
        feedback.save()

        with (
            mock.patch(NOTIFY) as notify,
            self.captureOnCommitCallbacks(execute=True),
        ):
            mark_expired_workflow_steps()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        notify.assert_called_once_with(
            self.proposal.uuid, ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED
        )


class ManualAdvanceIntoAutomaticDecisionTest(AutomaticDecisionBase):
    def configure(self):
        self.assessment.transition_mode = TransitionModes.MANUAL
        self.assessment.save()

    def test_advance_reaches_the_award(self):
        self._assess_both()
        self.proposal.refresh_from_db()
        self.assertTrue(workflow_service.is_awaiting_manual_advance(self.proposal))
        self.client.force_authenticate(self.call_manager)
        with mock.patch(NOTIFY) as notify:
            response = self.client.post(self._url("advance_workflow_step"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["proposal_state"], ProposalStates.ACCEPTED)
        notify.assert_called_once_with(
            self.proposal.uuid, ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED
        )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.approved_by, get_system_robot())

    def test_advance_into_a_held_decision_announces_nothing(self):
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save()
        self._assess_both()
        self.client.force_authenticate(self.call_manager)
        with mock.patch(NOTIFY) as notify:
            response = self.client.post(self._url("advance_workflow_step"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        notify.assert_not_called()
        self.proposal.refresh_from_db()
        self.assertTrue(self.proposal.decision_held)


class WithoutTheFlagTest(AutomaticDecisionBase):
    decides_automatically = False

    def test_decision_waits_for_the_call_managers(self):
        with mock.patch(NOTIFY) as notify:
            self._assess_both()
        notify.assert_not_called()
        self.proposal.refresh_from_db()
        decision = self._decision_instance()
        self.assertEqual(decision.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertEqual(self.proposal.current_step_id, decision.pk)
        self.assertFalse(self._deferred_events().exists())


class AutomaticDecisionConfigTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.new_call
        factories.RoundFactory(call=self.call)
        factories.RequestedOfferingFactory(call=self.call)
        self.decision = models.CallWorkflowStep.objects.get(
            call=self.call, key="allocation_decision"
        )
        self.user = self.fixture.call_organizer_user

    def _patch(self, step, payload):
        self.client.force_authenticate(self.user)
        return self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, step), payload
        )

    def _activate(self):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            factories.CallFactory.get_protected_url(self.call, "activate")
        )

    def _only_decision_left(self):
        models.CallWorkflowStep.objects.filter(call=self.call).exclude(
            pk=self.decision.pk
        ).update(is_enabled=False)

    def test_only_the_allocation_decision_decides_automatically(self):
        step = models.CallWorkflowStep.objects.get(
            call=self.call, key="administrative_check"
        )
        response = self._patch(step, {"decides_automatically": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("decides_automatically", response.data)

    def test_automatic_decision_cannot_wait_for_a_manual_advance(self):
        response = self._patch(
            self.decision,
            {"decides_automatically": True, "transition_mode": TransitionModes.MANUAL},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("transition_mode", response.data)

    def test_flag_is_read_back(self):
        response = self._patch(self.decision, {"decides_automatically": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["decides_automatically"])

    def test_activation_needs_a_step_that_can_reject_first(self):
        self._only_decision_left()
        self.decision.decides_automatically = True
        self.decision.save()
        response = self._activate()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("automatic allocation decision", str(response.data))
        self.call.refresh_from_db()
        self.assertEqual(self.call.state, CallStates.DRAFT)

    def test_a_step_that_continues_on_negative_is_no_guard(self):
        self._only_decision_left()
        models.CallWorkflowStep.objects.filter(
            call=self.call, key="administrative_check"
        ).update(is_enabled=True, on_negative=OnNegativeOutcomes.CONTINUE)
        self.decision.decides_automatically = True
        self.decision.save()
        self.assertEqual(self._activate().status_code, status.HTTP_400_BAD_REQUEST)

    def test_activation_with_a_rejecting_step_first_succeeds(self):
        self._only_decision_left()
        models.CallWorkflowStep.objects.filter(
            call=self.call, key="administrative_check"
        ).update(is_enabled=True)
        self.decision.decides_automatically = True
        self.decision.save()
        response = self._activate()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_active_call_refuses_an_unguarded_automatic_decision(self):
        self._only_decision_left()
        self.call.state = CallStates.ACTIVE
        self.call.save()
        response = self._patch(self.decision, {"decides_automatically": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("decides_automatically", response.data)

    def test_decision_kind_offers_the_flag(self):
        self.client.force_authenticate(self.user)
        kinds = self.client.get("/api/proposal-protected-calls/workflow_step_kinds/")
        self.assertEqual(kinds.status_code, status.HTTP_200_OK)
        decision = next(k for k in kinds.data if k["id"] == StepKinds.DECISION)
        self.assertIn("decides_automatically", decision["config_fields"])


class CallAdoptionOnProposalTest(test.APITestCase):
    def test_proposal_tells_whether_its_call_adopts_results(self):
        fixture = fixtures.ProposalFixture()
        proposal = fixture.proposal
        url = factories.ProposalFactory.get_url(proposal)
        self.client.force_authenticate(fixture.staff)
        self.assertFalse(self.client.get(url).data["call_adoption_required"])
        fixture.call.adoption_required = True
        fixture.call.save()
        self.assertTrue(self.client.get(url).data["call_adoption_required"])


class AutomaticDecisionAfterParallelStageTest(AutomaticDecisionBase):
    """The technical assessment runs beside a panel review; the automatic
    decision follows the stage, once both have ended."""

    def configure(self):
        models.CallWorkflowStep.objects.filter(
            call=self.call, key="expert_review"
        ).delete()
        self.panel = models.CallWorkflowStep.objects.get(
            call=self.call, key="panel_review"
        )
        self.panel.is_enabled = True
        self.panel.parallel_with_previous = True
        self.panel.completer = ResponsibleRoles.CALL_MANAGER
        self.panel.duration_in_days = 5
        self.panel.save()

    def _panel_instance(self):
        return self.proposal.workflow_step_instances.get(call_step=self.panel)

    def _complete_panel(self):
        self.client.force_authenticate(self.call_manager)
        return self.client.post(
            self._url("complete_workflow_step"),
            {
                "step_uuid": self._panel_instance().uuid.hex,
                "outcome": WorkflowStepOutcomes.APPROVED,
            },
            format="json",
        )

    def test_awarded_once_the_last_step_of_the_stage_ends(self):
        self.assertEqual(
            self._panel_instance().status, WorkflowStepInstanceStatuses.ACTIVE
        )
        response = self._complete_panel()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self._decision_instance().status, WorkflowStepInstanceStatuses.PENDING
        )

        with mock.patch(NOTIFY) as notify:
            response = self._assess_both()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(self._decision_instance().completed_by, get_system_robot())
        notify.assert_called_once_with(
            self.proposal.uuid, ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED
        )

    def test_awarded_when_the_other_step_of_the_stage_ends_last(self):
        self._assess_both()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(
            self._decision_instance().status, WorkflowStepInstanceStatuses.PENDING
        )

        with mock.patch(NOTIFY):
            response = self._complete_panel()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["proposal_state"], ProposalStates.ACCEPTED)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.approved_by, get_system_robot())

    def test_a_lapsed_step_of_the_stage_never_awards(self):
        self._assess_both()
        panel = self._panel_instance()
        panel.deadline = timezone.now() - timedelta(hours=1)
        panel.save(update_fields=["deadline"])
        with mock.patch(NOTIFY) as notify:
            mark_expired_workflow_steps()
        notify.assert_not_called()
        self.proposal.refresh_from_db()
        decision = self._decision_instance()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(decision.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertEqual(self.proposal.current_step_id, decision.pk)
        self.assertIn(
            "ended without a result", self._deferred_events().get().context["reason"]
        )


class AutomaticDecisionAfterReopenedAssessmentTest(AutomaticDecisionBase):
    """An assessment the call team reopens before the stage ends: the
    resubmission that completes it leads to the automatic decision, which
    still asks its gates."""

    def _reopen(self, requested):
        slot = self.instance.contributions.get(requested_offering=requested)
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self._url("reopen_contribution"),
            {"contribution_uuid": slot.uuid.hex, "message": "Recheck the hours."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def _reopened_and_resubmitted(self, outcome):
        self._assess(self.manager_a, self.requested_a, ContributionOutcomes.ACCEPTED)
        self._reopen(self.requested_a)
        self._assess(self.manager_b, self.requested_b, ContributionOutcomes.ACCEPTED)
        self.assertEqual(
            self._decision_instance().status, WorkflowStepInstanceStatuses.PENDING
        )
        with mock.patch(NOTIFY):
            response = self._assess(self.manager_a, self.requested_a, outcome)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["step_completed"])
        self.proposal.refresh_from_db()

    def test_the_resubmission_completing_the_step_leads_to_the_award(self):
        self._reopened_and_resubmitted(ContributionOutcomes.ACCEPTED)
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(self._decision_instance().completed_by, get_system_robot())

    def test_a_resubmitted_rejection_rejects_and_no_decision_runs(self):
        self._reopened_and_resubmitted(ContributionOutcomes.REJECTED)
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)
        self.assertEqual(
            self._decision_instance().status, WorkflowStepInstanceStatuses.PENDING
        )

    def test_the_decisions_gates_still_hold(self):
        self.decision.min_score_threshold = 4
        self.decision.save(update_fields=["min_score_threshold"])
        self._reopened_and_resubmitted(ContributionOutcomes.ACCEPTED)
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(
            self._decision_instance().status, WorkflowStepInstanceStatuses.ACTIVE
        )
        self.assertIn(
            "below the required minimum",
            self._deferred_events().get().context["reason"],
        )
