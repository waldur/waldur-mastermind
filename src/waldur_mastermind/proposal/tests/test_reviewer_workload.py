from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    ProposalStates,
    ReviewerPoolInvitationStatuses,
)

from . import factories


class WorkloadBaseTest(test.APITestCase):
    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.call = factories.CallFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call_manager = structure_factories.UserFactory()
        self.call.add_user(self.call_manager, CallRole.MANAGER)
        self.call.manager.add_user(self.call_manager, CallRole.MANAGER)
        self.round = factories.RoundFactory(call=self.call)
        self.reviewer_profile = factories.ReviewerProfileFactory()
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer_profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
            max_assignments=2,
        )

    def make_proposal(self, **kwargs):
        kwargs.setdefault("state", ProposalStates.SUBMITTED)
        return factories.ProposalFactory(round=self.round, **kwargs)

    def make_item(
        self,
        item_status=AssignmentItemStatuses.PENDING,
        batch_status=AssignmentBatchStatuses.SENT,
        review_state=None,
        pool_entry=None,
    ):
        pool_entry = pool_entry or self.pool_entry
        batch = factories.AssignmentBatchFactory(
            call=self.call, reviewer_pool_entry=pool_entry, status=batch_status
        )
        proposal = self.make_proposal()
        review = None
        if review_state:
            review = factories.ReviewFactory(
                proposal=proposal,
                reviewer=pool_entry.reviewer.user,
                state=review_state,
            )
        return factories.AssignmentItemFactory(
            batch=batch, proposal=proposal, status=item_status, review=review
        )

    def open_assignments(self, entry=None):
        entry = entry or self.pool_entry
        return (
            models.CallReviewerPool.objects.with_open_assignments()
            .get(pk=entry.pk)
            .open_assignments
        )


class OpenAssignmentCountTest(WorkloadBaseTest):
    def test_pending_items_count(self):
        self.make_item(batch_status=AssignmentBatchStatuses.DRAFT)
        self.make_item(batch_status=AssignmentBatchStatuses.SENT)
        self.assertEqual(self.open_assignments(), 2)

    def test_pending_items_in_cancelled_or_expired_batches_do_not_count(self):
        self.make_item(batch_status=AssignmentBatchStatuses.CANCELLED)
        self.make_item(batch_status=AssignmentBatchStatuses.EXPIRED)
        self.assertEqual(self.open_assignments(), 0)

    def test_accepted_item_with_review_in_progress_counts_once(self):
        self.make_item(
            item_status=AssignmentItemStatuses.ACCEPTED,
            batch_status=AssignmentBatchStatuses.RESPONDED,
            review_state=models.Review.States.IN_REVIEW,
        )
        self.assertEqual(self.open_assignments(), 1)

    def test_accepted_item_with_finished_review_does_not_count(self):
        for state in (models.Review.States.SUBMITTED, models.Review.States.REJECTED):
            self.make_item(
                item_status=AssignmentItemStatuses.ACCEPTED,
                batch_status=AssignmentBatchStatuses.RESPONDED,
                review_state=state,
            )
        self.assertEqual(self.open_assignments(), 0)

    def test_closed_items_do_not_count(self):
        for item_status in (
            AssignmentItemStatuses.DECLINED,
            AssignmentItemStatuses.EXPIRED,
            AssignmentItemStatuses.REASSIGNED,
            AssignmentItemStatuses.COI_BLOCKED,
        ):
            self.make_item(item_status=item_status)
        self.assertEqual(self.open_assignments(), 0)

    def test_direct_reviews_in_progress_count(self):
        user = self.reviewer_profile.user
        factories.ReviewFactory(proposal=self.make_proposal(), reviewer=user)
        factories.ReviewFactory(
            proposal=self.make_proposal(),
            reviewer=user,
            state=models.Review.States.SUBMITTED,
        )
        factories.ReviewFactory(
            proposal=self.make_proposal(),
            reviewer=user,
            state=models.Review.States.REJECTED,
        )
        self.assertEqual(self.open_assignments(), 1)

    def test_reviews_and_items_of_other_calls_do_not_count(self):
        other_call = factories.CallFactory()
        other_round = factories.RoundFactory(call=other_call)
        factories.ReviewFactory(
            proposal=factories.ProposalFactory(round=other_round),
            reviewer=self.reviewer_profile.user,
        )
        other_entry = factories.CallReviewerPoolFactory(
            call=other_call, reviewer=self.reviewer_profile
        )
        factories.AssignmentItemFactory(
            batch=factories.AssignmentBatchFactory(
                call=other_call, reviewer_pool_entry=other_entry
            ),
            proposal=factories.ProposalFactory(round=other_round),
        )
        self.assertEqual(self.open_assignments(), 0)

    def test_email_only_pool_entry_has_no_open_assignments(self):
        entry = factories.CallReviewerPoolFactory(
            call=self.call, reviewer=None, invited_email="someone@example.com"
        )
        self.assertEqual(self.open_assignments(entry), 0)


