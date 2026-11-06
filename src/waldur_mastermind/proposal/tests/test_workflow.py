from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.fixtures import CallRole, OfferingRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import workflow_service
from waldur_mastermind.proposal.enums import (
    DEFAULT_CALL_STEPS,
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    ReviewerPoolInvitationStatuses,
    TransitionModes,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.models import (
    CallWorkflowStep,
    Proposal,
    ProposalWorkflowStepInstance,
    Review,
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
        # Call creation seeds the default workflow steps. Clear them so these
        # tests can exercise the create endpoint from a clean slate.
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
                call=self.call, key="administrative_check"
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
        # A proposal needs a team to be submitted (production auto-adds the
        # creator; the factory does not).
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)

        # Configure workflow steps. The call creation signal seeds the default
        # steps; the factory uses update_or_create so these calls tune durations
        # on the seeded rows. expert_review is explicitly disabled to exercise
        # the SKIPPED submit-time branch (it is disabled by default anyway).
        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", duration_in_days=5
        )
        factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", duration_in_days=10
        )
        factories.CallWorkflowStepFactory(
            call=self.call, step="expert_review", is_enabled=False
        )
        # Every call that existed before steps had a kind has an award
        # response step, disabled unless the call asked for one.
        factories.ensure_default_step(self.call, "award_response")

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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        factories.ProposalWorkflowStepInstanceFactory(
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
        factories.point_at_step(self.proposal, "allocation_decision")
        # A freshly-submitted proposal has no project yet; the terminal step is
        # what provisions it. The fixture pre-assigns one, so clear it to
        # exercise the allocation path.
        self.proposal.project = None
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
        # Reaching the terminal step must also provision (converges with the
        # legacy approve action) and record who accepted it.
        self.assertIsNotNone(self.proposal.project)
        self.assertIsNotNone(self.proposal.approved_by)

    def test_terminal_step_records_approved_by_when_project_exists(self):
        # Regression: approved_by used to be written only inside
        # allocate_proposal, which the ``project_id is None`` guard skips when a
        # project already exists — leaving acceptance with no recorded approver.
        factories.point_at_step(self.proposal, "allocation_decision")
        # Keep the fixture's pre-assigned project so the allocation branch (and
        # its approved_by write) is skipped.
        self.assertIsNotNone(self.proposal.project)
        self.proposal.approved_by = None
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

        manager = structure_factories.UserFactory()
        self.call.add_user(manager, CallRole.MANAGER)
        self.client.force_authenticate(manager)
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
        self.assertEqual(self.proposal.approved_by, manager)

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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", duration_in_days=12
        )
        factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        factories.ProposalWorkflowStepInstanceFactory(
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
            call=self.call, key="administrative_check"
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
        CallWorkflowStep.objects.filter(pk=step.pk).update(completer=None)
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
        step = CallWorkflowStep.objects.get(call=self.call, key="expert_review")
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


class WorkflowStepCriteriaWeightTest(test.APITestCase):
    """A step's criteria carry weights, and stand as reviewers scored them."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.step = CallWorkflowStep.review_step_for(self.call)
        self.assertIsNotNone(self.step)
        self.merit = WorkflowCriterion.objects.create(
            workflow_step=self.step, name="Merit", order=0, weight=2
        )
        self.impact = WorkflowCriterion.objects.create(
            workflow_step=self.step, name="Impact", order=1
        )
        self.url = factories.CallWorkflowStepFactory.get_url(self.call, self.step)
        self.client.force_authenticate(self.fixture.call_organizer_user)

    def _patch(self, criteria):
        return self.client.patch(self.url, {"criteria": criteria}, format="json")

    def _score_a_review(self, **fields):
        review = self.fixture.review
        review.criteria_scores = {self.merit.uuid.hex: 4}
        for name, value in fields.items():
            setattr(review, name, value)
        review.save()

    def test_weights_round_trip(self):
        response = self._patch(
            [
                {"name": "Merit", "order": 0, "weight": 3},
                {"name": "Impact", "order": 1, "weight": 1},
            ]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [(c["name"], c["weight"]) for c in response.data["criteria"]],
            [("Merit", 3), ("Impact", 1)],
        )
        self.merit.refresh_from_db()
        self.assertEqual(self.merit.weight, 3)

    def test_a_criterion_sent_without_weight_keeps_its_weight(self):
        response = self._patch(
            [{"name": "Merit", "order": 0}, {"name": "Impact", "order": 1}]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.merit.refresh_from_db()
        self.assertEqual(self.merit.weight, 2)

    def test_weight_out_of_range_is_refused(self):
        response = self._patch([{"name": "Merit", "order": 0, "weight": 0}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_criteria_are_unlocked_before_scoring(self):
        response = self.client.get(self.url)
        self.assertFalse(response.data["criteria_locked"])

    def test_scored_criteria_are_locked(self):
        self._score_a_review()
        response = self.client.get(self.url)
        self.assertTrue(response.data["criteria_locked"])
        for criteria in (
            # renamed
            [
                {"name": "Excellence", "order": 0, "weight": 2},
                {"name": "Impact", "order": 1, "weight": 1},
            ],
            # dropped
            [{"name": "Merit", "order": 0, "weight": 2}],
            # reweighted
            [
                {"name": "Merit", "order": 0, "weight": 1},
                {"name": "Impact", "order": 1, "weight": 1},
            ],
        ):
            response = self._patch(criteria)
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, criteria
            )
            self.assertIn("criteria", response.data)
        self.assertEqual(
            sorted(self.step.criteria.values_list("name", "weight")),
            [("Impact", 1), ("Merit", 2)],
        )

    def test_scored_criteria_may_be_reordered(self):
        self._score_a_review()
        response = self._patch(
            [
                {"name": "Impact", "order": 0, "weight": 1},
                {"name": "Merit", "order": 1, "weight": 2},
            ]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            list(self.step.criteria.values_list("name", flat=True)),
            ["Impact", "Merit"],
        )

    def test_a_submitted_review_locks_the_criteria(self):
        self._score_a_review(
            criteria_scores={}, state=Review.States.SUBMITTED, summary_score=4
        )
        response = self._patch([{"name": "Merit", "order": 0, "weight": 2}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


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

    def test_allocation_time_rejected_on_non_allocation_step(self):
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {
            "step": "administrative_check",
            "is_enabled": True,
            "allocation_time": "fixed_date",
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allocation_time", response.data)

    def test_allocation_time_allowed_on_allocation_decision(self):
        step = factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision"
        )
        url = factories.CallWorkflowStepFactory.get_url(self.call, step)
        response = self.client.patch(
            url, {"allocation_time": "fixed_date"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        step.refresh_from_db()
        self.assertEqual(step.allocation_time, "fixed_date")

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

    def test_panel_review_no_longer_requires_expert_review(self):
        # Steps are composed freely now; the only rule across a call's steps
        # is its one allocation decision.
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {"step": "panel_review", "is_enabled": True}
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

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

    def test_listing_follows_step_order(self):
        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", order=10
        )
        factories.CallWorkflowStepFactory(
            call=self.call, step="technical_assessment", order=1
        )
        factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", order=0
        )

        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        steps = [s["step"] for s in response.data]
        # allocation_decision (0) -> technical_assessment (1)
        # -> administrative_check (10)
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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.pending_instance = factories.ProposalWorkflowStepInstanceFactory(
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
        factories.point_at_step(self.proposal, "allocation_decision")
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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

    def test_overdue_active_step_advances_to_next_enabled_step(self):
        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", duration_in_days=3
        )
        overdue = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now() - timedelta(days=2),
            deadline=timezone.now() - timedelta(hours=1),
        )
        next_pending = factories.ProposalWorkflowStepInstanceFactory(
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
        overdue = factories.ProposalWorkflowStepInstanceFactory(
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
        future = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
            deadline=timezone.now() + timedelta(days=1),
        )
        # Completed with past deadline — must stay COMPLETED.
        other_proposal = factories.ProposalFactory(round=self.fixture.round)
        completed = factories.ProposalWorkflowStepInstanceFactory(
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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        factories.CallWorkflowStepFactory(
            call=self.call,
            step="administrative_check",
            responsible_role=ResponsibleRoles.CALL_MANAGER,
        )
        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
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


class WorkflowStepPanelMemberTest(test.APITestCase):
    """panel_review is gated by the CALL.PANEL_MEMBER role: a real panel member
    may act; users holding only other call roles are denied."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        factories.point_at_step(self.proposal, "panel_review")
        self.proposal.save()

        # panel_review depends on expert_review being enabled.
        factories.CallWorkflowStepFactory(call=self.call, step="expert_review")
        factories.CallWorkflowStepFactory(
            call=self.call,
            step="panel_review",
            responsible_role=ResponsibleRoles.PANEL_MEMBER,
        )
        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="panel_review",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )

    def test_panel_member_step_allows_call_manager(self):
        # Call managers drive progression and may complete any non-applicant
        # step (gated by the review/score gates), including a panel step.
        manager = structure_factories.UserFactory()
        self.call.add_user(manager, CallRole.MANAGER)
        self.client.force_authenticate(manager)
        response = self.client.post(
            self.url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "approved"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_panel_member_step_denies_reviewer(self):
        reviewer = structure_factories.UserFactory()
        self.call.add_user(reviewer, CallRole.REVIEWER)
        # A reviewer of the proposal, so it is the step that refuses them.
        factories.ReviewFactory(proposal=self.proposal, reviewer=reviewer)
        self.client.force_authenticate(reviewer)
        response = self.client.post(
            self.url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "approved"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_panel_member_step_allows_panel_member(self):
        panel_member = structure_factories.UserFactory()
        self.call.add_user(panel_member, CallRole.PANEL_MEMBER)
        self.client.force_authenticate(panel_member)
        response = self.client.post(
            self.url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": "approved"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class WorkflowStepActiveUniqueConstraintTest(test.APITestCase):
    """Steps of one stage are active at once; one step is never there twice."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal

    def test_two_steps_of_a_proposal_may_be_active_at_once(self):
        for key in ("administrative_check", "technical_assessment"):
            factories.ProposalWorkflowStepInstanceFactory(
                proposal=self.proposal,
                step=key,
                status=WorkflowStepInstanceStatuses.ACTIVE,
                started_at=timezone.now(),
            )
        self.assertEqual(
            self.proposal.workflow_step_instances.filter(
                status=WorkflowStepInstanceStatuses.ACTIVE
            ).count(),
            2,
        )

    def test_cannot_have_one_step_twice_on_same_proposal(self):
        factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        with self.assertRaises(IntegrityError):
            with db_transaction.atomic():
                ProposalWorkflowStepInstance.objects.create(
                    proposal=self.proposal,
                    call_step=self.proposal.workflow_step_instances.get().call_step,
                    step="administrative_check",
                    order=0,
                )


class WorkflowConfigDriftTest(test.APITestCase):
    """Workflow path is fixed at submit; live call-config edits don't retro-apply."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        # Submit-time enabled set: administrative_check, allocation_decision.
        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", is_enabled=True
        )
        self.alloc_step_config = factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", is_enabled=True
        )
        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        factories.ProposalWorkflowStepInstanceFactory(
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
            factories.ProposalWorkflowStepInstanceFactory(
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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        factories.CallWorkflowStepFactory(
            call=self.call,
            step="administrative_check",
            transition_mode=TransitionModes.MANUAL,
        )
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        factories.ProposalWorkflowStepInstanceFactory(
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
        ProposalWorkflowStepInstance.objects.filter(
            proposal=self.proposal, step="allocation_decision"
        ).delete()
        CallWorkflowStep.objects.filter(
            call=self.call, key="allocation_decision"
        ).delete()
        # Clear the fixture's pre-assigned project so the terminal advance
        # exercises allocation (and records who approved it).
        self.proposal.project = None
        self.proposal.save()

        self._complete_manual_step()

        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(self.advance_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["proposal_state"], ProposalStates.ACCEPTED)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertIsNone(self.proposal.workflow_step)
        self.assertIsNotNone(self.proposal.project)
        # The manager who confirmed the advance is recorded as approver, not
        # the system robot.
        self.assertEqual(self.proposal.approved_by, self.fixture.call_manager)

    def test_advance_forbidden_for_non_call_manager(self):
        self._complete_manual_step()

        # A reviewer of the proposal is not a call manager.
        factories.ReviewFactory(
            proposal=self.proposal, reviewer=self.fixture.reviewer_1
        )
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
            call=self.call, key="administrative_check"
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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        factories.ProposalWorkflowStepInstanceFactory(
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
    def test_call_creation_enables_only_manager_drivable_steps(self):
        # Only the call-manager-owned steps are enabled by default, so the
        # default workflow is fully drivable by the call manager. The other
        # evaluation steps are seeded disabled (opt-in) until their actor-facing
        # surfaces exist.
        call = factories.CallFactory()
        enabled = set(
            CallWorkflowStep.objects.filter(call=call, is_enabled=True).values_list(
                "key", flat=True
            )
        )
        self.assertEqual(enabled, {"administrative_check", "allocation_decision"})

        disabled = set(
            CallWorkflowStep.objects.filter(call=call, is_enabled=False).values_list(
                "key", flat=True
            )
        )
        self.assertEqual(
            disabled, {"technical_assessment", "expert_review", "panel_review"}
        )

    def test_award_response_is_not_seeded(self):
        # award_response is provisioned via allocation_decision's
        # include_award_response toggle, not by the signal.
        call = factories.CallFactory()
        self.assertFalse(
            CallWorkflowStep.objects.filter(call=call, key="award_response").exists()
        )

    def test_seeding_is_idempotent_on_resave(self):
        call = factories.CallFactory()
        call.name = "Updated"
        call.save()
        expected_count = len(
            [s for s in DEFAULT_CALL_STEPS if s.key != "award_response"]
        )
        self.assertEqual(
            CallWorkflowStep.objects.filter(call=call).count(),
            expected_count,
        )


class CallActivateWorkflowGuardTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.draft_call = self.fixture.new_call
        factories.RoundFactory(call=self.draft_call)
        # A call must have at least one offering to be activated.
        factories.RequestedOfferingFactory(call=self.draft_call)

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

    def test_activate_without_offerings_returns_400(self):
        self.draft_call.requestedoffering_set.all().delete()
        response = self._activate()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering", str(response.data).lower())
        self.draft_call.refresh_from_db()
        self.assertEqual(self.draft_call.state, CallStates.DRAFT)

    def test_activate_with_only_requested_offering_returns_400(self):
        # A requested-but-not-accepted offering yields no resource templates, so
        # activation requires an ACCEPTED one (matches the accepted-only
        # `offerings` serializer field the frontend gate reads).
        self.draft_call.requestedoffering_set.update(
            state=RequestedOfferingStates.REQUESTED
        )
        response = self._activate()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering", str(response.data).lower())
        self.draft_call.refresh_from_db()
        self.assertEqual(self.draft_call.state, CallStates.DRAFT)

    def test_activate_with_allocation_decision_disabled_returns_400(self):
        CallWorkflowStep.objects.filter(
            call=self.draft_call, key="allocation_decision"
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
            call=self.call, key="allocation_decision"
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
                call=self.call, key="award_response", is_enabled=True
            ).exists()
        )

    def test_toggle_off_disables_award_response_step(self):
        self.allocation_step.include_award_response = True
        self.allocation_step.save()
        factories.CallWorkflowStepFactory(
            call=self.call, step="award_response", is_enabled=True
        )

        response = self._patch({"include_award_response": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        award = CallWorkflowStep.objects.get(call=self.call, key="award_response")
        self.assertFalse(award.is_enabled)

    def test_direct_post_of_award_response_step_rejected(self):
        user = self.fixture.call_organizer_user
        self.client.force_authenticate(user)
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        response = self.client.post(url, {"step": "award_response", "is_enabled": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            CallWorkflowStep.objects.filter(
                call=self.call, key="award_response"
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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        factories.CallWorkflowStepFactory(
            call=self.call,
            step="administrative_check",
            duration_in_days=14,
            applicant_visible=True,
        )
        # ``allocation_decision`` deliberately has no CallWorkflowStep here so
        # the serializer's "no per-call config" fallback path is exercised.

        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.pending_instance = factories.ProposalWorkflowStepInstanceFactory(
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


class WorkflowStatesHiddenStepOutcomeTest(test.APITestCase):
    """A step the call keeps from the applicant shows the applicant team no
    verdict, reasoning or actor; the call team still reads all of it."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        self.applicant = self.fixture.proposal_creator
        self.co_applicant = structure_factories.UserFactory()
        self.proposal.add_user(self.co_applicant, ProposalRole.MEMBER)
        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", applicant_visible=True
        )
        self.hidden_step = factories.CallWorkflowStepFactory(
            call=self.call,
            step="technical_assessment",
            applicant_visible=False,
            on_negative="continue",
        )
        self.visible = self._completed("administrative_check", "eligible")
        self.hidden = self._completed(
            "technical_assessment", WorkflowStepOutcomes.INFEASIBLE
        )

    def _completed(self, key, outcome):
        return factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step=key,
            status=WorkflowStepInstanceStatuses.COMPLETED,
            outcome=outcome,
            outcome_reason=f"{key} reasoning",
            completed_by=self.fixture.call_manager,
            completed_at=timezone.now(),
        )

    def _get_states(self, user):
        self.client.force_authenticate(user)
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {s["step"]: s for s in response.data}

    def test_applicant_team_reads_no_outcome_of_a_hidden_step(self):
        for user in (self.applicant, self.co_applicant):
            hidden = self._get_states(user)["technical_assessment"]
            self.assertIsNone(hidden["outcome"])
            self.assertEqual(hidden["outcome_reason"], "")
            self.assertIsNone(hidden["rejection_reason"])
            self.assertIsNone(hidden["completed_by"])
            self.assertEqual(hidden["status"], WorkflowStepInstanceStatuses.COMPLETED)

    def test_applicant_team_reads_the_outcome_of_a_visible_step(self):
        for user in (self.applicant, self.co_applicant):
            visible = self._get_states(user)["administrative_check"]
            self.assertEqual(visible["outcome"], "eligible")
            self.assertEqual(
                visible["outcome_reason"], "administrative_check reasoning"
            )

    def test_call_manager_reads_the_outcome_of_a_hidden_step(self):
        hidden = self._get_states(self.fixture.call_manager)["technical_assessment"]
        self.assertEqual(hidden["outcome"], WorkflowStepOutcomes.INFEASIBLE)
        self.assertEqual(hidden["outcome_reason"], "technical_assessment reasoning")

    def test_provider_reads_only_their_own_hidden_step(self):
        provider = self.fixture.offering_fixture.offering_manager
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.fixture.requested_offering_accepted,
            created_by=self.applicant,
        )
        self.visible.call_step.applicant_visible = False
        self.visible.call_step.save(update_fields=["applicant_visible"])
        states = self._get_states(provider)
        self.assertIsNone(states["administrative_check"]["outcome"])
        self.assertEqual(states["administrative_check"]["outcome_reason"], "")
        # The technical assessment is the requested offering's team's work.
        self.assertEqual(
            states["technical_assessment"]["outcome"], WorkflowStepOutcomes.INFEASIBLE
        )

    def test_applicant_reads_the_hidden_step_that_ended_the_proposal(self):
        self.hidden_step.on_negative = "reject"
        self.hidden_step.save(update_fields=["on_negative"])
        hidden = self._get_states(self.applicant)["technical_assessment"]
        self.assertEqual(hidden["outcome"], WorkflowStepOutcomes.INFEASIBLE)
        self.assertEqual(hidden["outcome_reason"], "technical_assessment reasoning")


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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        factories.ProposalWorkflowStepInstanceFactory(
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
        factories.point_at_step(self.proposal, "award_response")
        self.proposal.save()

        factories.CallWorkflowStepFactory(call=self.call, step="award_response")
        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
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
                "outcome_reason": "The applicant accepted by phone.",
                "internal_notes": "Applicant confirmed acceptance verbally too.",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.active_instance.refresh_from_db()
        self.assertEqual(
            self.active_instance.internal_notes,
            "Applicant confirmed acceptance verbally too.",
        )

    def test_applicant_cannot_reject_at_their_own_step(self):
        # The applicant declines the award by completing the step; rejecting
        # the proposal is the call's decision, refused before any text lands.
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
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.active_instance.refresh_from_db()
        self.assertEqual(self.active_instance.outcome_reason, "")
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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        factories.ProposalWorkflowStepInstanceFactory(
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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
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


class WorkflowStepCompletedByBlindReviewTest(test.APITestCase):
    """completed_by reveals who completed a step — including the reviewer on
    expert_review and the panel member on panel_review. It must honour the
    call's reviewer_identity_visible_to_submitters setting: masked for the
    proposal submitter when blind, visible to the call team, and revealed to
    the submitter only when the call opts in.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        factories.point_at_step(self.proposal, "expert_review")
        self.proposal.save()

        self.reviewer = structure_factories.UserFactory()
        self.call.add_user(self.reviewer, CallRole.REVIEWER)
        factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="expert_review",
            status=WorkflowStepInstanceStatuses.COMPLETED,
            started_at=timezone.now(),
            completed_at=timezone.now(),
            completed_by=self.reviewer,
            outcome=WorkflowStepOutcomes.REVIEWED,
            outcome_reason="Detailed reviewer commentary.",
        )

    def _states_for(self, user):
        self.client.force_authenticate(user)
        url = factories.ProposalFactory.get_url(self.proposal, action="workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {s["step"]: s for s in response.data}

    def test_submitter_cannot_see_completed_by_when_blind(self):
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.save()
        states = self._states_for(self.fixture.proposal_creator)
        # Field present but masked (stable response shape).
        self.assertIn("completed_by", states["expert_review"])
        self.assertIsNone(states["expert_review"]["completed_by"])

    def test_submitter_sees_completed_by_when_call_reveals_identity(self):
        self.call.reviewer_identity_visible_to_submitters = True
        self.call.save()
        states = self._states_for(self.fixture.proposal_creator)
        self.assertIsNotNone(states["expert_review"]["completed_by"])

    def test_call_manager_always_sees_completed_by(self):
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.save()
        manager = structure_factories.UserFactory()
        self.call.add_user(manager, CallRole.MANAGER)
        states = self._states_for(manager)
        self.assertIsNotNone(states["expert_review"]["completed_by"])

    def test_submitter_cannot_see_review_outcome_when_reviews_private(self):
        self.call.reviews_visible_to_submitters = False
        self.call.save()
        states = self._states_for(self.fixture.proposal_creator)
        # Verdict and commentary on the peer-review step are masked.
        self.assertIsNone(states["expert_review"]["outcome"])
        self.assertEqual(states["expert_review"]["outcome_reason"], "")

    def test_submitter_sees_review_outcome_when_reviews_visible(self):
        self.call.reviews_visible_to_submitters = True
        self.call.save()
        states = self._states_for(self.fixture.proposal_creator)
        self.assertEqual(
            states["expert_review"]["outcome"], WorkflowStepOutcomes.REVIEWED
        )
        self.assertEqual(
            states["expert_review"]["outcome_reason"], "Detailed reviewer commentary."
        )

    def test_submitter_cannot_see_rejection_reason_when_reviews_private(self):
        # Regression: rejection_reason re-exposes outcome_reason via a
        # SerializerMethodField. Masking outcome_reason alone leaked the exact
        # rejection text through rejection_reason on a REJECTED review step.
        instance = self.proposal.workflow_step_instances.get(step="expert_review")
        instance.outcome = WorkflowStepOutcomes.REJECTED
        instance.outcome_reason = "Weak methodology — reject."
        instance.save()
        self.call.reviews_visible_to_submitters = False
        self.call.save()
        states = self._states_for(self.fixture.proposal_creator)
        self.assertIsNone(states["expert_review"]["outcome"])
        self.assertEqual(states["expert_review"]["outcome_reason"], "")
        self.assertIsNone(states["expert_review"]["rejection_reason"])

    def test_submitter_sees_rejection_reason_when_reviews_visible(self):
        instance = self.proposal.workflow_step_instances.get(step="expert_review")
        instance.outcome = WorkflowStepOutcomes.REJECTED
        instance.outcome_reason = "Weak methodology — reject."
        instance.save()
        self.call.reviews_visible_to_submitters = True
        self.call.save()
        states = self._states_for(self.fixture.proposal_creator)
        self.assertEqual(
            states["expert_review"]["rejection_reason"], "Weak methodology — reject."
        )

    def test_call_manager_always_sees_rejection_reason(self):
        instance = self.proposal.workflow_step_instances.get(step="expert_review")
        instance.outcome = WorkflowStepOutcomes.REJECTED
        instance.outcome_reason = "Weak methodology — reject."
        instance.save()
        self.call.reviews_visible_to_submitters = False
        self.call.save()
        manager = structure_factories.UserFactory()
        self.call.add_user(manager, CallRole.MANAGER)
        states = self._states_for(manager)
        self.assertEqual(
            states["expert_review"]["rejection_reason"], "Weak methodology — reject."
        )


class ProposalApprovedByBlindReviewTest(test.APITestCase):
    """approved_by names the decision-maker who accepted the proposal. Hide it
    from the proposal's own submitter under blind review; show it to the call
    team and once the call reveals reviewer identity to submitters.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.approved_by = self.manager
        self.proposal.save()

    def _get(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_submitter_cannot_see_approver_when_blind(self):
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.save()
        self.assertIsNone(self._get(self.fixture.proposal_creator)["approved_by"])

    def test_submitter_sees_approver_when_identity_visible(self):
        self.call.reviewer_identity_visible_to_submitters = True
        self.call.save()
        self.assertIsNotNone(self._get(self.fixture.proposal_creator)["approved_by"])

    def test_call_manager_sees_approver(self):
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.save()
        self.assertIsNotNone(self._get(self.manager)["approved_by"])


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
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        self.instance = factories.ProposalWorkflowStepInstanceFactory(
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


class WorkflowStepGateEnforcementTest(test.APITestCase):
    """CallWorkflowStep.min_reviewers / min_score_threshold are the enforced
    source of truth: a step whose review gate is unmet cannot be completed
    (replacing the removed Round-level review/decision fields)."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        factories.point_at_step(self.proposal, "expert_review")
        self.proposal.save()

        self.expert_step = factories.CallWorkflowStepFactory(
            call=self.call, step="expert_review", min_reviewers=2
        )
        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="expert_review",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )

    def _submit_reviews(self, scores):
        for score in scores:
            factories.ReviewFactory(
                proposal=self.proposal,
                reviewer=structure_factories.UserFactory(),
                state=Review.States.SUBMITTED,
                summary_score=score,
            )

    def _complete(self, outcome="reviewed"):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            self.url,
            {"step_uuid": self.active_instance.uuid.hex, "outcome": outcome},
        )

    def test_cannot_complete_below_min_reviewers(self):
        self._submit_reviews([8])  # only one, gate requires two
        response = self._complete()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.active_instance.refresh_from_db()
        self.assertEqual(
            self.active_instance.status, WorkflowStepInstanceStatuses.ACTIVE
        )

    def test_can_complete_when_min_reviewers_met(self):
        self._submit_reviews([8, 9])
        response = self._complete()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.active_instance.refresh_from_db()
        self.assertEqual(
            self.active_instance.status, WorkflowStepInstanceStatuses.COMPLETED
        )

    def test_cannot_complete_below_score_threshold(self):
        self.expert_step.min_reviewers = None
        self.expert_step.min_score_threshold = Decimal("7.0")
        self.expert_step.save()
        self._submit_reviews([5, 6])  # average 5.5 < 7.0
        self.assertEqual(self._complete().status_code, status.HTTP_400_BAD_REQUEST)

    def test_can_complete_when_score_threshold_met(self):
        self.expert_step.min_reviewers = None
        self.expert_step.min_score_threshold = Decimal("7.0")
        self.expert_step.save()
        self._submit_reviews([8, 9])  # average 8.5 >= 7.0
        self.assertEqual(self._complete().status_code, status.HTTP_200_OK)

    def test_ungated_step_completes_without_reviews(self):
        self.expert_step.min_reviewers = None
        self.expert_step.min_score_threshold = None
        self.expert_step.save()
        self.assertEqual(self._complete().status_code, status.HTTP_200_OK)

    def test_declined_outcome_bypasses_gate(self):
        # Declining a proposal must never be blocked by a review gate, even on a
        # decision step configured with one and with zero reviews submitted.
        decision_step = factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", min_reviewers=5
        )
        # Exercise the helper directly to avoid the terminal-allocation path.
        workflow_service._enforce_step_gates(
            self.proposal,
            self.active_instance,
            WorkflowStepOutcomes.DECLINED,
            decision_step,
        )  # must not raise


class WorkflowStepNegativeOutcomeTest(test.APITestCase):
    """A negative step outcome terminates the workflow instead of advancing or
    allocating: an applicant declining the award cancels the proposal; any other
    negative decision (declined / ineligible / infeasible) rejects it. No project
    is provisioned, and the step instance keeps its real outcome."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.project = None
        self.proposal.save()

    def _complete(self, step, outcome):
        factories.CallWorkflowStepFactory(call=self.call, step=step, is_enabled=True)
        factories.point_at_step(self.proposal, step)
        self.proposal.save()
        instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step=step,
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        response = self.client.post(
            url,
            {
                "step_uuid": instance.uuid.hex,
                "outcome": outcome,
                # Staff answering the applicant's award response say why.
                "outcome_reason": "Recorded as the applicant asked by mail.",
            },
        )
        return response, instance

    def _assert_terminal(self, response, expected_state):
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, expected_state)
        self.assertIsNone(self.proposal.project)
        self.assertIsNone(self.proposal.workflow_step)

    def test_declined_allocation_decision_rejects(self):
        response, _ = self._complete("allocation_decision", "declined")
        self._assert_terminal(response, ProposalStates.REJECTED)

    def test_declined_panel_review_rejects(self):
        response, _ = self._complete("panel_review", "declined")
        self._assert_terminal(response, ProposalStates.REJECTED)

    def test_ineligible_administrative_check_rejects(self):
        response, _ = self._complete("administrative_check", "ineligible")
        self._assert_terminal(response, ProposalStates.REJECTED)

    def test_infeasible_technical_assessment_rejects(self):
        response, _ = self._complete("technical_assessment", "infeasible")
        self._assert_terminal(response, ProposalStates.REJECTED)

    def test_declined_award_response_cancels(self):
        response, _ = self._complete("award_response", "declined")
        self._assert_terminal(response, ProposalStates.CANCELED)

    def test_instance_keeps_true_outcome_not_rejected(self):
        _, instance = self._complete("allocation_decision", "declined")
        instance.refresh_from_db()
        self.assertEqual(instance.outcome, WorkflowStepOutcomes.DECLINED)


class ProposalTeamVisibilityTest(test.APITestCase):
    """A call's reviewers/panel members may view a proposal's team (read-only)
    so the review interface renders instead of 403-crashing its team section."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        # Submitted: the call's reviewers do not read drafts
        self.fixture.proposal.state = ProposalStates.SUBMITTED
        self.fixture.proposal.save()
        self.url = factories.ProposalFactory.get_url(
            self.fixture.proposal, "list_users"
        )

    def test_call_reviewer_can_view_proposal_team(self):
        # The call's reviewer role is its pool; the review grants the proposal.
        factories.ReviewFactory(
            proposal=self.fixture.proposal, reviewer=self.fixture.reviewer_1
        )
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_call_manager_can_view_proposal_team(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_unrelated_user_cannot_view_proposal_team(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(self.url)
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )


class CallManagerDrivesStepsTest(test.APITestCase):
    """The call manager drives progression: they may complete/reject any
    non-applicant step (gated by the review/score gates), while award_response
    stays the applicant's to complete."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()

    def _active(self, step, responsible_role):
        factories.CallWorkflowStepFactory(
            call=self.call, step=step, responsible_role=responsible_role
        )
        factories.point_at_step(self.proposal, step)
        self.proposal.save(update_fields=["current_step"])
        return factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step=step,
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )

    def _complete(self, user, instance, outcome):
        self.client.force_authenticate(user)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )
        return self.client.post(
            url, {"step_uuid": instance.uuid.hex, "outcome": outcome}
        )

    def test_call_manager_can_complete_a_reviewer_step(self):
        instance = self._active("expert_review", ResponsibleRoles.REVIEWER)
        response = self._complete(self.fixture.call_manager, instance, "reviewed")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_call_manager_can_complete_a_panel_step(self):
        instance = self._active("panel_review", ResponsibleRoles.PANEL_MEMBER)
        response = self._complete(self.fixture.call_manager, instance, "approved")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_call_manager_cannot_complete_award_response(self):
        instance = self._active("award_response", ResponsibleRoles.APPLICANT)
        response = self._complete(self.fixture.call_manager, instance, "accepted")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_applicant_can_complete_award_response(self):
        instance = self._active("award_response", ResponsibleRoles.APPLICANT)
        response = self._complete(self.fixture.proposal_creator, instance, "declined")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class CompleteWorkflowStepChecklistGateTest(test.APITestCase):
    """A step with an attached required checklist cannot be completed until its
    required questions are answered (WAL-9484)."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        factories.point_at_step(self.proposal, "administrative_check")
        self.proposal.save()

        self.checklist = checklist_factories.ChecklistFactory()
        self.question = checklist_factories.QuestionFactory(
            checklist=self.checklist, required=True
        )
        self.call_step = factories.CallWorkflowStepFactory(
            call=self.call,
            step="administrative_check",
            checklist=self.checklist,
            checklist_required=True,
        )
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")

        self.active_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="complete_workflow_step"
        )

    def _complete(self, outcome="eligible"):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            self.url,
            {
                "step_uuid": self.active_instance.uuid.hex,
                "outcome": outcome,
                "outcome_reason": "reason",
            },
        )

    def test_complete_blocked_when_required_checklist_unanswered(self):
        response = self._complete()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "administrative_check")

    def test_complete_allowed_after_required_question_answered(self):
        completion = factories.contribution_completion(
            self.proposal, "administrative_check", self.fixture.staff
        )
        checklist_factories.AnswerFactory(
            completion=completion,
            question=self.question,
            user=self.fixture.staff,
        )
        response = self._complete()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "allocation_decision")

    def test_advisory_checklist_does_not_block_completion(self):
        self.call_step.checklist_required = False
        self.call_step.save()
        response = self._complete()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_optional_questions_only_does_not_block(self):
        self.question.required = False
        self.question.save()
        response = self._complete()
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class WorkflowStepResponsibleUsersTest(test.APITestCase):
    """``responsible_users`` tells the call manager who evaluates each step."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.client.force_authenticate(self.fixture.call_manager)

    def _steps(self):
        response = self.client.get(
            factories.CallWorkflowStepFactory.get_list_url(self.call)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {s["step"]: s for s in response.data}

    def _emails(self, step):
        return sorted(u["email"] for u in step["responsible_users"])

    def test_call_manager_step_lists_call_managers(self):
        steps = self._steps()
        manager = self.fixture.call_manager
        self.assertEqual(self._emails(steps["administrative_check"]), [manager.email])
        (entry,) = steps["administrative_check"]["responsible_users"]
        self.assertEqual(
            entry,
            {
                "uuid": manager.uuid.hex,
                "username": manager.username,
                "full_name": manager.full_name,
                "email": manager.email,
                "is_panel_chair": False,
                "is_panel_vice_chair": False,
                "offerings": [],
            },
        )

    def test_reviewer_step_lists_pool_reviewers(self):
        expected = sorted(
            [self.fixture.reviewer_1.email, self.fixture.reviewer_2.email]
        )
        self.assertEqual(self._emails(self._steps()["expert_review"]), expected)

    def test_panel_step_marks_chair(self):
        chair = self.fixture.panel_member
        other = structure_factories.UserFactory()
        self.call.add_user(other, CallRole.PANEL_MEMBER)
        self.call.panel_chair = chair
        self.call.save()
        users = {
            u["email"]: u["is_panel_chair"]
            for u in self._steps()["panel_review"]["responsible_users"]
        }
        self.assertEqual(users, {chair.email: True, other.email: False})

    def test_technical_assessment_lists_managers_of_accepted_offerings_only(self):
        from waldur_core.permissions.fixtures import OfferingRole

        accepted_manager = structure_factories.UserFactory()
        self.fixture.requested_offering_accepted.offering.add_user(
            accepted_manager, OfferingRole.MANAGER
        )
        pending_manager = structure_factories.UserFactory()
        pending_offering = self.fixture.offering_fixture.__class__().offering
        factories.RequestedOfferingFactory(
            call=self.call,
            offering=pending_offering,
            state=RequestedOfferingStates.REQUESTED,
            created_by=self.fixture.owner,
        )
        pending_offering.add_user(pending_manager, OfferingRole.MANAGER)

        self.assertEqual(
            self._emails(self._steps()["technical_assessment"]),
            [accepted_manager.email],
        )

    def test_applicant_step_lists_nobody(self):
        factories.CallWorkflowStepFactory(
            call=self.call, step="award_response", is_enabled=True
        )
        self.assertEqual(self._steps()["award_response"]["responsible_users"], [])

    def test_offering_manager_names_the_offerings_they_manage(self):
        manager = structure_factories.UserFactory()
        offering = self.fixture.requested_offering_accepted.offering
        offering.add_user(manager, OfferingRole.MANAGER)

        step = self._steps()["technical_assessment"]
        (entry,) = step["responsible_users"]
        self.assertEqual(
            entry["offerings"],
            [
                {
                    "uuid": offering.uuid.hex,
                    "name": offering.name,
                    "customer_uuid": offering.customer.uuid.hex,
                }
            ],
        )
        # The technical assessment takes its contributions from the same
        # offering teams.
        (contributor,) = step["contributing_users"]
        self.assertEqual(contributor["email"], manager.email)
        self.assertEqual(contributor["offerings"], entry["offerings"])

    def _contributors(self, contributor_source, step="administrative_check"):
        models_step = self.call.workflow_steps.get(key=step)
        models_step.contributor_source = contributor_source
        models_step.save()
        return sorted(u["email"] for u in self._steps()[step]["contributing_users"])

    def test_without_contributors_nobody_contributes(self):
        self.assertEqual(self._contributors("none"), [])

    def test_assigned_reviewers_are_the_accepted_pool(self):
        accepted = factories.CallReviewerPoolFactory(call=self.call)
        factories.CallReviewerPoolFactory(
            call=self.call,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.assertEqual(
            self._contributors("assigned_reviewers"),
            [accepted.reviewer.user.email],
        )

    def test_call_role_contributors_are_its_holders(self):
        member = self.fixture.panel_member
        self.assertEqual(self._contributors("call_role:panel_member"), [member.email])

    def test_panel_chair_contributors_are_the_chair(self):
        chair = self.fixture.panel_member
        self.call.add_user(structure_factories.UserFactory(), CallRole.PANEL_MEMBER)
        self.call.panel_chair = chair
        self.call.save()
        self.assertEqual(self._contributors("call_role:panel_chair"), [chair.email])

    def test_contributors_are_withheld_from_evaluators_outside_the_team(self):
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(
            factories.CallWorkflowStepFactory.get_list_url(self.call)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for step in response.data:
            self.assertEqual(step["contributing_users"], [])
