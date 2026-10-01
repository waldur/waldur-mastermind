import datetime

from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole, OfferingRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
)
from waldur_mastermind.proposal.tests import factories, fixtures


class ProposalAsRoleFilterTest(test.APITestCase):
    """``as_role`` on the proposal list: one hat at a time.

    The scenario is the one the peer-review deployments actually produce — a
    single person who applies to one call, reviews for a second, sits on a third
    call's panel, manages a fourth and technically assesses requests for their
    own offering. Their readable list is the union of all five, which is exactly
    what makes it unusable; each test below pins one role's slice of it.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ProposalFactory.get_list_url()
        self.user = structure_factories.UserFactory()

        # Applies here, holds no role on the call.
        self.applicant_round = self._make_round()
        self.own_proposal = factories.ProposalFactory(
            name="Mine", round=self.applicant_round, created_by=self.user
        )

        # Manages this one; the proposal on it is somebody else's.
        self.managed_round = self._make_round()
        self.managed_round.call.add_user(self.user, CallRole.MANAGER)
        self.managed_proposal = factories.ProposalFactory(
            name="On a call I manage", round=self.managed_round
        )

        # Reviews for this one.
        self.reviewed_round = self._make_round()
        self.reviewed_round.call.add_user(self.user, CallRole.REVIEWER)
        self.reviewed_proposal = factories.ProposalFactory(
            name="On a call I review for", round=self.reviewed_round
        )

        # Sits on this one's panel.
        self.panel_round = self._make_round()
        self.panel_round.call.add_user(self.user, CallRole.PANEL_MEMBER)
        self.panel_proposal = factories.ProposalFactory(
            name="On a call I sit on the panel of", round=self.panel_round
        )

        # Technically assesses this one: it requested an accepted offering they
        # manage. No call-scoped role is involved.
        self.assessed_round = self._make_round()
        requested_offering = factories.RequestedOfferingFactory(
            call=self.assessed_round.call,
            state=RequestedOfferingStates.ACCEPTED,
            offering=self.fixture.offering,
        )
        self.fixture.offering.add_user(self.user, OfferingRole.MANAGER)
        self.assessed_proposal = factories.ProposalFactory(
            name="Requests my offering",
            round=self.assessed_round,
            state=ProposalStates.SUBMITTED,
        )
        factories.RequestedResourceFactory(
            proposal=self.assessed_proposal, requested_offering=requested_offering
        )

        # Nothing connects this one to the user at all.
        self.unrelated_proposal = factories.ProposalFactory(
            name="Not mine in any sense", round=self._make_round()
        )

    def _make_round(self):
        call = factories.CallFactory(
            manager=self.fixture.manager, state=CallStates.ACTIVE
        )
        return factories.RoundFactory(
            call=call,
            start_time=timezone.now(),
            cutoff_time=timezone.now() + datetime.timedelta(days=10),
        )

    def _names(self, role=None, user=None):
        self.client.force_authenticate(user or self.user)
        query = {"as_role": role} if role else {}
        response = self.client.get(self.url, query)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["name"] for row in response.data}

    def test_without_the_filter_every_hat_is_mixed_together(self):
        self.assertEqual(
            self._names(),
            {
                self.own_proposal.name,
                self.managed_proposal.name,
                self.reviewed_proposal.name,
                self.panel_proposal.name,
                self.assessed_proposal.name,
            },
        )

    def test_applicant_sees_only_their_own(self):
        self.assertEqual(
            self._names(ResponsibleRoles.APPLICANT), {self.own_proposal.name}
        )

    def test_call_manager_sees_only_proposals_of_calls_they_manage(self):
        self.assertEqual(
            self._names(ResponsibleRoles.CALL_MANAGER), {self.managed_proposal.name}
        )

    def test_reviewer_sees_only_proposals_of_calls_they_review_for(self):
        self.assertEqual(
            self._names(ResponsibleRoles.REVIEWER), {self.reviewed_proposal.name}
        )

    def test_reviewer_sees_proposals_they_hold_a_review_for(self):
        # Accepting a pool invitation or an assignment grants no call role at
        # all — the reviewer reads the proposal through the review — so the call
        # traversal alone returned nothing for the most common reviewer there is.
        lone = structure_factories.UserFactory()
        proposal = factories.ProposalFactory(
            name="Assigned to me", round=self._make_round()
        )
        factories.ReviewFactory(proposal=proposal, reviewer=lone)

        self.assertEqual(self._names(user=lone), {proposal.name})
        self.assertEqual(
            self._names(ResponsibleRoles.REVIEWER, user=lone), {proposal.name}
        )

    def test_a_rejected_review_stops_being_a_reason_to_read(self):
        lone = structure_factories.UserFactory()
        proposal = factories.ProposalFactory(
            name="No longer mine", round=self._make_round()
        )
        review = factories.ReviewFactory(proposal=proposal, reviewer=lone)
        review.state = models.Review.States.REJECTED
        review.save()

        self.assertEqual(self._names(ResponsibleRoles.REVIEWER, user=lone), set())

    def test_panel_member_sees_only_proposals_of_their_panels(self):
        self.assertEqual(
            self._names(ResponsibleRoles.PANEL_MEMBER), {self.panel_proposal.name}
        )

    def test_offering_manager_sees_only_what_requested_their_offering(self):
        self.assertEqual(
            self._names(ResponsibleRoles.OFFERING_MANAGER),
            {self.assessed_proposal.name},
        )

    def test_the_filter_never_widens_the_readable_set(self):
        for role, _ in ResponsibleRoles.CHOICES:
            with self.subTest(role=role):
                self.assertNotIn(self.unrelated_proposal.name, self._names(role))

    def test_offering_manager_still_excludes_drafts(self):
        # The readable scope itself withholds drafts from technical reviewers
        # (get_offering_manager_proposals), and narrowing must not resurrect one.
        self.assessed_proposal.state = ProposalStates.DRAFT
        self.assessed_proposal.save()
        self.assertEqual(self._names(ResponsibleRoles.OFFERING_MANAGER), set())

    def test_call_organizer_counts_as_a_call_manager(self):
        # The other way a call is managed: CUSTOMER.CALL_ORGANIZER on the
        # organisation running it, rather than CALL.MANAGER on the call.
        organizer = self.fixture.call_organizer_user
        self.assertIn(
            self.managed_proposal.name,
            self._names(ResponsibleRoles.CALL_MANAGER, user=organizer),
        )

    def test_a_proposal_role_holder_counts_as_an_applicant(self):
        # A team member added to someone else's proposal is an applicant on it
        # too, which `my_proposals` (created_by only) misses. The proposal is on
        # a call this user has nothing to do with, so the proposal role is the
        # only thing that can reach it — on managed_proposal the call-manager
        # role would have made the assertion pass either way.
        teammates = factories.ProposalFactory(
            name="A colleague's, with me on the team", round=self._make_round()
        )
        teammates.add_user(self.user, ProposalRole.MEMBER)

        self.assertEqual(
            self._names(ResponsibleRoles.APPLICANT),
            {self.own_proposal.name, teammates.name},
        )

    def test_an_unknown_role_is_rejected_rather_than_ignored(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url, {"as_role": "wizard"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_default_is_unchanged(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.assertIn(self.unrelated_proposal.name, self._names(user=staff))

    def test_staff_asking_for_one_role_get_that_role(self):
        # Opt-in only: the filter narrows whoever passes it, staff included.
        staff = structure_factories.UserFactory(is_staff=True)
        self.assertEqual(self._names(ResponsibleRoles.APPLICANT, user=staff), set())