class PoolApiTest(WorkloadBaseTest):
    def test_pool_detail_shows_open_assignments(self):
        self.make_item()
        # The stored column is not what the API reports.
        models.CallReviewerPool.objects.filter(pk=self.pool_entry.pk).update(
            current_assignments=7
        )
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            factories.CallReviewerPoolFactory.get_url(self.pool_entry)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["current_assignments"], 1)

    def test_call_reviewer_pool_action_shows_open_assignments(self):
        self.make_item()
        self.make_item()
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            factories.CallFactory.get_protected_url(self.call, action="reviewer-pool")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["current_assignments"], 2)

    def test_pool_list_can_be_ordered_by_open_assignments(self):
        busy = factories.CallReviewerPoolFactory(call=self.call)
        self.make_item(pool_entry=busy)
        self.make_item(pool_entry=busy)
        self.make_item()
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            factories.CallReviewerPoolFactory.get_list_url(),
            {"call_uuid": self.call.uuid.hex, "o": "-current_assignments"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row["current_assignments"] for row in response.data], [2, 1])

    def test_pool_list_query_count_does_not_grow_with_entries(self):
        self.make_item()
        self.client.force_authenticate(self.staff)
        url = factories.CallReviewerPoolFactory.get_list_url()
        with CaptureQueriesContext(connection) as one:
            self.client.get(url)
        for _ in range(3):
            entry = factories.CallReviewerPoolFactory(call=self.call)
            self.make_item(pool_entry=entry)
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(url)
        self.assertEqual(len(response.data), 4)
        self.assertEqual(len(many.captured_queries), len(one.captured_queries))


class GenerateAssignmentsLimitTest(WorkloadBaseTest):
    def generate(self, **payload):
        self.client.force_authenticate(self.staff)
        return self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="generate-assignments"
            ),
            {"reviewers_per_proposal": 1, **payload},
            format="json",
        )

    def created_items(self, entry=None):
        return models.AssignmentItem.objects.filter(
            batch__reviewer_pool_entry=entry or self.pool_entry,
            status=AssignmentItemStatuses.PENDING,
        )

    def test_limit_is_respected_within_one_run(self):
        self.pool_entry.max_assignments = 1
        self.pool_entry.save()
        for _ in range(3):
            self.make_proposal()

        response = self.generate()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["items_created"], 1)
        self.assertEqual(self.created_items().count(), 1)
        self.assertEqual(len(response.data["skipped_proposals"]), 2)

    def test_existing_open_assignments_count_against_limit(self):
        self.make_item()
        self.make_item()
        # The proposals behind the existing items are already assigned, so
        # only the new one is a candidate; the reviewer is at the limit.
        new_proposal = self.make_proposal()

        response = self.generate(proposal_uuids=[new_proposal.uuid.hex])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["items_created"], 0)
        self.assertFalse(
            models.AssignmentItem.objects.filter(proposal=new_proposal).exists()
        )

    def test_finished_assignments_free_capacity(self):
        self.make_item(item_status=AssignmentItemStatuses.DECLINED)
        self.make_item(
            item_status=AssignmentItemStatuses.ACCEPTED,
            review_state=models.Review.States.SUBMITTED,
        )
        new_proposal = self.make_proposal()

        response = self.generate(proposal_uuids=[new_proposal.uuid.hex])

        self.assertEqual(response.data["items_created"], 1)

    def test_load_spreads_to_other_reviewers(self):
        self.pool_entry.max_assignments = 1
        self.pool_entry.save()
        other = factories.CallReviewerPoolFactory(call=self.call, max_assignments=1)
        self.make_proposal()
        self.make_proposal()

        response = self.generate()

        self.assertEqual(response.data["items_created"], 2)
        self.assertEqual(self.created_items().count(), 1)
        self.assertEqual(self.created_items(other).count(), 1)


