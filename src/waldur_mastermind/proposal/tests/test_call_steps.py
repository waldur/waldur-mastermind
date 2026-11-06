"""Call steps: ordered, named steps of a code-defined kind, a kind repeating."""

from datetime import timedelta
from unittest import mock

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import serializers, status, test

from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.fixtures import ProposalRole
from waldur_mastermind.proposal import call_transfer, models, utils, workflow_service
from waldur_mastermind.proposal.enums import (
    STEP_KINDS,
    AllocationTimes,
    CallStates,
    OnNegativeOutcomes,
    OutcomeSets,
    ProposalStates,
    ResponsibleRoles,
    ResultsPublication,
    StepKinds,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures


class CallStepsTestMixin:
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)

    def add_step(self, key, kind, name, order, **values):
        models.CallWorkflowStep.make_room(self.call.pk, order)
        return models.CallWorkflowStep.objects.create(
            call=self.call, key=key, kind=kind, name=name, order=order, **values
        )

    def submit(self, proposal=None):
        proposal = proposal or self.proposal
        proposal.state = ProposalStates.DRAFT
        proposal.save()
        self.client.force_authenticate(self.fixture.proposal_creator)
        response = self.client.post(
            factories.ProposalFactory.get_url(proposal, action="submit")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        proposal.refresh_from_db()
        return proposal

    def complete(self, outcome, proposal=None, user=None):
        proposal = proposal or self.proposal
        proposal.refresh_from_db()
        self.client.force_authenticate(user or self.fixture.staff)
        response = self.client.post(
            factories.ProposalFactory.get_url(
                proposal, action="complete_workflow_step"
            ),
            {"step_uuid": proposal.current_step.uuid.hex, "outcome": outcome},
        )
        proposal.refresh_from_db()
        return response

    def path(self, proposal=None):
        proposal = proposal or self.proposal
        return [
            (instance.step, instance.status)
            for instance in proposal.workflow_step_instances.order_by(
                "call_step__order"
            )
        ]


class RepeatedKindTest(CallStepsTestMixin, test.APITestCase):
    """Two assessments of their own name run one after the other."""

    def setUp(self):
        super().setUp()
        # Seeded: administrative check (0, enabled), technical assessment,
        # expert review, panel review (disabled), allocation decision (4).
        self.individual = self.add_step(
            "individual-report",
            StepKinds.ASSESSMENT,
            "Individual report",
            1,
            completer=ResponsibleRoles.REVIEWER,
        )
        self.consolidated = self.add_step(
            "consolidated-report",
            StepKinds.ASSESSMENT,
            "Consolidated report",
            2,
            completer=ResponsibleRoles.REVIEWER,
            outcome_set=OutcomeSets.DONE,
        )

    def test_steps_of_one_kind_run_in_order(self):
        self.submit()
        self.assertEqual(self.proposal.workflow_step, "administrative_check")

        self.assertEqual(self.complete("eligible").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.workflow_step, "individual-report")

        response = self.complete("reviewed")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["next_step"], "consolidated-report")
        self.assertEqual(response.data["next_step_uuid"], self.consolidated.uuid.hex)
        self.assertIn("Consolidated report", response.data["detail"])

        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.workflow_step, "allocation_decision")

    def test_outcome_set_of_the_row_is_enforced(self):
        self.submit()
        self.complete("eligible")
        # The individual report collects reviews; "done" is the other one's.
        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("outcome", response.data)

    def test_workflow_states_show_the_custom_steps(self):
        self.submit()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        states = [
            (s["step"], s["step_name"], s["kind"], s["call_step_uuid"], s["order"])
            for s in response.data
        ]
        self.assertEqual(
            states[1:3],
            [
                (
                    "individual-report",
                    "Individual report",
                    StepKinds.ASSESSMENT,
                    self.individual.uuid.hex,
                    1,
                ),
                (
                    "consolidated-report",
                    "Consolidated report",
                    StepKinds.ASSESSMENT,
                    self.consolidated.uuid.hex,
                    2,
                ),
            ],
        )
        self.assertEqual(response.data[2]["allowed_outcomes"], [OutcomeSets.DONE])

    def test_order_decides_the_path(self):
        # Swap the two reports through the API before anyone submits.
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, self.consolidated),
            {"order": 1},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.individual.refresh_from_db()
        self.assertEqual(self.individual.order, 2)

        self.submit()
        self.complete("eligible")
        self.assertEqual(self.proposal.workflow_step, "consolidated-report")
        self.complete("done")
        self.assertEqual(self.proposal.workflow_step, "individual-report")

    def test_reaching_the_end_allocates_once(self):
        self.submit()
        self.complete("eligible")
        self.complete("reviewed")
        self.complete("done")
        self.proposal.project = None
        self.proposal.save(update_fields=["project"])
        with mock.patch.object(utils, "allocate_proposal") as allocate:
            response = self.complete("approved")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertIsNone(self.proposal.workflow_step)
        allocate.assert_called_once()


