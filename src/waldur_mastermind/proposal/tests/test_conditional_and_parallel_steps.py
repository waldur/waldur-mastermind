"""Steps that run only on some proposals, and steps that run at once.

A step with a run condition runs only when an earlier step, or one of its
contributions, ended with one of the outcomes it names; otherwise the
proposal skips it. Steps flagged to run alongside the step before them form
a stage: all start together, and the next stage starts once all have ended.
"""

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import (
    call_templates,
    call_transfer,
    round_lifecycle,
    utils,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    CallStates,
    ContributionOutcomes,
    ContributorSources,
    NotificationRuleTriggers,
    OnNegativeOutcomes,
    OutcomeSets,
    ProposalStates,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    TransitionModes,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.models import CallWorkflowStep
from waldur_mastermind.proposal.tasks import mark_expired_workflow_steps
from waldur_mastermind.proposal.tests import factories, fixtures

ACTIVE = WorkflowStepInstanceStatuses.ACTIVE
SKIPPED = WorkflowStepInstanceStatuses.SKIPPED
WITHDRAWN = WorkflowStepInstanceStatuses.WITHDRAWN

DISPATCH = "waldur_mastermind.proposal.notification_rules.dispatch_step_event"


class WorkflowBase(test.APITestCase):
    """A proposal asking two offerings, each managed by its own person, on a
    call whose workflow the test shapes before the proposal is submitted."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call_manager = self.fixture.call_manager
        self.panel_member = self.fixture.panel_member
        CallWorkflowStep.objects.filter(
            call=self.call, key__in=("administrative_check", "expert_review")
        ).delete()
        self.technical = self._per_offering_step("technical_assessment")
        self.proposal = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.SUBMITTED
        )
        fixtures.author_as_manager(self.proposal)
        self.manager_a, self.requested_a = self._request_offering()
        self.manager_b, self.requested_b = self._request_offering()

    def _per_offering_step(self, key, **values):
        step = CallWorkflowStep.objects.filter(call=self.call, key=key).first()
        if step is None:
            step = CallWorkflowStep(
                call=self.call,
                key=key,
                kind=StepKinds.ASSESSMENT,
                name=key.replace("_", " ").capitalize(),
                order=values.pop("order"),
            )
        step.is_enabled = True
        step.kind = StepKinds.ASSESSMENT
        step.completer = ResponsibleRoles.OFFERING_MANAGER
        step.contributor_source = ContributorSources.REQUESTED_OFFERING_TEAM
        step.per_requested_offering = True
        step.outcome_set = OutcomeSets.PER_OFFERING
        step.on_negative = OnNegativeOutcomes.REJECT
        for field, value in values.items():
            setattr(step, field, value)
        step.save()
        return step

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

    def _start(self):
        workflow_service.create_step_instances(self.proposal)
        workflow_service.activate_first_step(self.proposal)
        self.proposal.refresh_from_db()

    def _instance(self, key):
        return self.proposal.workflow_step_instances.get(step=key)

    def _url(self, action):
        return factories.ProposalFactory.get_url(self.proposal, action=action)

    def _assess(self, key, requested, manager, outcome, conditions=""):
        slot = self._instance(key).contributions.get(requested_offering=requested)
        self.client.force_authenticate(manager)
        response = self.client.post(
            self._url("submit_contribution"),
            {
                "contribution_uuid": slot.uuid.hex,
                "outcome": outcome,
                "conditions": conditions,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def _complete(self, user, key, outcome, **extra):
        self.client.force_authenticate(user)
        payload = {"outcome": outcome, **extra}
        if key is not None:
            payload["step_uuid"] = self._instance(key).uuid.hex
        return self.client.post(
            self._url("complete_workflow_step"), payload, format="json"
        )

    def _reject(self, user, key, reason="Not this time", **extra):
        self.client.force_authenticate(user)
        payload = {"reason": reason, **extra}
        if key is not None:
            payload["step_uuid"] = self._instance(key).uuid.hex
        return self.client.post(
            self._url("reject_workflow_step"), payload, format="json"
        )


# =============================================================================
# Conditional steps
# =============================================================================


class ConditionalStepTest(WorkflowBase):
    """The Quantum shape: an emulation assessment that runs only for the
    systems the technical assessment accepted on condition."""

    def setUp(self):
        super().setUp()
        self.emulation = self._per_offering_step(
            "emulation_assessment",
            order=self.technical.order + 1,
            run_condition={
                "step": "technical_assessment",
                "outcomes": [ContributionOutcomes.CONDITIONALLY_ACCEPTED],
            },
        )
        CallWorkflowStep.objects.filter(call=self.call, key="panel_review").delete()
        self._start()

    def test_proposal_accepted_outright_skips_the_conditional_step(self):
        with mock.patch(DISPATCH) as dispatch:
            self._assess(
                "technical_assessment",
                self.requested_a,
                self.manager_a,
                ContributionOutcomes.ACCEPTED,
            )
            self._assess(
                "technical_assessment",
                self.requested_b,
                self.manager_b,
                ContributionOutcomes.ACCEPTED,
            )
        emulation = self._instance("emulation_assessment")
        self.assertEqual(emulation.status, SKIPPED)
        self.assertIsNotNone(emulation.completed_at)
        self.assertIsNone(emulation.started_at)
        self.assertFalse(emulation.contributions.exists())
        notified = [call.args[0].step for call in dispatch.call_args_list]
        self.assertNotIn("emulation_assessment", notified)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.current_step.step, "allocation_decision")
        self.assertEqual(self._instance("allocation_decision").status, ACTIVE)

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = next(r for r in response.data if r["step"] == "emulation_assessment")
        self.assertEqual(row["skipped_reason"], "condition_not_met")
        self.assertEqual(row["run_condition"]["step"], "technical_assessment")

    def test_conditionally_accepted_system_alone_is_assessed_again(self):
        self._assess(
            "technical_assessment",
            self.requested_a,
            self.manager_a,
            ContributionOutcomes.CONDITIONALLY_ACCEPTED,
            conditions="Show the benchmark on the emulator",
        )
        self._assess(
            "technical_assessment",
            self.requested_b,
            self.manager_b,
            ContributionOutcomes.ACCEPTED,
        )
        emulation = self._instance("emulation_assessment")
        self.assertEqual(emulation.status, ACTIVE)
        self.assertEqual(
            list(emulation.contributions.values_list("requested_offering", flat=True)),
            [self.requested_a.pk],
        )
        # The team of the system accepted outright has nothing to fill.
        self.client.force_authenticate(self.manager_b)
        response = self.client.get(
            self._url("contributions"), {"step_uuid": self.emulation.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_the_applicant_does_not_read_a_hidden_steps_condition(self):
        for requested, manager in (
            (self.requested_a, self.manager_a),
            (self.requested_b, self.manager_b),
        ):
            self._assess(
                "technical_assessment",
                requested,
                manager,
                ContributionOutcomes.ACCEPTED,
            )
        self.client.force_authenticate(self.proposal.created_by)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = next(r for r in response.data if r["step"] == "emulation_assessment")
        self.assertEqual(row["run_condition"], {})
        self.assertIsNone(row["skipped_reason"])

    def test_condition_is_fixed_when_the_proposal_is_submitted(self):
        self.emulation.run_condition = {}
        self.emulation.save()
        for requested, manager in (
            (self.requested_a, self.manager_a),
            (self.requested_b, self.manager_b),
        ):
            self._assess(
                "technical_assessment",
                requested,
                manager,
                ContributionOutcomes.ACCEPTED,
            )
        self.assertEqual(self._instance("emulation_assessment").status, SKIPPED)

    def test_lapsed_condition_step_holds_only_for_expired(self):
        instance = self._instance("technical_assessment")
        instance.deadline = timezone.now() - timedelta(minutes=1)
        instance.save(update_fields=["deadline"])
        mark_expired_workflow_steps()
        self.proposal.refresh_from_db()
        # The proposal is rejected at the end when every step lapses or is
        # skipped, as before; the conditional step did not run.
        self.assertEqual(self._instance("emulation_assessment").status, SKIPPED)


# =============================================================================
# Parallel stages
# =============================================================================


class ParallelStageBase(WorkflowBase):
    """The technical assessment, by the offering teams, running beside a
    panel decision, by the call's panel, before the allocation decision."""

    panel_mode = TransitionModes.AUTOMATIC_ON_COMPLETION

    def setUp(self):
        super().setUp()
        self.panel = CallWorkflowStep.objects.get(call=self.call, key="panel_review")
        self.panel.is_enabled = True
        self.panel.parallel_with_previous = True
        self.panel.transition_mode = self.panel_mode
        self.panel.save()
        with mock.patch(DISPATCH) as dispatch:
            self._start()
        self.started = [
            call.args[0].step
            for call in dispatch.call_args_list
            if call.args[1] == NotificationRuleTriggers.STEP_STARTED
        ]

    def _assess_all(self, outcome_b=ContributionOutcomes.ACCEPTED):
        self._assess(
            "technical_assessment",
            self.requested_a,
            self.manager_a,
            ContributionOutcomes.ACCEPTED,
        )
        return self._assess(
            "technical_assessment", self.requested_b, self.manager_b, outcome_b
        )


class ParallelStageTest(ParallelStageBase):
    def test_steps_of_a_stage_start_together(self):
        self.assertEqual(self._instance("technical_assessment").status, ACTIVE)
        self.assertEqual(self._instance("panel_review").status, ACTIVE)
        self.assertEqual(self.started, ["technical_assessment", "panel_review"])
        self.assertEqual(self.proposal.current_step.step, "technical_assessment")
        self.assertEqual(
            self._instance("technical_assessment").stage,
            self._instance("panel_review").stage,
        )

    def test_the_stage_moves_on_once_every_step_has_ended(self):
        response = self._complete(
            self.panel_member, "panel_review", WorkflowStepOutcomes.APPROVED
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("Still running", response.data["detail"])
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(self.proposal.current_step.step, "technical_assessment")
        self.assertEqual(
            self._instance("allocation_decision").status,
            WorkflowStepInstanceStatuses.PENDING,
        )

        self._assess_all()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.current_step.step, "allocation_decision")
        self.assertEqual(self._instance("allocation_decision").status, ACTIVE)

    def test_the_stage_moves_on_exactly_once(self):
        self._assess_all()
        self.assertEqual(self._instance("allocation_decision").status, "pending")
        with mock.patch(DISPATCH) as dispatch:
            self._complete(
                self.panel_member, "panel_review", WorkflowStepOutcomes.APPROVED
            )
        started = [
            call.args[0].step
            for call in dispatch.call_args_list
            if call.args[1] == NotificationRuleTriggers.STEP_STARTED
        ]
        self.assertEqual(started, ["allocation_decision"])
        # Completing the ended step again is refused, and starts nothing: its
        # completer, like the call manager, is told the proposal moved on.
        with mock.patch(DISPATCH) as dispatch:
            response = self._complete(
                self.panel_member, "panel_review", WorkflowStepOutcomes.APPROVED
            )
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
            response = self._complete(
                self.call_manager, "panel_review", WorkflowStepOutcomes.APPROVED
            )
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        dispatch.assert_not_called()
        self.assertEqual(self._instance("allocation_decision").status, ACTIVE)

    def test_a_rejection_withdraws_the_steps_running_beside_it(self):
        panel_slot = factories.StepContributionFactory(
            proposal_step=self._instance("panel_review"), contributor=self.panel_member
        )
        with mock.patch(DISPATCH) as dispatch:
            self._assess_all(outcome_b=ContributionOutcomes.REJECTED)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)
        self.assertIsNone(self.proposal.current_step)
        self.assertEqual(self._instance("panel_review").status, WITHDRAWN)
        panel_slot.refresh_from_db()
        self.assertEqual(panel_slot.status, StepContributionStatuses.WITHDRAWN)
        # Each submitted assessment announces itself; of the step events,
        # only the rejection goes out, and nothing starts after it.
        events = [
            (c.args[0].step, c.args[1])
            for c in dispatch.call_args_list
            if c.args[1] != NotificationRuleTriggers.CONTRIBUTION_SUBMITTED
        ]
        self.assertEqual(
            events,
            [("technical_assessment", NotificationRuleTriggers.STEP_REJECTED)],
        )

    def test_rejecting_at_one_step_withdraws_the_other(self):
        response = self._reject(self.panel_member, "panel_review")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._instance("technical_assessment").status, WITHDRAWN)
        self.assertFalse(
            self._instance("technical_assessment")
            .contributions.filter(status__in=StepContributionStatuses.OPEN)
            .exists()
        )

    def test_one_step_lapsing_waits_for_the_other(self):
        panel = self._instance("panel_review")
        panel.deadline = timezone.now() - timedelta(minutes=1)
        panel.save(update_fields=["deadline"])
        mark_expired_workflow_steps()
        self.proposal.refresh_from_db()
        self.assertEqual(
            self._instance("panel_review").status,
            WorkflowStepInstanceStatuses.EXPIRED,
        )
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(self.proposal.current_step.step, "technical_assessment")
        self.assertEqual(self._instance("allocation_decision").status, "pending")
        self._assess_all()
        self.assertEqual(self._instance("allocation_decision").status, ACTIVE)

    def test_proposals_submitted_before_stages_walk_one_step_at_a_time(self):
        other = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.SUBMITTED
        )
        self.panel.parallel_with_previous = False
        self.panel.save()
        workflow_service.create_step_instances(other)
        other.workflow_step_instances.update(stage=None)
        workflow_service.activate_first_step(other)
        self.assertEqual(
            list(
                other.workflow_step_instances.filter(status=ACTIVE).values_list(
                    "step", flat=True
                )
            ),
            ["technical_assessment"],
        )


