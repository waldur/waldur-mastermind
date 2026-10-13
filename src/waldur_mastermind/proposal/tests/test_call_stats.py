import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import ProposalStates
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
        ):
            models.CallWorkflowStep.objects.create(
                call=self.call, step=step, is_enabled=enabled, min_reviewers=2
            )
        self.client.force_authenticate(self.fixture.staff)

    def add_proposal(self, round_obj, step, admin_status=None):
        proposal = factories.ProposalFactory(
            round=round_obj, state=ProposalStates.IN_REVIEW, workflow_step=step
        )
        if admin_status:
            models.ProposalWorkflowStepInstance.objects.create(
                proposal=proposal,
                step="administrative_check",
                status=admin_status,
                outcome="eligible" if admin_status == "completed" else None,
            )
        return proposal

    def test_counts(self):
        round_a, round_b = self.fixture.round, self.fixture.new_round
        round_a.review_duration_in_days = 3
        round_a.save()
        # The fixture holds a draft and a submitted proposal with one review in A.
        models.Review.objects.update(created=timezone.now() - datetime.timedelta(5))
        self.add_proposal(round_a, "expert_review", "completed")
        self.add_proposal(round_b, "administrative_check", "active")
        self.add_proposal(round_b, "technical_assessment")

        data = self.client.get(self.url).data
        a, b = data["rounds"]
        self.assertEqual(a["uuid"], round_a.uuid)
        self.assertEqual(a["proposals"]["draft"], 1)
        self.assertEqual(a["proposals"]["in_review"], 1)
        self.assertEqual(a["admin_approved"], 1)
        self.assertEqual(a["missing_reviews"], 1)
        self.assertEqual(a["overdue_reviews"], 1)
        self.assertEqual(b["missing_admin_checks"], 1)
        self.assertEqual(data["call"]["proposals"]["in_review"], 3)
        self.assertEqual(
            [(s["step"], s["count"]) for s in data["call"]["steps"]],
            [("administrative_check", 1), ("expert_review", 1)],
        )

    def test_permissions(self):
        other_manager = structure_factories.UserFactory()
        factories.CallFactory().add_user(other_manager, CallRole.MANAGER)
        for user, allowed in (
            (self.fixture.call_manager, True),
            (self.fixture.call_organizer_user, True),
            (self.fixture.reviewer_1, False),
            (self.fixture.panel_member, False),
            (self.fixture.proposal_creator, False),
            (other_manager, False),
        ):
            self.client.force_authenticate(user)
            code = self.client.get(self.url).status_code
            self.assertEqual(code == status.HTTP_200_OK, allowed, user)

    def test_query_count_does_not_grow(self):
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url)
        for _ in range(3):
            round_obj = factories.RoundFactory(call=self.call)
            for _ in range(3):
                factories.ReviewFactory(
                    proposal=self.add_proposal(round_obj, "expert_review", "completed")
                )
        with CaptureQueriesContext(connection) as large:
            self.client.get(self.url)
        self.assertEqual(len(small), len(large))

    def test_proposals_filter_by_workflow_step(self):
        proposal = self.add_proposal(self.fixture.round, "expert_review")
        response = self.client.get(
            factories.ProposalFactory.get_list_url(),
            {"call_uuid": self.call.uuid.hex, "workflow_step": "expert_review"},
        )
        self.assertEqual([p["uuid"] for p in response.data], [proposal.uuid.hex])