class NegativeOutcomeTest(CallStepsTestMixin, test.APITestCase):
    def test_on_negative_cancel_cancels_the_proposal(self):
        self.add_step(
            "response-to-reviews",
            StepKinds.APPLICANT_ACTION,
            "Response to reviews",
            1,
            outcome_set=OutcomeSets.ACCEPTANCE,
            on_negative=OnNegativeOutcomes.CANCEL,
            completer=ResponsibleRoles.CALL_MANAGER,
        )
        self.submit()
        self.complete("eligible")
        self.assertEqual(self.proposal.workflow_step, "response-to-reviews")
        self.assertEqual(self.complete("declined").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.state, ProposalStates.CANCELED)

    def test_negative_outcome_rejects_by_default(self):
        self.add_step(
            "rapporteur-check",
            StepKinds.DECISION,
            "Rapporteur check",
            1,
            outcome_set=OutcomeSets.APPROVAL,
        )
        self.submit()
        self.complete("eligible")
        self.assertEqual(self.complete("declined").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)

    def test_declining_the_award_cancels(self):
        decision = self.call.workflow_steps.get(key="allocation_decision")
        self.client.force_authenticate(self.fixture.staff)
        self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, decision),
            {"include_award_response": True},
        )
        self.submit()
        self.complete("eligible")
        self.complete("approved")
        self.assertEqual(self.proposal.workflow_step, "award_response")
        self.assertEqual(
            self.complete("declined", user=self.fixture.proposal_creator).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(self.proposal.state, ProposalStates.CANCELED)


class AllocationDecisionTest(CallStepsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.call.state = CallStates.DRAFT
        self.call.save()
        self.decision = self.call.workflow_steps.get(key="allocation_decision")

    def activate(self):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            factories.CallFactory.get_protected_url(self.call, "activate")
        )

    def test_timing_comes_from_the_allocation_decision_row(self):
        # A differently named decision settles the award.
        models.CallWorkflowStep.objects.filter(pk=self.decision.pk).update(
            is_allocation_decision=False
        )
        board = self.add_step(
            "board-decision",
            StepKinds.DECISION,
            "Board decision",
            10,
            is_allocation_decision=True,
            allocation_time=AllocationTimes.FIXED_DATE,
        )
        call_round = self.fixture.round
        call_round.allocation_date = timezone.now() + timedelta(days=30)
        call_round.save()
        self.assertEqual(
            models.CallWorkflowStep.allocation_decision_for(self.call), board
        )
        self.assertEqual(
            utils.allocation_start_date(call_round), call_round.allocation_date.date()
        )

    def test_activation_refuses_a_call_without_an_allocation_decision(self):
        models.CallWorkflowStep.objects.filter(pk=self.decision.pk).update(
            is_allocation_decision=False
        )
        response = self.activate()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_activation_refuses_two_allocation_decisions(self):
        second = self.add_step("second-decision", StepKinds.DECISION, "Second", 10)
        models.CallWorkflowStep.objects.filter(pk=second.pk).update(
            is_allocation_decision=True
        )
        response = self.activate()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exactly one", str(response.data))

    def test_a_second_allocation_decision_cannot_be_configured(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.CallWorkflowStepFactory.get_list_url(self.call),
            {
                "kind": StepKinds.DECISION,
                "name": "Another",
                "is_allocation_decision": True,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is_allocation_decision", response.data)

    def test_only_a_decision_settles_the_award(self):
        self.client.force_authenticate(self.fixture.staff)
        models.CallWorkflowStep.objects.filter(pk=self.decision.pk).update(
            is_allocation_decision=False
        )
        response = self.client.post(
            factories.CallWorkflowStepFactory.get_list_url(self.call),
            {
                "kind": StepKinds.ASSESSMENT,
                "name": "Assessment",
                "is_allocation_decision": True,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AwardResponseOrderTest(CallStepsTestMixin, test.APITestCase):
    """The applicant answers an award once it is decided: the award response
    never comes before the allocation decision."""

    def setUp(self):
        super().setUp()
        self.call.state = CallStates.DRAFT
        self.call.save()
        self.client.force_authenticate(self.fixture.staff)
        self.decision = self.call.workflow_steps.get(key="allocation_decision")
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, self.decision),
            {"include_award_response": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.award = self.call.workflow_steps.get(key="award_response")

    def patch(self, step, payload):
        return self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, step), payload
        )

    def misorder(self):
        """Put the award response first, as an older import could have."""
        models.CallWorkflowStep.make_room(self.call.pk, 0)
        models.CallWorkflowStep.objects.filter(pk=self.award.pk).update(order=0)

    def test_the_award_response_cannot_move_before_the_decision(self):
        response = self.patch(self.award, {"order": 0})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("after the allocation decision", str(response.data["order"]))

    def test_the_decision_cannot_move_after_the_award_response(self):
        self.award.refresh_from_db()
        response = self.patch(self.decision, {"order": self.award.order + 5})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("order", response.data)

    def test_steps_still_move_around_them(self):
        first = self.call.workflow_steps.order_by("order").first()
        self.award.refresh_from_db()
        response = self.patch(first, {"order": self.award.order + 1})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_activation_refuses_an_award_response_before_the_decision(self):
        self.misorder()
        response = self.client.post(
            factories.CallFactory.get_protected_url(self.call, "activate")
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("after the allocation decision", str(response.data))

    def test_an_out_of_order_call_is_still_edited_into_order(self):
        self.misorder()
        response = self.patch(self.decision, {"name": "Board decision"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        last = self.call.workflow_steps.order_by("-order").first().order
        response = self.patch(self.award, {"order": last + 1})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class CallStepConfigurationApiTest(CallStepsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.call.state = CallStates.DRAFT
        self.call.save()
        self.client.force_authenticate(self.fixture.call_organizer_user)
        self.list_url = factories.CallWorkflowStepFactory.get_list_url(self.call)

    def test_create_step_of_a_kind(self):
        response = self.client.post(
            self.list_url,
            {"kind": StepKinds.ASSESSMENT, "name": "Scientific evaluation"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["key"], "scientific-evaluation")
        # Read alias of key.
        self.assertEqual(response.data["step"], "scientific-evaluation")
        self.assertEqual(response.data["outcome_set"], OutcomeSets.REVIEWED)
        self.assertEqual(response.data["completer"], ResponsibleRoles.REVIEWER)
        self.assertEqual(response.data["responsible_role"], ResponsibleRoles.REVIEWER)
        # Appended after the seeded steps, with its kind's notification rules.
        self.assertEqual(response.data["order"], 5)
        self.assertTrue(response.data["notification_rules"])

    def test_same_name_twice_gets_distinct_keys(self):
        payload = {"kind": StepKinds.ASSESSMENT, "name": "Report"}
        first = self.client.post(self.list_url, payload)
        second = self.client.post(self.list_url, payload)
        self.assertEqual(first.data["key"], "report")
        self.assertEqual(second.data["key"], "report-2")

    def test_order_inserts_and_moves_later_steps_down(self):
        response = self.client.post(
            self.list_url,
            {"kind": StepKinds.ASSIGNMENT, "name": "Rapporteur assignment", "order": 1},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            list(self.call.workflow_steps.values_list("key", "order")),
            [
                ("administrative_check", 0),
                ("rapporteur-assignment", 1),
                ("technical_assessment", 2),
                ("expert_review", 3),
                ("panel_review", 4),
                ("allocation_decision", 5),
            ],
        )

    def test_kind_is_required_for_a_new_key(self):
        response = self.client.post(self.list_url, {"name": "Unknown"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("kind", response.data)

    def test_outcome_set_must_belong_to_the_kind(self):
        response = self.client.post(
            self.list_url,
            {
                "kind": StepKinds.DECISION,
                "name": "Odd",
                "outcome_set": OutcomeSets.REVIEWED,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("outcome_set", response.data)

    def test_kind_and_key_cannot_change(self):
        step = self.call.workflow_steps.get(key="expert_review")
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, step),
            {"kind": StepKinds.DECISION, "key": "renamed", "name": "Peer review"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        step.refresh_from_db()
        self.assertEqual(
            (step.kind, step.key, step.name),
            (StepKinds.ASSESSMENT, "expert_review", "Peer review"),
        )

    def test_responsible_role_alias_writes_the_completer(self):
        step = self.call.workflow_steps.get(key="administrative_check")
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, step),
            {"responsible_role": ResponsibleRoles.PANEL_MEMBER},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        step.refresh_from_db()
        self.assertEqual(step.completer, ResponsibleRoles.PANEL_MEMBER)

    def test_include_award_response_adds_the_step_right_after_the_decision(self):
        decision = self.call.workflow_steps.get(key="allocation_decision")
        self.add_step("final-report", StepKinds.APPLICANT_ACTION, "Final report", 5)
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, decision),
            {"include_award_response": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        award = self.call.workflow_steps.get(key="award_response")
        self.assertEqual(
            (award.kind, award.order, award.on_negative, award.is_enabled),
            (StepKinds.APPLICANT_ACTION, 5, OnNegativeOutcomes.CANCEL, True),
        )
        self.assertEqual(self.call.workflow_steps.get(key="final-report").order, 6)

    def test_step_walked_by_a_proposal_cannot_be_deleted(self):
        self.call.state = CallStates.ACTIVE
        self.call.save()
        self.submit()
        step = self.call.workflow_steps.get(key="expert_review")
        self.client.force_authenticate(self.fixture.call_organizer_user)
        response = self.client.delete(
            factories.CallWorkflowStepFactory.get_url(self.call, step)
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(models.CallWorkflowStep.objects.filter(pk=step.pk).exists())

    def test_unwalked_step_can_be_deleted(self):
        step = self.add_step("draft-step", StepKinds.ASSIGNMENT, "Draft step", 9)
        response = self.client.delete(
            factories.CallWorkflowStepFactory.get_url(self.call, step)
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_step_filter_of_notification_rules_matches_the_key(self):
        step = self.add_step("board", StepKinds.DECISION, "Board", 9)
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=step, trigger="step_started", recipient="call_managers"
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            "/api/call-workflow-step-notification-rules/", {"step": "board"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([rule["step"] for rule in response.data], ["board"])

    def patch_step(self, key, payload):
        step = self.call.workflow_steps.get(key=key)
        return self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, step), payload
        )

    def test_the_applicant_completes_only_an_applicant_action(self):
        for key in ("administrative_check", "expert_review", "allocation_decision"):
            with self.subTest(step=key):
                response = self.patch_step(
                    key, {"completer": ResponsibleRoles.APPLICANT}
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("completer", response.data)
        response = self.client.post(
            self.list_url,
            {"kind": StepKinds.APPLICANT_ACTION, "name": "Final report"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["completer"], ResponsibleRoles.APPLICANT)

    def test_assignment_and_applicant_steps_take_no_minimum(self):
        self.add_step("rapporteurs", StepKinds.ASSIGNMENT, "Rapporteurs", 9)
        self.add_step("report", StepKinds.APPLICANT_ACTION, "Report", 10)
        for key, field in (
            ("rapporteurs", "min_contributions"),
            ("report", "min_contributions"),
            ("report", "max_contributions"),
        ):
            with self.subTest(step=key, field=field):
                response = self.patch_step(key, {field: 1})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)

    def test_a_per_offering_step_has_no_maximum(self):
        payload = {
            "kind": StepKinds.ASSESSMENT,
            "name": "Feasibility",
            "outcome_set": OutcomeSets.FEASIBILITY,
            "contributor_source": "requested_offering_team",
            "per_requested_offering": True,
        }
        response = self.client.post(self.list_url, {**payload, "max_contributions": 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("max_contributions", response.data)
        response = self.client.post(self.list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_a_step_mailing_the_applicant_cannot_turn_internal(self):
        step = self.add_step(
            "board", StepKinds.DECISION, "Board", 9, applicant_visible=True
        )
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=step, trigger="step_completed", recipient="applicant"
        )
        response = self.patch_step("board", {"applicant_visible": False})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("applicant_visible", response.data)
        step.notification_rules.all().delete()
        response = self.patch_step("board", {"applicant_visible": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class StepKindsEndpointTest(test.APITestCase):
    def test_lists_the_four_kinds(self):
        fixture = fixtures.ProposalFixture()
        self.client.force_authenticate(fixture.call_manager)
        response = self.client.get("/api/proposal-protected-calls/workflow_step_kinds/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        kinds = {kind["id"]: kind for kind in response.data}
        self.assertEqual(set(kinds), {kind.id for kind in STEP_KINDS})

        assessment = kinds[StepKinds.ASSESSMENT]
        self.assertEqual(assessment["default_completer"], ResponsibleRoles.REVIEWER)
        self.assertEqual(assessment["default_outcome_set"], OutcomeSets.REVIEWED)
        self.assertIn(
            {
                "id": "feasibility",
                "name": "Feasible / infeasible",
                "outcomes": ["feasible", "infeasible"],
            },
            assessment["outcome_sets"],
        )
        self.assertIn("criteria", assessment["config_fields"])
        self.assertIn("name", assessment["config_fields"])
        self.assertNotIn("criteria", kinds[StepKinds.DECISION]["config_fields"])
        self.assertIn(
            "is_allocation_decision", kinds[StepKinds.DECISION]["config_fields"]
        )
        self.assertTrue(kinds[StepKinds.APPLICANT_ACTION]["applicant_facing"])
        self.assertIn(
            {"trigger": "step_started", "recipient": "applicant", "days_before": None},
            kinds[StepKinds.APPLICANT_ACTION]["default_notification_rules"],
        )


class StepUuidQueryParamTest(test.APITestCase):
    """The step-form actions name the call step by uuid as well as by key."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal_submitted
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        self.checklist = checklist_factories.ChecklistFactory()
        self.question = checklist_factories.QuestionFactory(
            checklist=self.checklist, required=True
        )
        # Two assessments; only the second has this checklist.
        models.CallWorkflowStep.make_room(self.call.pk, 3)
        self.step = models.CallWorkflowStep.objects.create(
            call=self.call,
            key="second-review",
            kind=StepKinds.ASSESSMENT,
            name="Second review",
            order=3,
            checklist=self.checklist,
            completer=ResponsibleRoles.REVIEWER,
        )
        factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            call_step=self.step,
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.client.force_authenticate(self.fixture.reviewer_1)

    def url(self, action, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return factories.ProposalFactory.get_url(self.proposal, action=action) + (
            f"?{query}" if query else ""
        )

    def test_read_and_answer_by_step_uuid(self):
        response = self.client.get(
            self.url("step-checklist", step_uuid=self.step.uuid.hex)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["checklist"]["uuid"], str(self.checklist.uuid))

        response = self.client.post(
            self.url("submit-step-checklist-answers", step_uuid=self.step.uuid.hex),
            [{"question_uuid": str(self.question.uuid), "answer_data": "fine"}],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url("step-checklist-responses", step_uuid=self.step.uuid.hex)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)

    def test_key_still_works(self):
        response = self.client.get(self.url("step-checklist", step="second-review"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_step_of_another_call_is_not_found(self):
        other = factories.CallFactory().workflow_steps.first()
        response = self.client.get(self.url("step-checklist", step_uuid=other.uuid.hex))
        self.assertIn(
            response.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
        )

    def test_malformed_step_uuid_is_not_a_server_error(self):
        response = self.client.get(self.url("step-checklist", step_uuid="nope"))
        self.assertIn(
            response.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
        )


class EngineHelpersTest(CallStepsTestMixin, test.APITestCase):
    def test_instance_named_by_key_resolves_its_call_step(self):
        instance = models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal, step="expert_review"
        )
        self.assertEqual(
            instance.call_step, self.call.workflow_steps.get(key="expert_review")
        )

    def test_create_step_instances_follows_order_and_enablement(self):
        self.add_step("first", StepKinds.ASSIGNMENT, "First", 0, is_enabled=False)
        workflow_service.create_step_instances(self.proposal)
        self.assertEqual(
            self.path(),
            [
                ("first", WorkflowStepInstanceStatuses.SKIPPED),
                ("administrative_check", WorkflowStepInstanceStatuses.PENDING),
                ("technical_assessment", WorkflowStepInstanceStatuses.SKIPPED),
                ("expert_review", WorkflowStepInstanceStatuses.SKIPPED),
                ("panel_review", WorkflowStepInstanceStatuses.SKIPPED),
                ("allocation_decision", WorkflowStepInstanceStatuses.PENDING),
            ],
        )


class CopyPathsTest(CallStepsTestMixin, test.APITestCase):
    """duplicate_call and the call export/import keep repeated kinds and names."""

    STEP_FIELDS = (
        "key",
        "kind",
        "name",
        "description",
        "order",
        "outcome_set",
        "on_negative",
        "completer",
        "is_enabled",
        "is_allocation_decision",
    )

    def setUp(self):
        super().setUp()
        self.add_step(
            "individual-report",
            StepKinds.ASSESSMENT,
            "Individual report",
            1,
            description="Each reviewer on their own.",
            completer=ResponsibleRoles.REVIEWER,
        )
        self.add_step(
            "consolidated-report",
            StepKinds.ASSESSMENT,
            "Consolidated report",
            2,
            outcome_set=OutcomeSets.DONE,
            completer=ResponsibleRoles.REVIEWER,
        )
        self.add_step(
            "response-to-reviews",
            StepKinds.APPLICANT_ACTION,
            "Response to reviews",
            3,
            is_enabled=False,
            on_negative=OnNegativeOutcomes.CANCEL,
        )

    def steps(self, call):
        return list(
            call.workflow_steps.order_by("order").values_list(*self.STEP_FIELDS)
        )

    def test_duplicate_keeps_every_step(self):
        new_call = utils.duplicate_call(self.call, "Copy", self.fixture.staff)
        self.assertEqual(self.steps(new_call), self.steps(self.call))
        # The copied custom steps get their kind's notification rules.
        self.assertTrue(
            new_call.workflow_steps.get(
                key="individual-report"
            ).notification_rules.exists()
        )

    def criteria(self, call):
        return list(
            models.WorkflowCriterion.objects.filter(workflow_step__call=call)
            .order_by("workflow_step__key", "order")
            .values_list("workflow_step__key", "name", "order", "weight")
        )

    def test_both_paths_keep_the_criteria_and_their_weights(self):
        step = self.call.workflow_steps.get(key="individual-report")
        step.criteria.create(name="Excellence", order=1, weight=3)
        step.criteria.create(name="Impact", order=2)
        expected = [
            ("individual-report", "Excellence", 1, 3),
            ("individual-report", "Impact", 2, 1),
        ]
        duplicate = utils.duplicate_call(self.call, "Copy", self.fixture.staff)
        self.assertEqual(self.criteria(duplicate), expected)
        data, _ = call_transfer.export_call(self.call)
        imported, _, _ = call_transfer.import_call(
            data, self.call.manager, self.fixture.staff
        )
        self.assertEqual(self.criteria(imported), expected)

    def test_export_import_round_trip(self):
        data, _ = call_transfer.export_call(self.call)
        self.assertEqual(data["schema_version"], call_transfer.SCHEMA_VERSION)
        new_call, imported, warnings = call_transfer.import_call(
            data, self.call.manager, self.fixture.staff
        )
        self.assertEqual(warnings, [])
        self.assertIn("workflow_steps", imported)
        self.assertEqual(self.steps(new_call), self.steps(self.call))

    def test_import_of_an_export_made_before_steps_had_a_kind(self):
        data, _ = call_transfer.export_call(self.call)
        data["schema_version"] = 1
        legacy_fields = {
            "kind",
            "name",
            "description",
            "order",
            "outcome_set",
            "on_negative",
            "is_allocation_decision",
        }
        steps = []
        # The catalogue in the reverse display order an old export listed it in.
        for display_order, entry in enumerate(
            reversed([s for s in data["workflow_steps"] if "-" not in s["key"]])
        ):
            entry = {k: v for k, v in entry.items() if k not in legacy_fields}
            entry["step"] = entry.pop("key")
            entry["responsible_role"] = entry.pop("completer")
            entry["display_order"] = display_order
            steps.append(entry)
        steps.append(
            {
                "step": "award_response",
                "is_enabled": True,
                "responsible_role": "applicant",
                "notification_rules": [],
                "criteria": [],
                "checklist": None,
            }
        )
        data["workflow_steps"] = steps

        new_call, _, warnings = call_transfer.import_call(
            data, self.call.manager, self.fixture.staff
        )
        self.assertEqual(warnings, [])
        self.assertEqual(
            [
                (key, kind, order, is_allocation_decision, on_negative)
                for key, kind, _, _, order, _, on_negative, _, _, is_allocation_decision in self.steps(
                    new_call
                )
            ],
            [
                ("administrative_check", StepKinds.DECISION, 0, False, "reject"),
                ("technical_assessment", StepKinds.ASSESSMENT, 1, False, "reject"),
                ("expert_review", StepKinds.ASSESSMENT, 2, False, "reject"),
                ("panel_review", StepKinds.DECISION, 3, False, "reject"),
                ("allocation_decision", StepKinds.DECISION, 4, True, "reject"),
                ("award_response", StepKinds.APPLICANT_ACTION, 5, False, "cancel"),
            ],
        )
        self.assertEqual(
            new_call.workflow_steps.get(key="expert_review").completer,
            ResponsibleRoles.REVIEWER,
        )

    def test_import_refuses_duplicate_keys(self):
        data, _ = call_transfer.export_call(self.call)
        data["workflow_steps"].append(dict(data["workflow_steps"][1]))
        with self.assertRaises(serializers.ValidationError):
            call_transfer.import_call(data, self.call.manager, self.fixture.staff)


class SnapshotPathTest(CallStepsTestMixin, test.APITestCase):
    """A proposal walks the path it was submitted with."""

    def setUp(self):
        super().setUp()
        self.panel = self.call.workflow_steps.get(key="panel_review")
        self.panel.is_enabled = True
        self.panel.save()
        self.decision = self.call.workflow_steps.get(key="allocation_decision")

    def test_reordering_mid_flight_does_not_skip_the_allocation_decision(self):
        self.submit()
        self.complete("eligible")
        self.assertEqual(self.proposal.workflow_step, "panel_review")

        # Move the panel after the allocation decision while it is active.
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, self.panel),
            {"order": self.decision.order + 1},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.assertEqual(self.complete("approved").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(self.proposal.workflow_step, "allocation_decision")

        # workflow_states keeps the proposal's own order.
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        )
        keys = [s["step"] for s in response.data]
        self.assertLess(keys.index("panel_review"), keys.index("allocation_decision"))

    def test_proposals_submitted_after_a_reorder_follow_it(self):
        self.client.force_authenticate(self.fixture.staff)
        self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, self.panel),
            {"order": 0},
        )
        self.submit()
        self.assertEqual(self.proposal.workflow_step, "panel_review")

    def test_instances_copy_order_and_allocation_decision(self):
        self.submit()
        instance = self.proposal.workflow_step_instances.get(step="allocation_decision")
        self.assertEqual(instance.order, self.decision.order)
        self.assertTrue(instance.is_allocation_decision)


class AwardResponseFlagTest(CallStepsTestMixin, test.APITestCase):
    """The allocation decision's include_award_response says whether the call
    has an enabled award response step, however the step came and went."""

    def setUp(self):
        super().setUp()
        self.call.state = CallStates.DRAFT
        self.call.save()
        self.client.force_authenticate(self.fixture.staff)
        self.decision = self.call.workflow_steps.get(key="allocation_decision")
        self.patch(self.decision, {"include_award_response": True})
        self.award = self.call.workflow_steps.get(key="award_response")

    def patch(self, step, payload):
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, step), payload
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def flag(self):
        self.decision.refresh_from_db()
        return self.decision.include_award_response

    def test_deleting_the_award_step_clears_the_flag(self):
        self.assertTrue(self.flag())
        response = self.client.delete(
            factories.CallWorkflowStepFactory.get_url(self.call, self.award)
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(self.flag())
        # Writing the decision back as it reads does not bring the step back.
        response = self.patch(self.decision, {"name": "Board decision"})
        self.assertFalse(response.data["include_award_response"])
        self.assertFalse(self.call.workflow_steps.filter(key="award_response").exists())

    def test_disabling_and_enabling_the_step_follow(self):
        self.patch(self.award, {"is_enabled": False})
        self.assertFalse(self.flag())
        self.patch(self.award, {"is_enabled": True})
        self.assertTrue(self.flag())

    def test_without_the_step_the_decision_settles_the_award(self):
        self.client.delete(
            factories.CallWorkflowStepFactory.get_url(self.call, self.award)
        )
        self.call.state = CallStates.ACTIVE
        self.call.save()
        self.submit()
        self.complete("eligible")
        self.assertEqual(self.complete("approved").status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)


class AllocationDecisionProtectionTest(CallStepsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.decision = self.call.workflow_steps.get(key="allocation_decision")
        self.url = factories.CallWorkflowStepFactory.get_url(self.call, self.decision)
        self.client.force_authenticate(self.fixture.staff)

    def test_active_call_keeps_its_allocation_decision(self):
        self.assertEqual(self.call.state, CallStates.ACTIVE)
        response = self.client.patch(self.url, {"is_allocation_decision": False})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.patch(self.url, {"is_enabled": False})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.decision.refresh_from_db()
        self.assertTrue(self.decision.is_allocation_decision)
        self.assertTrue(self.decision.is_enabled)

    def test_allocation_decision_cannot_move_once_proposals_are_submitted(self):
        self.submit()
        self.call.state = CallStates.DRAFT
        self.call.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(self.url, {"is_allocation_decision": False})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        panel = self.call.workflow_steps.get(key="panel_review")
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, panel),
            {"is_allocation_decision": True},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_draft_call_without_proposals_can_move_it(self):
        self.call.state = CallStates.DRAFT
        self.call.save()
        response = self.client.patch(self.url, {"is_allocation_decision": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class HeldDecisionSnapshotTest(CallStepsTestMixin, test.APITestCase):
    """A held decision is released or reopened from the proposal's own record."""

    def setUp(self):
        super().setUp()
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save()
        self.submit()
        self.complete("eligible")
        self.assertEqual(self.complete("approved").status_code, status.HTTP_200_OK)
        self.assertTrue(self.proposal.decision_held)
        # The call's configuration changes after the decision was held.
        models.CallWorkflowStep.objects.filter(
            call=self.call, key="allocation_decision"
        ).update(is_allocation_decision=False)

    def test_release_still_carries_out_the_decision(self):
        with mock.patch.object(utils, "allocate_proposal"):
            with transaction.atomic():
                workflow_service.release_held_decision(self.proposal)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertFalse(self.proposal.decision_held)

    def test_reopen_still_finds_the_decision(self):
        instance = workflow_service.reopen_held_decision(
            self.proposal, self.fixture.staff, "Board changed its mind"
        )
        self.assertEqual(instance.step, "allocation_decision")
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)


class ReviewConcealmentTest(CallStepsTestMixin, test.APITestCase):
    """What the applicant reads of each step when the call keeps reviews private."""

    def setUp(self):
        super().setUp()
        self.call.reviews_visible_to_submitters = False
        self.call.save()
        self.call.workflow_steps.filter(key="expert_review").update(
            completer=ResponsibleRoles.CALL_MANAGER
        )
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        outcomes = {
            "administrative_check": ("rejected", "Missing ethics statement"),
            "technical_assessment": ("feasible", "Fits the partition"),
            "expert_review": ("reviewed", "Weak methodology"),
            "panel_review": ("approved", "Ranked third"),
            "allocation_decision": ("approved", "Granted in full"),
        }
        for key, (outcome, reason) in outcomes.items():
            factories.ProposalWorkflowStepInstanceFactory(
                proposal=self.proposal,
                step=key,
                status=WorkflowStepInstanceStatuses.COMPLETED,
                outcome=outcome,
                outcome_reason=reason,
            )
        self.client.force_authenticate(self.fixture.proposal_creator)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.states = {state["step"]: state for state in response.data}

    def test_admin_check_rejection_reason_is_visible(self):
        state = self.states["administrative_check"]
        self.assertEqual(state["outcome"], "rejected")
        self.assertEqual(state["rejection_reason"], "Missing ethics statement")

    def test_technical_assessment_hidden_from_the_applicant_is_masked(self):
        # The step is kept from the applicant (``applicant_visible`` off), as
        # by default: the applicant team reads neither its verdict nor why.
        state = self.states["technical_assessment"]
        self.assertIsNone(state["outcome"])
        self.assertEqual(state["outcome_reason"], "")
        self.assertIsNone(state["completed_by"])

    def test_technical_assessment_shown_to_the_applicant_is_visible(self):
        models.CallWorkflowStep.objects.filter(
            call=self.call, key="technical_assessment"
        ).update(applicant_visible=True)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        state = {row["step"]: row for row in response.data}["technical_assessment"]
        self.assertEqual(state["outcome"], "feasible")
        self.assertEqual(state["outcome_reason"], "Fits the partition")

    def test_expert_review_is_hidden_whoever_completes_it(self):
        state = self.states["expert_review"]
        self.assertEqual(state["completer"], ResponsibleRoles.CALL_MANAGER)
        self.assertIsNone(state["outcome"])
        self.assertEqual(state["outcome_reason"], "")

    def test_panel_review_is_hidden(self):
        state = self.states["panel_review"]
        self.assertIsNone(state["outcome"])
        self.assertEqual(state["outcome_reason"], "")
        self.assertIsNone(state["rejection_reason"])

    def test_allocation_decision_follows_its_own_rules(self):
        # Not review content: shown unless the round holds the decision back.
        state = self.states["allocation_decision"]
        self.assertEqual(state["outcome"], "approved")
        self.assertEqual(state["outcome_reason"], "Granted in full")


class CallStepOrderLimitsTest(CallStepsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.call.state = CallStates.DRAFT
        self.call.save()
        self.client.force_authenticate(self.fixture.staff)
        self.list_url = factories.CallWorkflowStepFactory.get_list_url(self.call)

    def test_order_has_an_upper_bound(self):
        response = self.client.post(
            self.list_url,
            {"kind": StepKinds.ASSIGNMENT, "name": "Far", "order": 3_000_000_000},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("order", response.data)

    def test_append_after_the_last_order_renumbers(self):
        response = self.client.post(
            self.list_url,
            {
                "kind": StepKinds.ASSIGNMENT,
                "name": "Last",
                "order": models.CallWorkflowStep.MAX_ORDER,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.client.post(
            self.list_url, {"kind": StepKinds.ASSIGNMENT, "name": "After"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            list(self.call.workflow_steps.values_list("key", flat=True))[-2:],
            ["last", "after"],
        )
        self.assertLessEqual(
            max(self.call.workflow_steps.values_list("order", flat=True)),
            models.CallWorkflowStep.MAX_ORDER,
        )

    def test_writes_lock_the_call(self):
        with CaptureQueriesContext(connection) as queries:
            self.client.post(
                self.list_url, {"kind": StepKinds.ASSIGNMENT, "name": "Locked"}
            )
        self.assertTrue(
            any(
                "FOR UPDATE" in query["sql"] and '"proposal_call"' in query["sql"]
                for query in queries.captured_queries
            )
        )


class CopyParkingTest(CallStepsTestMixin, test.APITestCase):
    """Incoming orders above the seeded ones do not collide while copying."""

    def setUp(self):
        super().setUp()
        self.call.workflow_steps.exclude(key="allocation_decision").delete()
        models.CallWorkflowStep.objects.filter(
            call=self.call, key="allocation_decision"
        ).update(order=12)
        self.add_step("z", StepKinds.DECISION, "Z", 11)

    def test_duplicate(self):
        new_call = utils.duplicate_call(self.call, "Copy", self.fixture.staff)
        self.assertEqual(
            list(new_call.workflow_steps.values_list("key", "order")),
            [("z", 11), ("allocation_decision", 12)],
        )

    def test_export_and_import(self):
        data, _ = call_transfer.export_call(self.call)
        new_call, _, warnings = call_transfer.import_call(
            data, self.call.manager, self.fixture.staff
        )
        self.assertEqual(warnings, [])
        self.assertEqual(
            list(new_call.workflow_steps.values_list("key", "order")),
            [("z", 11), ("allocation_decision", 12)],
        )
