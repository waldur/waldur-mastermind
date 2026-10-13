import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    ProposalStates,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, fixtures


class CallStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = factories.CallFactory.get_protected_url(self.call, action="stats")
        self.call.workflow_steps.all().delete()
        for step, enabled in (
            ("administrative_check", True),
            ("technical_assessment", False),
            ("expert_review", True),
            ("allocation_decision", True),
        ):
            models.CallWorkflowStep.objects.create(
                call=self.call,
                step=step,
                is_enabled=enabled,
                min_reviewers=2 if step == "expert_review" else None,
            )

    def add_proposal(self, round_obj, state, workflow_step=None):
        return factories.ProposalFactory(
            round=round_obj, state=state, workflow_step=workflow_step
        )

    def add_admin_check(self, proposal, status_, outcome=None):
        models.ProposalWorkflowStepInstance.objects.create(
            proposal=proposal,
            step="administrative_check",
            status=status_,
            outcome=outcome,
        )

    def get_stats(self, user=None):
        self.client.force_authenticate(user or self.fixture.staff)
        return self.client.get(self.url)

    def test_counts_per_round_and_for_call(self):
        round_a = self.fixture.round
        round_b = self.fixture.new_round
        # The fixture already holds a draft and a submitted proposal in round A.
        approved = self.add_proposal(round_a, ProposalStates.IN_REVIEW, "expert_review")
        self.add_admin_check(
            approved,
            WorkflowStepInstanceStatuses.COMPLETED,
            WorkflowStepOutcomes.ELIGIBLE,
        )
        factories.ReviewFactory(proposal=approved, state=models.Review.States.SUBMITTED)
        waiting = self.add_proposal(
            round_b, ProposalStates.SUBMITTED, "administrative_check"
        )
        self.add_admin_check(waiting, WorkflowStepInstanceStatuses.ACTIVE)
        self.add_proposal(round_b, ProposalStates.ACCEPTED, "allocation_decision")
        # On a step disabled for the call: counted by state, not by step.
        self.add_proposal(round_b, ProposalStates.IN_REVIEW, "technical_assessment")

        response = self.get_stats()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        rounds = {r["uuid"]: r for r in data["rounds"]}
        block_a = rounds[str(round_a.uuid)]
        block_b = rounds[str(round_b.uuid)]

        self.assertEqual(block_a["proposals"]["total"], 3)
        self.assertEqual(block_a["proposals"]["draft"], 1)
        self.assertEqual(block_a["proposals"]["in_review"], 1)
        self.assertEqual(block_a["admin_approved"], 1)
        self.assertEqual(block_a["missing_admin_checks"], 0)
        self.assertEqual(block_a["proposals_missing_reviews"], 1)
        self.assertEqual(block_b["proposals"]["accepted"], 1)
        self.assertEqual(block_b["missing_admin_checks"], 1)

        self.assertEqual(data["call"]["proposals"]["total"], 6)
        self.assertEqual(data["call"]["reviews"]["total"], 2)
        self.assertEqual(data["call"]["reviews"]["submitted"], 1)

        for key in ("total", *[s for s, _ in ProposalStates.CHOICES]):
            self.assertEqual(
                data["call"]["proposals"][key],
                block_a["proposals"][key] + block_b["proposals"][key],
            )

        steps = [(s["step"], s["count"]) for s in data["call"]["steps"]]
        self.assertEqual(
            steps,
            [
                ("administrative_check", 1),
                ("expert_review", 1),
                ("allocation_decision", 1),
            ],
        )

    def test_overdue_reviews(self):
        round_obj = self.fixture.round
        round_obj.review_duration_in_days = 3
        round_obj.save()
        review = self.fixture.review
        models.Review.objects.filter(pk=review.pk).update(
            created=timezone.now() - datetime.timedelta(days=5)
        )

        response = self.get_stats()
        self.assertEqual(response.data["call"]["reviews"]["overdue"], 1)
        self.assertEqual(response.data["rounds"][0]["reviews"]["overdue"], 1)

    def test_call_without_proposals(self):
        self.url = factories.CallFactory.get_protected_url(
            self.fixture.new_call, action="stats"
        )
        response = self.get_stats()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["rounds"], [])
        self.assertEqual(response.data["call"]["proposals"]["total"], 0)

    def test_call_manager_and_organizer_can_read(self):
        for user in (self.fixture.call_manager, self.fixture.call_organizer_user):
            self.assertEqual(self.get_stats(user).status_code, status.HTTP_200_OK)

    def test_other_roles_are_refused(self):
        other_call = factories.CallFactory()
        other_manager = structure_factories.UserFactory()
        other_call.add_user(other_manager, CallRole.MANAGER)
        for user in (
            self.fixture.reviewer_1,
            self.fixture.panel_member,
            self.fixture.proposal_creator,
            other_manager,
        ):
            self.assertIn(
                self.get_stats(user).status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )

    def test_query_count_does_not_grow(self):
        self.client.force_authenticate(self.fixture.staff)
        self.add_proposal(self.fixture.round, ProposalStates.IN_REVIEW, "expert_review")
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url)

        for _ in range(3):
            round_obj = factories.RoundFactory(call=self.call)
            for _ in range(3):
                proposal = self.add_proposal(
                    round_obj, ProposalStates.IN_REVIEW, "expert_review"
                )
                self.add_admin_check(
                    proposal,
                    WorkflowStepInstanceStatuses.COMPLETED,
                    WorkflowStepOutcomes.ELIGIBLE,
                )
                factories.ReviewFactory(proposal=proposal)
        with CaptureQueriesContext(connection) as large:
            self.client.get(self.url)

        self.assertEqual(len(small), len(large))


class ProposalWorkflowStepFilterTest(test.APITestCase):
    def test_filter_by_workflow_step(self):
        fixture = fixtures.ProposalFixture()
        in_review = factories.ProposalFactory(
            round=fixture.round, workflow_step="expert_review"
        )
        factories.ProposalFactory(
            round=fixture.round, workflow_step="administrative_check"
        )
        self.client.force_authenticate(fixture.staff)
        response = self.client.get(
            factories.ProposalFactory.get_list_url(),
            {"call_uuid": fixture.call.uuid.hex, "workflow_step": "expert_review"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([p["uuid"] for p in response.data], [in_review.uuid.hex])
