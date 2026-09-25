"""Support users read call-management data but write none of it.

Support can read every call, so the reviewer pool, COI and matching settings,
reviews, suggestions and assignment data around a call have to be readable to
them too -- and every write on them has to stay refused.
"""

from ddt import data, ddt
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    ReviewerPoolInvitationStatuses,
    ReviewerSuggestionStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures


class SupportReadAccessBase(APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.support = structure_factories.UserFactory(is_support=True)
        self.proposal = self.fixture.proposal

        self.pool_member = factories.CallReviewerPoolFactory(
            call=self.call,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        self.pending_member = factories.CallReviewerPoolFactory(
            call=self.call,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.coi = factories.ConflictOfInterestFactory(
            proposal=self.proposal, reviewer=self.pool_member.reviewer
        )
        self.batch = factories.AssignmentBatchFactory(
            call=self.call, reviewer_pool_entry=self.pool_member
        )
        self.suggestion = models.ReviewerSuggestion.objects.create(
            call=self.call,
            reviewer=factories.ReviewerProfileFactory(),
            affinity_score=0.8,
            status=ReviewerSuggestionStatuses.PENDING,
        )
        self.assignment_config = models.CallAssignmentConfiguration.objects.create(
            call=self.call
        )

    def call_url(self, action):
        return factories.CallFactory.get_protected_url(self.call, action=action)


@ddt
class SupportReadsCallActionsTest(SupportReadAccessBase):
    @data(
        "compliance_overview",
        "reviewer-pool",
        "suggestions",
        "coi-configuration",
        "conflicts",
        "conflict-summary",
        "matching-configuration",
        "affinity-matrix",
        "proposed-assignments",
    )
    def test_support_can_read(self, action):
        self.client.force_authenticate(self.support)
        response = self.client.get(self.call_url(action))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data(
        "reviewer-pool",
        "coi-configuration",
        "matching-configuration",
    )
    def test_user_without_role_still_cannot_read(self, action):
        # The call itself is out of reach for a user with no role, so the
        # action never gets as far as its permission check.
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(self.call_url(action))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("reviewer_1", "panel_member")
    def test_call_role_without_review_management_cannot_read(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.call_url("coi-configuration"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_support_can_list_available_compliance_checklists(self):
        self.client.force_authenticate(self.support)
        response = self.client.get(
            factories.CallFactory.get_protected_list_url(
                action="available_compliance_checklists"
            ),
            {"customer_uuid": self.fixture.customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class SupportCannotWriteCallActionsTest(SupportReadAccessBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.support)

    def test_invite_to_reviewer_pool(self):
        response = self.client.post(
            self.call_url("reviewer-pool"), {"email": "someone@example.com"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_coi_configuration(self):
        response = self.client.patch(
            self.call_url("coi-configuration"), {"recusal_required": False}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_matching_configuration(self):
        response = self.client.patch(
            self.call_url("matching-configuration"), {"min_reviewers": 1}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_call(self):
        response = self.client.patch(
            factories.CallFactory.get_protected_url(self.call),
            {"description": "changed"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_force_accept_pool_invitation(self):
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(
                self.pending_member, action="force-accept"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_dismiss_conflict(self):
        response = self.client.post(
            factories.ConflictOfInterestFactory.get_url(self.coi, action="dismiss"),
            {"resolution_comment": "x"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_send_assignment_batch(self):
        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(self.batch, action="send")
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_assignment_configuration(self):
        response = self.client.patch(
            f"/api/call-assignment-configurations/{self.assignment_config.uuid.hex}/",
            {"assignment_expiration_days": 1},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_delete_suggestion(self):
        response = self.client.delete(
            f"/api/reviewer-suggestions/{self.suggestion.uuid.hex}/"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SupportListsCallManagementDataTest(SupportReadAccessBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.support)

    def uuids(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def test_reviewer_pool(self):
        self.assertIn(
            self.pool_member.uuid.hex, self.uuids("/api/call-reviewer-pools/")
        )

    def test_conflicts_of_interest(self):
        self.assertIn(self.coi.uuid.hex, self.uuids("/api/conflicts-of-interest/"))

    def test_assignment_batches(self):
        self.assertIn(self.batch.uuid.hex, self.uuids("/api/assignment-batches/"))

    def test_reviewer_suggestions(self):
        self.assertIn(
            self.suggestion.uuid.hex, self.uuids("/api/reviewer-suggestions/")
        )

    def test_assignment_configurations(self):
        self.assertIn(
            self.assignment_config.uuid.hex,
            self.uuids("/api/call-assignment-configurations/"),
        )

    def test_reviews_with_reviewer_identity(self):
        review = self.fixture.review
        response = self.client.get(f"/api/proposal-reviews/{review.uuid.hex}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("reviewer_full_name", response.data)

    def test_reviewer_profiles_are_limited_to_accepted_pool_members(self):
        uuids = self.uuids("/api/reviewer-profiles/")
        self.assertIn(self.pool_member.reviewer.uuid.hex, uuids)
        self.assertNotIn(self.pending_member.reviewer.uuid.hex, uuids)


class AssignmentConfigurationWriteTest(SupportReadAccessBase):
    def url(self):
        return f"/api/call-assignment-configurations/{self.assignment_config.uuid.hex}/"

    def patch_as(self, user):
        self.client.force_authenticate(user)
        return self.client.patch(self.url(), {"assignment_expiration_days": 3})

    def test_reviewer_cannot_update(self):
        response = self.patch_as(self.fixture.reviewer_1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_panel_member_cannot_delete(self):
        self.client.force_authenticate(self.fixture.panel_member)
        response = self.client.delete(self.url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.CallAssignmentConfiguration.objects.filter(
                pk=self.assignment_config.pk
            ).exists()
        )

    def test_call_manager_can_update(self):
        response = self.patch_as(self.fixture.call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assignment_config.refresh_from_db()
        self.assertEqual(self.assignment_config.assignment_expiration_days, 3)

    def test_staff_can_update(self):
        response = self.patch_as(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_is_refused(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            "/api/call-assignment-configurations/", {"assignment_expiration_days": 3}
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
