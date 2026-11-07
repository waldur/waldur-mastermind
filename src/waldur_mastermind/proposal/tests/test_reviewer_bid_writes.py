"""Who may write a reviewer bid through the generic routes.

A bid is the reviewer's own preference. Only that reviewer may change or
withdraw it, and only while they could place it through ``submit``: as an
accepted member of the call's reviewer pool, in good standing. Everyone else
who can see the bid (the call's managers, roles on the managing customer, its
call organizers, staff) reads it but cannot write it. Bids are placed through
``submit`` and ``bulk-submit``; the generic create is disabled.
"""

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole, CustomerRole
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    ReviewerPoolInvitationStatuses,
    ReviewerStandings,
)

from . import factories


def proposal_url(proposal):
    return "http://testserver" + reverse(
        "proposal-proposal-detail", kwargs={"uuid": proposal.uuid.hex}
    )


class ReviewerBidWritesTest(test.APITestCase):
    def setUp(self):
        self.call = factories.CallFactory()
        self.proposal = factories.ProposalFactory(round__call=self.call)
        self.pool_entry = factories.CallReviewerPoolFactory(call=self.call)
        self.reviewer = self.pool_entry.reviewer
        self.bid = models.ReviewerBid.objects.create(
            call=self.call,
            reviewer=self.reviewer,
            proposal=self.proposal,
            bid="eager",
        )
        self.url = reverse("reviewer-bid-detail", kwargs={"uuid": self.bid.uuid.hex})

    def readers(self):
        manager = structure_factories.UserFactory()
        self.call.add_user(manager, CallRole.MANAGER)
        support = structure_factories.UserFactory()
        self.call.manager.customer.add_user(support, CustomerRole.SUPPORT)
        organizer = structure_factories.UserFactory()
        self.call.manager.add_user(
            organizer,
            Role.objects.get_system_role(
                "CUSTOMER.CALL_ORGANIZER",
                content_type=ContentType.objects.get_for_model(
                    models.CallManagingOrganisation
                ),
            ),
        )
        staff = structure_factories.UserFactory(is_staff=True)
        return {
            "call manager": manager,
            "customer support": support,
            "call organizer": organizer,
            "staff": staff,
        }

    def assert_unchanged(self):
        self.bid.refresh_from_db()
        self.assertEqual(self.bid.bid, "eager")
        self.assertEqual(self.bid.proposal, self.proposal)

    def test_reviewer_changes_own_bid(self):
        self.client.force_authenticate(self.reviewer.user)
        response = self.client.patch(
            self.url, {"bid": "not_willing", "comment": "Busy"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.bid.refresh_from_db()
        self.assertEqual(self.bid.bid, "not_willing")
        self.assertEqual(self.bid.comment, "Busy")

    def test_reviewer_replaces_own_bid(self):
        self.client.force_authenticate(self.reviewer.user)
        response = self.client.put(
            self.url, {"bid": "willing", "comment": ""}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.bid.refresh_from_db()
        self.assertEqual(self.bid.bid, "willing")
        self.assertEqual(self.bid.proposal, self.proposal)

    def test_reviewer_withdraws_own_bid(self):
        self.client.force_authenticate(self.reviewer.user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.ReviewerBid.objects.filter(pk=self.bid.pk).exists())

    def test_readers_see_the_bid_but_cannot_write_it(self):
        for name, user in self.readers().items():
            with self.subTest(caller=name):
                self.client.force_authenticate(user)
                self.assertEqual(
                    self.client.get(self.url).status_code, status.HTTP_200_OK
                )
                response = self.client.patch(
                    self.url, {"bid": "not_willing"}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                response = self.client.put(
                    self.url, {"bid": "not_willing"}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                response = self.client.delete(self.url)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assert_unchanged()

    def test_proposal_cannot_be_moved(self):
        other_proposal = factories.ProposalFactory()
        self.client.force_authenticate(self.reviewer.user)
        response = self.client.patch(
            self.url, {"proposal": proposal_url(other_proposal)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assert_unchanged()

    def test_reviewer_removed_from_pool_cannot_write(self):
        self.pool_entry.invitation_status = ReviewerPoolInvitationStatuses.DECLINED
        self.pool_entry.save()
        self.client.force_authenticate(self.reviewer.user)
        response = self.client.patch(self.url, {"bid": "not_willing"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assert_unchanged()

    def test_suspended_reviewer_cannot_write(self):
        self.reviewer.standing = ReviewerStandings.SUSPENDED
        self.reviewer.save()
        self.client.force_authenticate(self.reviewer.user)
        response = self.client.patch(self.url, {"bid": "not_willing"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assert_unchanged()

    def test_create_is_disabled(self):
        self.client.force_authenticate(self.reviewer.user)
        response = self.client.post(
            reverse("reviewer-bid-list"),
            {"proposal": proposal_url(self.proposal), "bid": "willing"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_reviewer_still_submits_through_submit(self):
        self.bid.delete()
        self.client.force_authenticate(self.reviewer.user)
        response = self.client.post(
            reverse("reviewer-bid-submit-bid"),
            {"proposal_uuid": self.proposal.uuid.hex, "bid": "willing"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            models.ReviewerBid.objects.filter(
                call=self.call, reviewer=self.reviewer, proposal=self.proposal
            ).exists()
        )