class ManualAssignmentLimitTest(WorkloadBaseTest):
    def assign(self, proposals, user=None, **payload):
        self.client.force_authenticate(user or self.call_manager)
        return self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="create-manual-assignment"
            ),
            {
                "reviewer_pool_entry_uuid": self.pool_entry.uuid.hex,
                "proposal_uuids": [p.uuid.hex for p in proposals],
                **payload,
            },
            format="json",
        )

    def test_assignment_within_limit_is_allowed(self):
        self.make_item()
        response = self.assign([self.make_proposal()])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["items_created"], 1)

    def test_assignment_above_limit_is_refused(self):
        self.make_item()
        proposals = [self.make_proposal(), self.make_proposal()]

        response = self.assign(proposals)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("override_workload_limit", str(response.data))
        self.assertFalse(
            models.AssignmentItem.objects.filter(proposal__in=proposals).exists()
        )

    def test_duplicates_do_not_count_as_new_assignments(self):
        item = self.make_item()
        self.make_item()
        response = self.assign([item.proposal])
        # Refused for having nothing new to assign, not for the workload.
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already exists", str(response.data["proposal_uuids"][0]))

    def test_override_allows_assignment_and_is_logged(self):
        self.make_item()
        self.make_item()

        response = self.assign([self.make_proposal()], override_workload_limit=True)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["items_created"], 1)
        self.assertEqual(self.open_assignments(), 3)
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.REVIEWER_WORKLOAD_LIMIT_OVERRIDDEN
            ).exists()
        )


class ReviewCreateLimitTest(WorkloadBaseTest):
    def setUp(self):
        super().setUp()
        self.reviewer = self.reviewer_profile.user
        self.call.add_user(self.reviewer, CallRole.REVIEWER)

    def create_review(self, **payload):
        self.client.force_authenticate(self.call_manager)
        return self.client.post(
            factories.ReviewFactory.get_list_url(),
            {
                "proposal": factories.ProposalFactory.get_url(self.make_proposal()),
                "reviewer": structure_factories.UserFactory.get_url(self.reviewer),
                **payload,
            },
        )

    def test_review_within_limit_is_created(self):
        self.make_item()
        response = self.create_review()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.open_assignments(), 2)

    def test_review_above_limit_is_refused(self):
        self.make_item()
        self.make_item()
        response = self.create_review()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("override_workload_limit", str(response.data))

    def test_override_allows_review_and_is_logged(self):
        self.make_item()
        self.make_item()
        response = self.create_review(override_workload_limit=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.REVIEWER_WORKLOAD_LIMIT_OVERRIDDEN
            ).exists()
        )

    def test_reviewer_outside_pool_is_not_limited(self):
        self.pool_entry.delete()
        response = self.create_review()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class GenerateAssignmentsExistingReviewersTest(WorkloadBaseTest):
    """A proposal's reviewers are not offered it again, and a proposal that
    has its reviewers is not given more."""

    def setUp(self):
        super().setUp()
        self.proposal = self.make_proposal()
        self.other_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
            max_assignments=5,
        )

    def generate(self, per_proposal):
        self.client.force_authenticate(self.staff)
        return self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="generate-assignments"
            ),
            {
                "reviewers_per_proposal": per_proposal,
                "proposal_uuids": [self.proposal.uuid.hex],
            },
            format="json",
        )

    def offered(self):
        return set(
            models.AssignmentItem.objects.filter(proposal=self.proposal).values_list(
                "batch__reviewer_pool_entry", flat=True
            )
        )

    def test_a_reviewer_of_the_proposal_is_not_offered_it(self):
        factories.ReviewFactory(
            proposal=self.proposal, reviewer=self.reviewer_profile.user
        )
        response = self.generate(per_proposal=2)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.offered(), {self.other_entry.pk})

    def test_a_proposal_with_its_reviewers_gets_no_more(self):
        factories.ReviewFactory(
            proposal=self.proposal, reviewer=self.reviewer_profile.user
        )
        response = self.generate(per_proposal=1)
        self.assertEqual(response.data["items_created"], 0)
        self.assertEqual(self.offered(), set())
        (skipped,) = response.data["skipped_proposals"]
        self.assertEqual(skipped["reason"], "Already has enough reviewers")


