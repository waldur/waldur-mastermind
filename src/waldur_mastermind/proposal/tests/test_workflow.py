from datetime import timedelta
from unittest import mock

from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.enums import (
    WORKFLOW_STEPS,
    CallStates,
    ProposalStates,
    ResponsibleRoles,
    TransitionModes,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.models import (
    CallWorkflowStep,
    Proposal,
    ProposalWorkflowStepInstance,
    WorkflowCriterion,
)
from waldur_mastermind.proposal.tasks import mark_expired_workflow_steps
from waldur_mastermind.proposal.tests import factories, fixtures


class WorkflowStepCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.state = CallStates.DRAFT
        self.call.save()
        # Call creation seeds all 6 workflow steps as enabled. Clear them so
        # these tests can exercise the create endpoint from a clean slate.
        CallWorkflowStep.objects.filter(call=self.call).delete()

    def test_create_workflow_step(self):
        user = self.fixture.call_organizer_user
        self.client.force_authenticate(user)
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {
            "step": "administrative_check",
            "is_enabled": True,
            "duration_in_days": 5,
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            CallWorkflowStep.objects.filter(
                call=self.call, step="administrative_check"
            ).exists()
        )

    def test_create_duplicate_step_fails(self):
        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        user = self.fixture.call_organizer_user
        self.client.force_authenticate(user)
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {"step": "administrative_check", "is_enabled": True}
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class WorkflowStepUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.state = CallStates.DRAFT
        self.call.save()
        self.workflow_step = factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check"
        )

    def test_update_workflow_step_duration(self):
        user = self.fixture.call_organizer_user
        self.client.force_authenticate(user)
        url = factories.CallWorkflowStepFactory.get_url(self.call, self.workflow_step)
        payload = {"duration_in_days": 10}
        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.workflow_step.refresh_from_db()
        self.assertEqual(self.workflow_step.duration_in_days, 10)


class ProposalSubmitWorkflowTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal

        # Configure workflow steps. The call creation signal seeds all 6 steps
        # as enabled by default; the factory uses update_or_create so these
        # calls tune durations on the seeded rows. expert_review is explicitly
        # disabled to exercise the SKIPPED submit-time branch.
        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", duration_in_days=5
        )
        factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", duration_in_days=10
        )
        factories.CallWorkflowStepFactory(
            call=self.call, step="expert_review", is_enabled=False
        )

    def test_submit_creates_step_instances(self):
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        user = self.fixture.proposal_creator
        self.client.force_authenticate(user)
        url = factories.ProposalFactory.get_url(self.proposal, action="submit")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(self.proposal.workflow_step, "administrative_check")

        instances = ProposalWorkflowStepInstance.objects.filter(proposal=self.proposal)
        self.assertEqual(instances.count(), 6)  # All 6 step types

        active = instances.get(step="administrative_check")
        self.assertEqual(active.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNotNone(active.started_at)
        self.assertIsNotNone(active.deadline)

        alloc = instances.get(step="allocation_decision")
        self.assertEqual(alloc.status, WorkflowStepInstanceStatuses.PENDING)

        # Disabled steps should be skipped
        expert = instances.get(step="expert_review")
        self.assertEqual(expert.status, WorkflowStepInstanceStatuses.SKIPPED)

    def test_submit_without_workflow_steps_stays_submitted(self):
        # Remove all workflow steps
        CallWorkflowStep.objects.filter(call=self.call).delete()

        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        user = self.fixture.proposal_creator
        self.client.force_authenticate(user)
        url = factories.ProposalFactory.get_url(self.proposal, action="submit")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.SUBMITTED)
        self.assertIsNone(self.proposal.workflow_step)

    def test_submit_is_atomic_on_failure(self):
        # If anything between the first instance create and proposal.save()
        # fails, no step instances must remain and the proposal stays in DRAFT.
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        user = self.fixture.proposal_creator
        self.client.force_authenticate(user)
        url = factories.ProposalFactory.get_url(self.proposal, action="submit")

        with mock.patch.object(
            Proposal, "save", autospec=True, side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(url)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)
        self.assertEqual(
            ProposalWorkflowStepInstance.objects.filter(proposal=self.proposal).count(),
            0,
        )

    def test_resubmit_does_not_duplicate_step_instances(self):
        # The outer StateValidator catches re-submits of an already-submitted
        # proposal; this test pins the contract — a second POST must not
        # create a parallel set of step instances and trip unique_together.
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        user = self.fixture.proposal_creator
        self.client.force_authenticate(user)
        url = factories.ProposalFactory.get_url(self.proposal, action="submit")

        first = self.client.post(url)
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        first_count = ProposalWorkflowStepInstance.objects.filter(
            proposal=self.proposal
        ).count()

        second = self.client.post(url)
        self.assertNotEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(
            ProposalWorkflowStepInstance.objects.filter(proposal=self.proposal).count(),
            first_count,
        )


class CompleteWorkflowStepTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )

    def test_complete_step_advances_to_next(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        response = self.client.post(
            url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": "eligible",
                "outcome_reason": "All criteria met",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "allocation_decision")

        admin_step = ProposalWorkflowStepInstance.objects.get(
            proposal=self.proposal, step="administrative_check"
        )
        self.assertEqual(admin_step.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(admin_step.outcome, "eligible")

    def test_complete_last_step_accepts_proposal(self):
        self.proposal.workflow_step = "allocation_decision"
        self.proposal.save()

        admin_instance = ProposalWorkflowStepInstance.objects.get(
            proposal=self.proposal, step="administrative_check"
        )
        admin_instance.status = WorkflowStepInstanceStatuses.COMPLETED
        admin_instance.save()

        alloc_instance = ProposalWorkflowStepInstance.objects.get(
            proposal=self.proposal, step="allocation_decision"
        )
        alloc_instance.status = WorkflowStepInstanceStatuses.ACTIVE
        alloc_instance.started_at = timezone.now()
        alloc_instance.save()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        response = self.client.post(
            url,
            {"step_uuid": alloc_instance.uuid.hex, "outcome": "approved"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)

    def test_cannot_complete_when_not_in_review(self):
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        response = self.client.post(
            url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "eligible"},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class RejectWorkflowStepTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )

    def test_reject_step_rejects_proposal(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="reject_workflow_step"
        )
        response = self.client.post(
            url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "reason": "Institution not eligible",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)

        step = ProposalWorkflowStepInstance.objects.get(
            proposal=self.proposal, step="administrative_check"
        )
        self.assertEqual(step.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(step.outcome, "rejected")
        self.assertEqual(step.outcome_reason, "Institution not eligible")


class WorkflowStatesListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", duration_in_days=12
        )
        ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )

    def test_list_workflow_states(self):
        user = self.fixture.proposal_creator
        self.client.force_authenticate(user)
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        active_states = [s for s in response.data if s["status"] == "active"]
        self.assertEqual(len(active_states), 1)
        self.assertEqual(active_states[0]["step"], "administrative_check")

    def test_workflow_states_expose_responsible_role(self):
        # administrative_check has a CallWorkflowStep (catalog default: call_manager).
        # allocation_decision has no CallWorkflowStep, so falls back to catalog default.
        self.client.force_authenticate(self.fixture.proposal_creator)
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        roles = {s["step"]: s["responsible_role"] for s in response.data}
        self.assertEqual(roles["administrative_check"], ResponsibleRoles.CALL_MANAGER)
        self.assertEqual(roles["allocation_decision"], ResponsibleRoles.CALL_MANAGER)

    def test_workflow_states_respect_call_step_role_override(self):
        admin_step = CallWorkflowStep.objects.get(
            call=self.call, step="administrative_check"
        )
        admin_step.responsible_role = ResponsibleRoles.OFFERING_MANAGER
        admin_step.save()

        self.client.force_authenticate(self.fixture.proposal_creator)
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        roles = {s["step"]: s["responsible_role"] for s in response.data}
        self.assertEqual(
            roles["administrative_check"], ResponsibleRoles.OFFERING_MANAGER
        )
        # allocation_decision still falls back to catalog default.
        self.assertEqual(roles["allocation_decision"], ResponsibleRoles.CALL_MANAGER)


class WorkflowStepResponsibilityAndTransitionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.state = CallStates.DRAFT
        self.call.save()

    def test_responsible_role_set_from_catalog_on_create(self):
        cases = {
            "administrative_check": ResponsibleRoles.CALL_MANAGER,
            "technical_assessment": ResponsibleRoles.OFFERING_MANAGER,
            "expert_review": ResponsibleRoles.REVIEWER,
            "panel_review": ResponsibleRoles.PANEL_MEMBER,
            "allocation_decision": ResponsibleRoles.CALL_MANAGER,
            "award_response": ResponsibleRoles.APPLICANT,
        }
        for step_id, expected_role in cases.items():
            step = factories.CallWorkflowStepFactory(call=self.call, step=step_id)
            self.assertEqual(
                step.responsible_role,
                expected_role,
                f"Wrong default role for step '{step_id}'",
            )

    def test_partial_update_changes_responsible_role(self):
        workflow_step = factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check"
        )
        self.client.force_authenticate(self.fixture.call_organizer_user)
        url = factories.CallWorkflowStepFactory.get_url(self.call, workflow_step)
        response = self.client.patch(
            url, {"responsible_role": ResponsibleRoles.OFFERING_MANAGER}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        workflow_step.refresh_from_db()
        self.assertEqual(
            workflow_step.responsible_role, ResponsibleRoles.OFFERING_MANAGER
        )

    def test_transition_mode_default_is_automatic(self):
        workflow_step = factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check"
        )
        self.assertEqual(
            workflow_step.transition_mode, TransitionModes.AUTOMATIC_ON_COMPLETION
        )

    def test_db_loaded_step_with_null_role_is_not_mutated(self):
        # The catalog default is applied on save(), not on hydration. A row
        # whose responsible_role is genuinely NULL must stay NULL when read
        # back from the DB so refresh_from_db / queries reflect storage truth.
        step = factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check"
        )
        CallWorkflowStep.objects.filter(pk=step.pk).update(responsible_role=None)
        reloaded = CallWorkflowStep.objects.get(pk=step.pk)
        self.assertIsNone(reloaded.responsible_role)


class WorkflowStepChecklistNameTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.state = CallStates.DRAFT
        self.call.save()

    def test_serializer_returns_checklist_name_when_set(self):
        checklist = checklist_factories.ChecklistFactory(name="Eligibility form")
        workflow_step = factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", checklist=checklist
        )
        self.client.force_authenticate(self.fixture.call_organizer_user)
        url = factories.CallWorkflowStepFactory.get_url(self.call, workflow_step)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["checklist_name"], "Eligibility form")

    def test_serializer_returns_null_checklist_name_when_no_checklist(self):
        workflow_step = factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", checklist=None
        )
        self.client.force_authenticate(self.fixture.call_organizer_user)
        url = factories.CallWorkflowStepFactory.get_url(self.call, workflow_step)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["checklist_name"])


class WorkflowStepCriteriaTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.state = CallStates.DRAFT
        self.call.save()
        # Call creation seeds workflow steps; clear so these tests can exercise
        # the create endpoint from a clean state.
        CallWorkflowStep.objects.filter(call=self.call).delete()
        self.client.force_authenticate(self.fixture.call_organizer_user)

    def test_create_step_with_criteria(self):
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {
            "step": "expert_review",
            "is_enabled": True,
            "criteria": [
                {"name": "Scientific merit", "order": 1},
                {"name": "Feasibility", "order": 2},
            ],
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        step = CallWorkflowStep.objects.get(call=self.call, step="expert_review")
        self.assertEqual(step.criteria.count(), 2)
        names = list(step.criteria.order_by("order").values_list("name", flat=True))
        self.assertEqual(names, ["Scientific merit", "Feasibility"])

    def test_update_step_replaces_criteria(self):
        step = factories.CallWorkflowStepFactory(call=self.call, step="expert_review")
        WorkflowCriterion.objects.create(
            workflow_step=step, name="Old criterion", order=0
        )
        url = factories.CallWorkflowStepFactory.get_url(self.call, step)
        payload = {
            "criteria": [
                {"name": "New criterion A", "order": 1},
                {"name": "New criterion B", "order": 2},
            ],
        }
        response = self.client.patch(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = set(step.criteria.values_list("name", flat=True))
        self.assertEqual(names, {"New criterion A", "New criterion B"})

    def test_remove_criterion_via_partial_update(self):
        step = factories.CallWorkflowStepFactory(call=self.call, step="expert_review")
        WorkflowCriterion.objects.create(workflow_step=step, name="Keep", order=1)
        WorkflowCriterion.objects.create(workflow_step=step, name="Drop", order=2)
        url = factories.CallWorkflowStepFactory.get_url(self.call, step)
        payload = {"criteria": [{"name": "Keep", "order": 1}]}
        response = self.client.patch(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = list(step.criteria.values_list("name", flat=True))
        self.assertEqual(names, ["Keep"])


class WorkflowStepValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.state = CallStates.DRAFT
        self.call.save()
        # Call creation seeds workflow steps; clear so these tests can exercise
        # the create endpoint and its validators from a clean state.
        CallWorkflowStep.objects.filter(call=self.call).delete()
        self.client.force_authenticate(self.fixture.call_organizer_user)

    def test_include_award_response_rejected_on_non_allocation_step(self):
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {
            "step": "administrative_check",
            "is_enabled": True,
            "include_award_response": True,
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("include_award_response", response.data)

    def test_include_award_response_allowed_on_allocation_decision(self):
        step = factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision"
        )
        url = factories.CallWorkflowStepFactory.get_url(self.call, step)
        response = self.client.patch(
            url, {"include_award_response": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_criteria_rejected_on_non_expert_review_step(self):
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {
            "step": "administrative_check",
            "is_enabled": True,
            "criteria": [{"name": "Some criterion", "order": 1}],
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("criteria", response.data)

    def test_cannot_disable_mandatory_step(self):
        step = factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", is_enabled=True
        )
        url = factories.CallWorkflowStepFactory.get_url(self.call, step)
        response = self.client.patch(url, {"is_enabled": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_enable_panel_review_without_expert_review(self):
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {"step": "panel_review", "is_enabled": True}
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_panel_review_allowed_when_expert_review_enabled(self):
        factories.CallWorkflowStepFactory(
            call=self.call, step="expert_review", is_enabled=True
        )
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {"step": "panel_review", "is_enabled": True}
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class WorkflowStepDisplayOrderTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.state = CallStates.DRAFT
        self.call.save()
        # These tests assert the exact listing; wipe the seeded rows so only
        # the steps explicitly created by each test appear.
        CallWorkflowStep.objects.filter(call=self.call).delete()
        self.client.force_authenticate(self.fixture.call_organizer_user)

    def test_display_order_overrides_catalog_order_in_listing(self):
        # Catalog order: administrative_check (0), technical_assessment (1),
        # expert_review (2), allocation_decision (4)
        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", display_order=10
        )
        factories.CallWorkflowStepFactory(call=self.call, step="technical_assessment")
        factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", display_order=0
        )

        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        steps = [s["step"] for s in response.data]
        # allocation_decision (override 0) -> technical_assessment (catalog 1)
        # -> administrative_check (override 10)
        self.assertEqual(
            steps,
            ["allocation_decision", "technical_assessment", "administrative_check"],
        )

    def test_default_listing_uses_catalog_order(self):
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")
        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="technical_assessment")

        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        steps = [s["step"] for s in response.data]
        self.assertEqual(
            steps,
            ["administrative_check", "technical_assessment", "allocation_decision"],
        )


class WorkflowStepConcurrencyGuardTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.pending_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        self.client.force_authenticate(self.fixture.staff)
        self.complete_url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        self.reject_url = factories.ProposalFactory.get_url(
            self.proposal, action="reject_workflow_step"
        )

    def test_complete_step_rejects_mismatched_step_uuid(self):
        bogus_uuid = "00000000000000000000000000000001"
        response = self.client.post(
            self.complete_url, {"step_uuid": bogus_uuid, "outcome": "eligible"}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_complete_step_with_correct_step_uuid_succeeds(self):
        response = self.client.post(
            self.complete_url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "eligible"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_complete_step_rejects_request_for_skipped_status(self):
        response = self.client.post(
            self.complete_url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "skipped"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_complete_step_rejects_reserved_rejected_outcome(self):
        # 'rejected' belongs to reject_workflow_step; allowing it via complete
        # would flag the step as rejected but still accept the proposal.
        response = self.client.post(
            self.complete_url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "rejected"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_complete_step_rejects_reserved_expired_outcome(self):
        response = self.client.post(
            self.complete_url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "expired"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_complete_step_rejects_request_for_prior_step(self):
        # Mark current as completed and advance to next active instance
        self.active_instance.status = WorkflowStepInstanceStatuses.COMPLETED
        self.active_instance.save()
        self.pending_instance.status = WorkflowStepInstanceStatuses.ACTIVE
        self.pending_instance.started_at = timezone.now()
        self.pending_instance.save()
        self.proposal.workflow_step = "allocation_decision"
        self.proposal.save()

        # Posting the now-completed earlier instance's uuid must 409.
        # Use 'approved' so it survives the per-step outcome allow-list and
        # we genuinely test the lock check.
        response = self.client.post(
            self.complete_url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "approved"},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_reject_step_rejects_mismatched_step_uuid(self):
        bogus_uuid = "00000000000000000000000000000002"
        response = self.client.post(
            self.reject_url, {"step_uuid": bogus_uuid, "reason": "nope"}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class MarkExpiredWorkflowStepsTaskTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

    def test_overdue_active_step_advances_to_next_enabled_step(self):
        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", duration_in_days=3
        )
        overdue = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now() - timedelta(days=2),
            deadline=timezone.now() - timedelta(hours=1),
        )
        next_pending = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )

        count = mark_expired_workflow_steps()
        self.assertEqual(count, 1)

        overdue.refresh_from_db()
        next_pending.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertEqual(overdue.status, WorkflowStepInstanceStatuses.EXPIRED)
        self.assertEqual(overdue.outcome, "expired")
        self.assertEqual(next_pending.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNotNone(next_pending.deadline)
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(self.proposal.workflow_step, "allocation_decision")

    def test_overdue_terminal_step_rejects_proposal(self):
        # Only one enabled step — when it expires, no further step exists.
        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        overdue = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now() - timedelta(days=2),
            deadline=timezone.now() - timedelta(hours=1),
        )

        count = mark_expired_workflow_steps()
        self.assertEqual(count, 1)

        overdue.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertEqual(overdue.status, WorkflowStepInstanceStatuses.EXPIRED)
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)
        self.assertIsNone(self.proposal.workflow_step)

    def test_mark_expired_does_not_touch_future_or_non_active_steps(self):
        # Active with future deadline — must stay untouched.
        future = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
            deadline=timezone.now() + timedelta(days=1),
        )
        # Completed with past deadline — must stay COMPLETED.
        other_proposal = factories.ProposalFactory(round=self.fixture.round)
        completed = ProposalWorkflowStepInstance.objects.create(
            proposal=other_proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.COMPLETED,
            started_at=timezone.now() - timedelta(days=2),
            deadline=timezone.now() - timedelta(hours=1),
        )

        count = mark_expired_workflow_steps()
        self.assertEqual(count, 0)

        future.refresh_from_db()
        completed.refresh_from_db()
        self.assertEqual(future.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertEqual(completed.status, WorkflowStepInstanceStatuses.COMPLETED)


class WorkflowStepResponsibleRolePermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        factories.CallWorkflowStepFactory(
            call=self.call,
            step="administrative_check",
            responsible_role=ResponsibleRoles.CALL_MANAGER,
        )
        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )

    def test_complete_step_forbidden_for_user_without_responsible_role(self):
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.post(
            self.url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "eligible"},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_complete_step_allowed_for_user_with_responsible_role(self):
        manager = structure_factories.UserFactory()
        self.call.add_user(manager, CallRole.MANAGER)
        self.client.force_authenticate(manager)
        response = self.client.post(
            self.url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "eligible"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class WorkflowStepPanelMemberFailsClosedTest(test.APITestCase):
    """panel_member has no system role yet — must fail closed instead of allowing."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "panel_review"
        self.proposal.save()

        # panel_review depends on expert_review being enabled.
        factories.CallWorkflowStepFactory(call=self.call, step="expert_review")
        factories.CallWorkflowStepFactory(
            call=self.call,
            step="panel_review",
            responsible_role=ResponsibleRoles.PANEL_MEMBER,
        )
        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="panel_review",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )

    def test_panel_member_step_denies_call_manager(self):
        manager = structure_factories.UserFactory()
        self.call.add_user(manager, CallRole.MANAGER)
        self.client.force_authenticate(manager)
        response = self.client.post(
            self.url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "approved"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_panel_member_step_denies_reviewer(self):
        reviewer = structure_factories.UserFactory()
        self.call.add_user(reviewer, CallRole.REVIEWER)
        self.client.force_authenticate(reviewer)
        response = self.client.post(
            self.url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "approved"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class WorkflowStepActiveUniqueConstraintTest(test.APITransactionTestCase):
    """At most one workflow step instance per proposal may be ACTIVE at a time."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal

    def test_cannot_have_two_active_steps_on_same_proposal(self):
        ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        with self.assertRaises(IntegrityError):
            with db_transaction.atomic():
                ProposalWorkflowStepInstance.objects.create(
                    proposal=self.proposal,
                    step="allocation_decision",
                    status=WorkflowStepInstanceStatuses.ACTIVE,
                    started_at=timezone.now(),
                )


class WorkflowConfigDriftTest(test.APITestCase):
    """Workflow path is fixed at submit; live call-config edits don't retro-apply."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        # Submit-time enabled set: administrative_check, allocation_decision.
        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", is_enabled=True
        )
        self.alloc_step_config = factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", is_enabled=True
        )
        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        # Submit-time skipped set (these instances exist with status SKIPPED).
        for step_id in (
            "technical_assessment",
            "expert_review",
            "panel_review",
            "award_response",
        ):
            ProposalWorkflowStepInstance.objects.create(
                proposal=self.proposal,
                step=step_id,
                status=WorkflowStepInstanceStatuses.SKIPPED,
            )
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_completing_advances_to_submit_time_next_even_if_call_config_changed(
        self,
    ):
        # Admin enables expert_review on the call after submit. Existing
        # proposal must ignore this (its expert_review instance is SKIPPED)
        # and still advance to allocation_decision.
        factories.CallWorkflowStepFactory(
            call=self.call, step="expert_review", is_enabled=True
        )
        response = self.client.post(
            self.url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": WorkflowStepOutcomes.ELIGIBLE,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "allocation_decision")

    def test_disabling_downstream_step_after_submit_does_not_affect_proposal(self):
        # Admin disables allocation_decision after submit; the proposal still
        # has a PENDING instance for it and must still advance there.
        self.alloc_step_config.is_enabled = False
        self.alloc_step_config.save()
        response = self.client.post(
            self.url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": WorkflowStepOutcomes.ELIGIBLE,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "allocation_decision")


class ManualTransitionTest(test.APITestCase):
    """Steps configured with transition_mode=manual require a separate advance call."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        factories.CallWorkflowStepFactory(
            call=self.call,
            step="administrative_check",
            transition_mode=TransitionModes.MANUAL,
        )
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )

        self.complete_url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        self.advance_url = factories.ProposalFactory.get_url(
            self.proposal, action="advance_workflow_step"
        )

    def _complete_manual_step(self):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            self.complete_url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": WorkflowStepOutcomes.ELIGIBLE,
                "outcome_reason": "All criteria met",
            },
        )

    def test_manual_completion_does_not_advance(self):
        response = self._complete_manual_step()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # No next_step in response — step is parked awaiting advance.
        self.assertNotIn("next_step", response.data)
        self.assertNotIn("proposal_state", response.data)

        self.active_instance.refresh_from_db()
        self.assertEqual(
            self.active_instance.status, WorkflowStepInstanceStatuses.COMPLETED
        )
        self.assertEqual(self.active_instance.outcome, WorkflowStepOutcomes.ELIGIBLE)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        # workflow_step still points at the manual step.
        self.assertEqual(self.proposal.workflow_step, "administrative_check")

        # Downstream step is still PENDING — was not activated.
        alloc = ProposalWorkflowStepInstance.objects.get(
            proposal=self.proposal, step="allocation_decision"
        )
        self.assertEqual(alloc.status, WorkflowStepInstanceStatuses.PENDING)

    def test_manual_advance_happy_path(self):
        self._complete_manual_step()

        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(self.advance_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["next_step"], "allocation_decision")

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "allocation_decision")

        alloc = ProposalWorkflowStepInstance.objects.get(
            proposal=self.proposal, step="allocation_decision"
        )
        self.assertEqual(alloc.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNotNone(alloc.started_at)

    def test_manual_advance_terminates_workflow(self):
        # Remove the downstream step so the manual step is terminal.
        CallWorkflowStep.objects.filter(
            call=self.call, step="allocation_decision"
        ).delete()
        ProposalWorkflowStepInstance.objects.filter(
            proposal=self.proposal, step="allocation_decision"
        ).delete()

        self._complete_manual_step()

        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(self.advance_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["proposal_state"], ProposalStates.ACCEPTED)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertIsNone(self.proposal.workflow_step)

    def test_advance_forbidden_for_non_call_manager(self):
        self._complete_manual_step()

        # Reviewer is added to the call but is not a call manager.
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.post(self.advance_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Unrelated user can't even see the proposal.
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.post(self.advance_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_advance_conflicts_when_step_still_active(self):
        # The manual step has not been completed yet — advance must 409.
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(self.advance_url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_advance_conflicts_for_automatic_step(self):
        # Switch to automatic mode; complete the step (auto-advances), then
        # try to advance — there is no pending manual advance.
        admin_step = CallWorkflowStep.objects.get(
            call=self.call, step="administrative_check"
        )
        admin_step.transition_mode = TransitionModes.AUTOMATIC_ON_COMPLETION
        admin_step.save()

        self._complete_manual_step()

        self.proposal.refresh_from_db()
        # Auto-advance moved the proposal onto allocation_decision.
        self.assertEqual(self.proposal.workflow_step, "allocation_decision")

        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(self.advance_url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class WorkflowOutcomeValidationTest(test.APITestCase):
    """Outcome must be a known value AND in the active step's allow-list."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_valid_outcome_for_administrative_check_accepted(self):
        response = self.client.post(
            self.url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": WorkflowStepOutcomes.ELIGIBLE,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_outcome_from_other_step_rejected(self):
        # 'feasible' belongs to technical_assessment, not administrative_check.
        response = self.client.post(
            self.url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": WorkflowStepOutcomes.FEASIBLE,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("outcome", response.data)

    def test_status_string_cannot_be_used_as_outcome(self):
        # 'completed' is a status enum, not a valid outcome — ChoiceField rejects.
        response = self.client.post(
            self.url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "completed"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_outcome_rejected(self):
        response = self.client.post(
            self.url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "bogus"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_system_reserved_rejected_outcome_blocked_with_step_context(self):
        # 'rejected' is a known choice but system-reserved — must be blocked
        # with the system-reserved error, not a step-allow-list error.
        response = self.client.post(
            self.url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": WorkflowStepOutcomes.REJECTED,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("system-reserved", str(response.data))


class CallCreationSeedsWorkflowStepsTest(test.APITestCase):
    def test_call_creation_seeds_evaluation_workflow_steps(self):
        call = factories.CallFactory()
        seeded = set(
            CallWorkflowStep.objects.filter(call=call, is_enabled=True).values_list(
                "step", flat=True
            )
        )
        expected = {s.id for s in WORKFLOW_STEPS if s.id != "award_response"}
        self.assertEqual(seeded, expected)

    def test_award_response_is_not_seeded(self):
        # award_response is provisioned via allocation_decision's
        # include_award_response toggle, not by the signal.
        call = factories.CallFactory()
        self.assertFalse(
            CallWorkflowStep.objects.filter(call=call, step="award_response").exists()
        )

    def test_seeding_is_idempotent_on_resave(self):
        call = factories.CallFactory()
        call.name = "Updated"
        call.save()
        expected_count = len([s for s in WORKFLOW_STEPS if s.id != "award_response"])
        self.assertEqual(
            CallWorkflowStep.objects.filter(call=call).count(),
            expected_count,
        )


class CallActivateWorkflowGuardTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.draft_call = self.fixture.new_call
        factories.RoundFactory(call=self.draft_call)

    def _activate(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CallFactory.get_protected_url(self.draft_call, "activate")
        return self.client.post(url)

    def test_activate_without_workflow_steps_returns_400(self):
        CallWorkflowStep.objects.filter(call=self.draft_call).delete()
        response = self._activate()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.draft_call.refresh_from_db()
        self.assertEqual(self.draft_call.state, CallStates.DRAFT)

    def test_activate_with_allocation_decision_disabled_returns_400(self):
        CallWorkflowStep.objects.filter(
            call=self.draft_call, step="allocation_decision"
        ).update(is_enabled=False)
        # Add another enabled step so the "no enabled steps" branch doesn't fire.
        factories.CallWorkflowStepFactory(
            call=self.draft_call, step="administrative_check"
        )
        response = self._activate()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Allocation decision", str(response.data))

    def test_activate_with_mandatory_step_enabled_succeeds(self):
        response = self._activate()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.draft_call.refresh_from_db()
        self.assertEqual(self.draft_call.state, CallStates.ACTIVE)


class IncludeAwardResponseSyncTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.new_call
        # allocation_decision is auto-seeded by the post_save signal
        self.allocation_step = CallWorkflowStep.objects.get(
            call=self.call, step="allocation_decision"
        )

    def _patch(self, payload):
        user = self.fixture.call_organizer_user
        self.client.force_authenticate(user)
        url = factories.CallWorkflowStepFactory.get_url(self.call, self.allocation_step)
        return self.client.patch(url, payload)

    def test_toggle_on_creates_award_response_step(self):
        response = self._patch({"include_award_response": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            CallWorkflowStep.objects.filter(
                call=self.call, step="award_response", is_enabled=True
            ).exists()
        )

    def test_toggle_off_disables_award_response_step(self):
        self.allocation_step.include_award_response = True
        self.allocation_step.save()
        CallWorkflowStep.objects.update_or_create(
            call=self.call,
            step="award_response",
            defaults={"is_enabled": True},
        )

        response = self._patch({"include_award_response": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        award = CallWorkflowStep.objects.get(call=self.call, step="award_response")
        self.assertFalse(award.is_enabled)

    def test_direct_post_of_award_response_step_rejected(self):
        user = self.fixture.call_organizer_user
        self.client.force_authenticate(user)
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        response = self.client.post(url, {"step": "award_response", "is_enabled": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            CallWorkflowStep.objects.filter(
                call=self.call, step="award_response"
            ).exists()
        )


class WorkflowStatesFrontendFieldsTest(test.APITestCase):
    """Cover the per-call config / catalog fields exposed by workflow_states.

    Frontend MR waldur-homeport!6781 renders the proposal workflow stepper
    from this endpoint; these tests pin the contract for ``applicant_visible``,
    ``duration_in_days``, ``is_required``, ``rejection_reason``, and the
    visibility rules for ``internal_notes``.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        factories.CallWorkflowStepFactory(
            call=self.call,
            step="administrative_check",
            duration_in_days=14,
            applicant_visible=True,
        )
        # ``allocation_decision`` deliberately has no CallWorkflowStep here so
        # the serializer's "no per-call config" fallback path is exercised.

        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.pending_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )

    def _get_states(self, user):
        self.client.force_authenticate(user)
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {s["step"]: s for s in response.data}

    def test_applicant_visible_and_duration_pulled_from_call_step(self):
        states = self._get_states(self.fixture.proposal_creator)
        self.assertTrue(states["administrative_check"]["applicant_visible"])
        self.assertEqual(states["administrative_check"]["duration_in_days"], 14)

    def test_applicant_visible_and_duration_default_when_no_call_step(self):
        # Conservative defaults: hidden + no deadline so the applicant doesn't
        # see steps that were never explicitly configured for them.
        states = self._get_states(self.fixture.proposal_creator)
        self.assertFalse(states["allocation_decision"]["applicant_visible"])
        self.assertIsNone(states["allocation_decision"]["duration_in_days"])

    def test_is_required_sourced_from_catalog(self):
        # ``allocation_decision`` is the only catalog-mandatory step today.
        states = self._get_states(self.fixture.proposal_creator)
        self.assertFalse(states["administrative_check"]["is_required"])
        self.assertTrue(states["allocation_decision"]["is_required"])

    def test_rejection_reason_only_populated_on_rejected_outcome(self):
        self.active_instance.status = WorkflowStepInstanceStatuses.COMPLETED
        self.active_instance.outcome = WorkflowStepOutcomes.REJECTED
        self.active_instance.outcome_reason = "Institution not eligible"
        self.active_instance.save()

        states = self._get_states(self.fixture.staff)
        self.assertEqual(
            states["administrative_check"]["rejection_reason"],
            "Institution not eligible",
        )
        # Non-rejected steps must report a null rejection_reason so the
        # frontend never accidentally displays a reason on a pending step.
        self.assertIsNone(states["allocation_decision"]["rejection_reason"])

    def test_rejection_reason_null_when_outcome_not_rejected(self):
        self.active_instance.status = WorkflowStepInstanceStatuses.COMPLETED
        self.active_instance.outcome = "eligible"
        self.active_instance.outcome_reason = "All criteria met"
        self.active_instance.save()

        states = self._get_states(self.fixture.staff)
        self.assertIsNone(states["administrative_check"]["rejection_reason"])

    def test_applicant_never_sees_internal_notes(self):
        self.active_instance.internal_notes = "Borderline; flag for VP."
        self.active_instance.save()

        states = self._get_states(self.fixture.proposal_creator)
        # The field MUST be present in the response (so the SDK can type it
        # as Optional[str] rather than "sometimes there, sometimes not"),
        # but with the value masked to null for non-team users.
        self.assertIn("internal_notes", states["administrative_check"])
        self.assertIsNone(states["administrative_check"]["internal_notes"])
        self.assertIn("internal_notes", states["allocation_decision"])
        self.assertIsNone(states["allocation_decision"]["internal_notes"])

    def test_call_manager_sees_internal_notes(self):
        self.active_instance.internal_notes = "Borderline; flag for VP."
        self.active_instance.save()

        states = self._get_states(self.fixture.call_manager)
        self.assertEqual(
            states["administrative_check"]["internal_notes"],
            "Borderline; flag for VP.",
        )

    def test_staff_sees_internal_notes(self):
        self.active_instance.internal_notes = "Borderline; flag for VP."
        self.active_instance.save()

        states = self._get_states(self.fixture.staff)
        self.assertIn("internal_notes", states["administrative_check"])


class WorkflowInternalNotesPersistenceTest(test.APITestCase):
    """Internal notes round-trip through complete/reject actions and accumulate.

    The service layer appends notes rather than overwriting so a call manager
    who records a holding note before deciding doesn't lose that context when
    the step is completed.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )

    def test_complete_step_persists_internal_notes(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        response = self.client.post(
            url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": "eligible",
                "outcome_reason": "Looks good",
                "internal_notes": "Director also approved verbally.",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.active_instance.refresh_from_db()
        self.assertEqual(
            self.active_instance.internal_notes,
            "Director also approved verbally.",
        )

    def test_reject_step_persists_internal_notes(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="reject_workflow_step"
        )
        response = self.client.post(
            url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "reason": "Not eligible — institution outside member states",
                "internal_notes": "Confirmed with legal on 2026-05-25.",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.active_instance.refresh_from_db()
        self.assertEqual(
            self.active_instance.internal_notes,
            "Confirmed with legal on 2026-05-25.",
        )

    def test_internal_notes_are_appended_not_overwritten(self):
        self.active_instance.internal_notes = "First pass: needs follow-up."
        self.active_instance.save()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        response = self.client.post(
            url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": "eligible",
                "internal_notes": "Follow-up complete; approving.",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.active_instance.refresh_from_db()
        self.assertIn("First pass", self.active_instance.internal_notes)
        self.assertIn("Follow-up complete", self.active_instance.internal_notes)

    def test_internal_notes_optional_on_complete(self):
        # Omitting internal_notes must not raise — call managers shouldn't be
        # forced to add a note on every transition.
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        response = self.client.post(
            url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": "eligible",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.active_instance.refresh_from_db()
        self.assertEqual(self.active_instance.internal_notes, "")


class WorkflowInternalNotesWriteGuardTest(test.APITestCase):
    """The internal_notes field must be symmetric: anyone who can't read it
    must not be able to write it. ``can_act_on_active_workflow_step``
    deliberately lets applicants act on applicant-owned steps (e.g.
    award_response), so without a write-side guard the applicant could
    silently inject text into a field the call-management team treats as
    internal.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        # ``award_response`` is the only catalog step whose default
        # responsible_role is the applicant, so it's the natural fixture for
        # testing applicant-as-actor scenarios.
        self.proposal.workflow_step = "award_response"
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="award_response")
        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="award_response",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )

    def test_applicant_complete_drops_internal_notes(self):
        self.client.force_authenticate(self.fixture.proposal_creator)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        response = self.client.post(
            url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": "accepted",
                "outcome_reason": "I accept the offer.",
                "internal_notes": "Trying to leak internal stuff.",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.active_instance.refresh_from_db()
        # outcome_reason (applicant-visible) is persisted, internal_notes is not.
        self.assertEqual(self.active_instance.outcome_reason, "I accept the offer.")
        self.assertEqual(self.active_instance.internal_notes, "")

    def test_staff_complete_on_applicant_step_still_persists_internal_notes(self):
        # Sanity check that the write-side gate doesn't over-block: staff
        # (who can read notes) completing the same applicant-owned step
        # persists internal_notes normally. Without this, a refactor that
        # over-eagerly drops the field would silently break the team flow
        # without any failing test on the symmetric read side.
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        response = self.client.post(
            url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": "accepted",
                "internal_notes": "Applicant confirmed acceptance verbally too.",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.active_instance.refresh_from_db()
        self.assertEqual(
            self.active_instance.internal_notes,
            "Applicant confirmed acceptance verbally too.",
        )

    def test_applicant_reject_drops_internal_notes(self):
        # award_response allows the applicant to decline; reject endpoint also
        # passes through the same write-side guard.
        self.client.force_authenticate(self.fixture.proposal_creator)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="reject_workflow_step"
        )
        response = self.client.post(
            url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "reason": "I decline.",
                "internal_notes": "Trying to leak via reject.",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.active_instance.refresh_from_db()
        self.assertEqual(self.active_instance.outcome_reason, "I decline.")
        self.assertEqual(self.active_instance.internal_notes, "")


class WorkflowStatesAccessTest(test.APITestCase):
    """Pin the access policy of the workflow_states GET endpoint.

    The viewset's queryset filter (``filter_queryset_for_user``) is the only
    gate. These tests document that contract — adding a new caller class
    (e.g. a 'reporting analyst' role) means thinking about whether they
    should be in the filter, and the tests will catch a regression that
    silently widens or narrows access.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )

    def _url(self):
        return factories.ProposalFactory.get_url(
            self.proposal, action="workflow_states"
        )

    def test_applicant_can_view_their_own_proposal(self):
        # Positive baseline: the applicant must be able to render the timeline.
        self.client.force_authenticate(self.fixture.proposal_creator)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unrelated_user_is_denied(self):
        # Authenticated but not the applicant, not on the call, not a member
        # of the project. The queryset filter must hide the proposal entirely.
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class InternalNotesDualRolePolicyTest(test.APITestCase):
    """Document the deliberate segregation-of-duties rule.

    A user who is both the applicant of a proposal AND a call manager on
    the same call must not see internal_notes on that proposal — the
    applicant identity wins. See ``user_can_view_internal_notes`` for the
    full rationale.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        self.active_instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
            internal_notes="Confidential team observation.",
        )

    def test_dual_role_applicant_still_denied(self):
        # Promote the applicant to also be a call manager on the same call.
        # The view-side gate must still hide internal_notes from them.
        self.fixture.call.add_user(self.fixture.proposal_creator, CallRole.MANAGER)

        self.client.force_authenticate(self.fixture.proposal_creator)
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        states = {s["step"]: s for s in response.data}
        # Field present but masked: this is the same contract the SDK relies
        # on, the dual-role-applicant case just exercises the same branch.
        self.assertIn("internal_notes", states["administrative_check"])
        self.assertIsNone(states["administrative_check"]["internal_notes"])


class RejectionReasonBlankStringTest(test.APITestCase):
    """When a step is rejected with an empty reason, the API must still
    signal "this step was rejected" rather than collapse to null. Frontends
    branch on ``rejection_reason === null`` to decide whether to render the
    rejection state, so an empty string and null carry different meaning.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()

        self.instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.COMPLETED,
            outcome=WorkflowStepOutcomes.REJECTED,
            outcome_reason="",  # legacy / admin-edited row
            started_at=timezone.now(),
        )

    def test_rejected_with_blank_reason_returns_empty_string_not_null(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        state = next(s for s in response.data if s["step"] == "administrative_check")
        # The signal is distinguishability: "" means "rejected, no reason
        # captured", None means "not rejected". Both are valid; conflating
        # them via ``return obj.outcome_reason or None`` was the bug.
        self.assertEqual(state["rejection_reason"], "")
        self.assertIsNotNone(state["rejection_reason"])