class ManualStageTest(ParallelStageBase):
    panel_mode = TransitionModes.MANUAL

    def test_a_manual_step_parks_the_stage(self):
        self._complete(self.panel_member, "panel_review", WorkflowStepOutcomes.APPROVED)
        self.proposal.refresh_from_db()
        self.assertFalse(workflow_service.is_awaiting_manual_advance(self.proposal))
        self._assess_all()
        self.proposal.refresh_from_db()
        self.assertTrue(workflow_service.is_awaiting_manual_advance(self.proposal))
        self.assertEqual(self.proposal.current_step.step, "panel_review")
        self.assertEqual(self._instance("allocation_decision").status, "pending")

        self.client.force_authenticate(self.call_manager)
        response = self.client.post(self._url("advance_workflow_step"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._instance("allocation_decision").status, ACTIVE)


class ParallelStageAuthorisationTest(ParallelStageBase):
    """With two steps active, a user acts only on the one they hold."""

    def test_the_panel_member_cannot_complete_the_technical_assessment(self):
        response = self._complete(
            self.panel_member, "technical_assessment", WorkflowStepOutcomes.FEASIBLE
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._instance("technical_assessment").status, ACTIVE)

    def test_the_panel_member_cannot_reject_at_the_technical_assessment(self):
        response = self._reject(self.panel_member, "technical_assessment")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    def test_an_offering_manager_cannot_complete_the_panel_step(self):
        response = self._complete(
            self.manager_a, "panel_review", WorkflowStepOutcomes.APPROVED
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self._reject(self.manager_a, "panel_review")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._instance("panel_review").status, ACTIVE)

    def test_a_request_not_naming_the_step_is_refused(self):
        for user in (self.panel_member, self.manager_a):
            response = self._complete(user, None, WorkflowStepOutcomes.APPROVED)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            response = self._reject(user, None)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_call_manager_acts_on_each_named_step(self):
        response = self._complete(
            self.call_manager, "panel_review", WorkflowStepOutcomes.APPROVED
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self._instance("panel_review").status,
            WorkflowStepInstanceStatuses.COMPLETED,
        )
        self.assertEqual(self._instance("technical_assessment").status, ACTIVE)

    def test_internal_notes_go_only_to_the_named_step(self):
        response = self._complete(
            self.panel_member,
            "panel_review",
            WorkflowStepOutcomes.APPROVED,
            internal_notes="Panel notes",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._instance("panel_review").internal_notes, "Panel notes")
        self.assertEqual(self._instance("technical_assessment").internal_notes, "")

    def test_round_completion_checks_the_step_completed(self):
        completed, skipped, failed = round_lifecycle.complete_step_for_round(
            self.fixture.round,
            self.panel,
            WorkflowStepOutcomes.APPROVED,
            {},
            self.manager_a,
        )
        self.assertEqual(completed, [])
        entry = next(e for e in skipped if e["proposal_uuid"] == self.proposal.uuid)
        self.assertEqual(
            str(entry["detail"]), "You cannot act on this step of the proposal."
        )
        self.assertEqual(self._instance("panel_review").status, ACTIVE)

        completed, _, _ = round_lifecycle.complete_step_for_round(
            self.fixture.round,
            self.panel,
            WorkflowStepOutcomes.APPROVED,
            {},
            self.panel_member,
        )
        self.assertEqual(len(completed), 1)
        self.assertEqual(self._instance("technical_assessment").status, ACTIVE)

    def test_round_entries_name_every_running_step(self):
        technical = CallWorkflowStep.objects.get(
            call=self.call, key="technical_assessment"
        )
        completed, _, _ = round_lifecycle.complete_step_for_round(
            self.fixture.round,
            self.panel,
            WorkflowStepOutcomes.APPROVED,
            {},
            self.panel_member,
        )
        self.assertEqual(completed[0]["current_step_name"], technical.name)
        other = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.IN_REVIEW
        )
        self.assertIsNone(workflow_service.standing_label(other))
        self.proposal.refresh_from_db()
        fresh = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.SUBMITTED
        )
        workflow_service.create_step_instances(fresh)
        workflow_service.activate_first_step(fresh)
        self.assertEqual(
            workflow_service.standing_label(fresh),
            f"{technical.name} · {self.panel.name}",
        )

    def test_ready_to_complete_follows_the_step(self):
        self.assertFalse(
            workflow_service.is_ready_to_complete(
                self.manager_a, self._instance("panel_review")
            )
        )

    def test_one_steps_contributor_does_not_read_the_other_steps_work(self):
        factories.StepContributionFactory(
            proposal_step=self._instance("panel_review"), contributor=self.panel_member
        )
        self.client.force_authenticate(self.manager_a)
        response = self.client.get(
            self._url("contributions"), {"step_uuid": self.panel.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

        self.client.force_authenticate(self.panel_member)
        response = self.client.get(
            self._url("contributions"), {"step_uuid": self.technical.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])


# =============================================================================
# Configuration
# =============================================================================


class StageAndConditionConfigTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.state = CallStates.DRAFT
        self.call.save()
        self.steps = {
            step.key: step for step in CallWorkflowStep.objects.filter(call=self.call)
        }
        self.client.force_authenticate(self.fixture.call_organizer_user)

    def _patch(self, key, payload):
        step = self.steps[key]
        return self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, step),
            payload,
            format="json",
        )

    def test_a_step_runs_alongside_the_previous_one(self):
        response = self._patch("panel_review", {"parallel_with_previous": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["parallel_with_previous"])

    def test_the_allocation_decision_runs_alone(self):
        self._patch("panel_review", {"is_enabled": True})
        response = self._patch("allocation_decision", {"parallel_with_previous": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.steps["allocation_decision"].refresh_from_db()
        self.assertFalse(self.steps["allocation_decision"].parallel_with_previous)

    def test_an_applicant_step_runs_alone(self):
        response = self._patch("allocation_decision", {"include_award_response": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        award = CallWorkflowStep.objects.get(call=self.call, key="award_response")
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, award),
            {"parallel_with_previous": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_step_does_not_read_a_step_of_its_own_stage(self):
        response = self._patch(
            "panel_review",
            {
                "is_enabled": True,
                "input_steps": [self.steps["expert_review"].uuid.hex],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self._patch("expert_review", {"is_enabled": True})
        response = self._patch("panel_review", {"parallel_with_previous": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_condition_names_an_earlier_step_and_its_outcomes(self):
        response = self._patch(
            "panel_review",
            {
                "run_condition": {
                    "step": "technical_assessment",
                    "outcomes": ["infeasible"],
                }
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["run_condition"],
            {"step": "technical_assessment", "outcomes": ["infeasible"]},
        )
        self.assertEqual(
            response.data["run_condition_step_name"],
            self.steps["technical_assessment"].name,
        )
        # Back to every proposal.
        response = self._patch("panel_review", {"run_condition": None})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["run_condition"], {})

    def test_a_condition_on_a_later_step_is_refused(self):
        response = self._patch(
            "technical_assessment",
            {"run_condition": {"step": "panel_review", "outcomes": ["approved"]}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_condition_on_a_step_of_the_same_stage_is_refused(self):
        self._patch("panel_review", {"parallel_with_previous": True})
        response = self._patch(
            "panel_review",
            {"run_condition": {"step": "expert_review", "outcomes": ["reviewed"]}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_unknown_outcome_is_refused(self):
        response = self._patch(
            "panel_review",
            {"run_condition": {"step": "technical_assessment", "outcomes": ["maybe"]}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_malformed_condition_is_refused(self):
        response = self._patch(
            "panel_review", {"run_condition": {"step": "technical_assessment"}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_allocation_decision_runs_on_every_proposal(self):
        response = self._patch(
            "allocation_decision",
            {
                "run_condition": {
                    "step": "technical_assessment",
                    "outcomes": ["feasible"],
                }
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_step_a_condition_names_cannot_be_deleted(self):
        self._patch(
            "panel_review",
            {
                "run_condition": {
                    "step": "technical_assessment",
                    "outcomes": ["infeasible"],
                }
            },
        )
        response = self.client.delete(
            factories.CallWorkflowStepFactory.get_url(
                self.call, self.steps["technical_assessment"]
            )
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_and_transfer_keep_stages_and_conditions(self):
        self._patch("panel_review", {"parallel_with_previous": True})
        self._patch(
            "allocation_decision",
            {"duration_in_days": 3},
        )
        CallWorkflowStep.objects.filter(pk=self.steps["panel_review"].pk).update(
            run_condition={
                "step": "administrative_check",
                "outcomes": ["eligible"],
            }
        )
        copy = utils.duplicate_call(self.call, "Copy", self.fixture.owner)
        panel = copy.workflow_steps.get(key="panel_review")
        self.assertTrue(panel.parallel_with_previous)
        self.assertEqual(panel.run_condition["step"], "administrative_check")

        document, _ = call_transfer.export_call(self.call)
        imported, _, _ = call_transfer.import_call(
            document, self.call.manager, self.fixture.owner, name="Imported"
        )
        panel = imported.workflow_steps.get(key="panel_review")
        self.assertTrue(panel.parallel_with_previous)
        self.assertEqual(panel.run_condition["outcomes"], ["eligible"])


class TemplatesTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()

    def _steps(self, name):
        call, _, _ = call_templates.import_template(
            name, self.fixture.manager, self.fixture.owner
        )
        return {step.key: step for step in call.workflow_steps.all()}, call

    def test_quantum_emulation_runs_only_after_a_conditional_acceptance(self):
        steps, _ = self._steps("quantum_access")
        self.assertEqual(
            steps["emulation_assessment"].run_condition,
            {
                "step": "technical_assessment",
                "outcomes": [ContributionOutcomes.CONDITIONALLY_ACCEPTED],
            },
        )

    def test_parallel_stages_of_the_templates(self):
        for name, first, second in (
            (
                "ai_factory_large_scale",
                "technical_assessment",
                "innovation_group_evaluation",
            ),
            ("extreme_scale", "technical_assessment", "rapporteur_assignment"),
            ("regular_access", "technical_assessment", "domain_panel_chair_assignment"),
        ):
            steps, call = self._steps(name)
            stages = CallWorkflowStep.stages(
                list(call.workflow_steps.order_by("order", "id"))
            )
            self.assertEqual(
                stages[steps[first].pk], stages[steps[second].pk], (name, second)
            )
            later = [
                step
                for step in steps.values()
                if step.order > steps[second].order and step.is_enabled
            ]
            self.assertTrue(
                all(stages[step.pk] > stages[steps[second].pk] for step in later),
                name,
            )