class GenerateAssignmentsMatchingTest(WorkloadBaseTest):
    """Reviewers are taken by affinity, within the call's matching limits."""

    def setUp(self):
        super().setUp()
        self.pool_entry.max_assignments = 5
        self.pool_entry.save()
        self.proposal = self.make_proposal()

    def put_first_in_pool_order(self, entry):
        # The pool is ordered newest invitation first.
        models.CallReviewerPool.objects.filter(pk=entry.pk).update(
            invited_at=timezone.now() + timedelta(days=1)
        )

    def entry(self, affinity=None, proposal=None, **kwargs):
        entry = factories.CallReviewerPoolFactory(
            call=self.call,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
            **kwargs,
        )
        if affinity is not None:
            models.ReviewerProposalAffinity.objects.create(
                call=self.call,
                reviewer=entry.reviewer,
                proposal=proposal or self.proposal,
                affinity_score=affinity,
            )
        return entry

    def generate(self, **payload):
        self.client.force_authenticate(self.staff)
        return self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="generate-assignments"
            ),
            payload,
            format="json",
        )

    def offered(self, proposal=None):
        return set(
            models.AssignmentItem.objects.filter(
                proposal=proposal or self.proposal
            ).values_list("batch__reviewer_pool_entry", flat=True)
        )

    def test_a_full_top_reviewer_is_replaced_by_the_next_best(self):
        top = self.entry(affinity=0.9, max_assignments=1)
        self.make_item(pool_entry=top)
        second = self.entry(affinity=0.8)
        third = self.entry(affinity=0.7)
        # A reviewer with no affinity, first in pool order, is not preferred
        # to the next-best scored one.
        self.put_first_in_pool_order(self.pool_entry)

        response = self.generate(
            reviewers_per_proposal=2, proposal_uuids=[self.proposal.uuid.hex]
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.offered(), {second.pk, third.pk})

    def test_reviewers_without_affinity_fill_up_after_the_scored_ones(self):
        scored = self.entry(affinity=0.5)

        self.generate(reviewers_per_proposal=2, proposal_uuids=[self.proposal.uuid.hex])

        self.assertEqual(self.offered(), {scored.pk, self.pool_entry.pk})

    def test_a_reviewer_below_the_minimum_affinity_is_not_offered(self):
        factories.MatchingConfigurationFactory(
            call=self.call, min_affinity_threshold=0.3
        )
        good = self.entry(affinity=0.6)
        self.entry(affinity=0.2)

        self.generate(reviewers_per_proposal=3, proposal_uuids=[self.proposal.uuid.hex])

        # The poor match is left out even though the proposal stays short;
        # the reviewer with no affinity computed still fills up.
        self.assertEqual(self.offered(), {good.pk, self.pool_entry.pk})

    def test_without_configuration_the_default_minimum_applies(self):
        default = models.MatchingConfiguration._meta.get_field(
            "min_affinity_threshold"
        ).default
        self.entry(affinity=default / 2)

        self.generate(reviewers_per_proposal=2, proposal_uuids=[self.proposal.uuid.hex])

        self.assertEqual(self.offered(), {self.pool_entry.pk})

    def test_a_proposal_nobody_matches_closely_enough_is_skipped(self):
        models.ReviewerProposalAffinity.objects.create(
            call=self.call,
            reviewer=self.reviewer_profile,
            proposal=self.proposal,
            affinity_score=0.01,
        )

        response = self.generate(
            reviewers_per_proposal=1, proposal_uuids=[self.proposal.uuid.hex]
        )

        self.assertEqual(response.data["items_created"], 0)
        self.assertEqual(
            response.data["skipped_proposals"][0]["reason"],
            "No eligible reviewer reaches the minimum affinity",
        )

    def test_without_configuration_the_model_default_is_used(self):
        for _ in range(3):
            self.entry()
        default = models.MatchingConfiguration._meta.get_field(
            "min_reviewers_per_proposal"
        ).default

        response = self.generate(proposal_uuids=[self.proposal.uuid.hex])

        self.assertEqual(response.data["items_created"], default)

    def test_configured_minimum_is_used(self):
        for _ in range(3):
            self.entry()
        factories.MatchingConfigurationFactory(
            call=self.call, min_reviewers_per_proposal=2
        )
        response = self.generate(proposal_uuids=[self.proposal.uuid.hex])
        self.assertEqual(response.data["items_created"], 2)

    def test_more_reviewers_than_the_call_allows_is_refused(self):
        factories.MatchingConfigurationFactory(
            call=self.call, min_reviewers_per_proposal=1, max_reviewers_per_proposal=2
        )
        response = self.generate(reviewers_per_proposal=3)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reviewers_per_proposal", response.data)
        self.assertFalse(models.AssignmentItem.objects.exists())

    def test_max_proposals_per_reviewer_caps_the_pool_limit(self):
        factories.MatchingConfigurationFactory(
            call=self.call,
            min_reviewers_per_proposal=1,
            max_proposals_per_reviewer=1,
        )
        self.make_proposal()

        response = self.generate()

        self.assertEqual(response.data["items_created"], 1)
        self.assertEqual(
            models.AssignmentItem.objects.filter(
                batch__reviewer_pool_entry=self.pool_entry
            ).count(),
            1,
        )

    def test_the_pool_limit_holds_below_max_proposals_per_reviewer(self):
        self.pool_entry.max_assignments = 1
        self.pool_entry.save()
        factories.MatchingConfigurationFactory(
            call=self.call,
            min_reviewers_per_proposal=1,
            max_proposals_per_reviewer=10,
        )
        self.make_proposal()

        response = self.generate()

        self.assertEqual(response.data["items_created"], 1)
