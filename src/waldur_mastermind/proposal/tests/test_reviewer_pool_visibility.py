"""Who may read a call's reviewer pool, and the review data about the same
reviewers: those who manage the call's review, staff, support, a site-wide
CALL.LIST grant (where the endpoint honours one), and the reviewers
themselves."""

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CallRole,
    CustomerRole,
    GlobalRole,
    ProjectRole,
)
from waldur_core.permissions.models import Role
from waldur_core.permissions.tests.factories import RoleFactory
from waldur_core.permissions.utils import grant_global_role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    ReviewerPoolInvitationStatuses,
    ReviewerSuggestionStatuses,
)
from waldur_mastermind.proposal.tests import factories


class ReviewRolesMixin:
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.organisation = factories.CallManagingOrganisationFactory(
            customer=self.customer
        )
        self.call = factories.CallFactory(manager=self.organisation)

        self.organizer_role = Role.objects.get_system_role(
            "CUSTOMER.CALL_ORGANIZER",
            content_type=ContentType.objects.get_for_model(
                models.CallManagingOrganisation
            ),
        )
        # Mirrors permissions.yaml: call managers and call organisers manage
        # the review; owners and readers list their organisation's calls but
        # do not manage their review.
        for role in (CallRole.MANAGER, self.organizer_role):
            role.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
            role.add_permission(PermissionEnum.LIST_CALLS)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_CALLS)
        CustomerRole.READER.add_permission(PermissionEnum.VIEW_CALL_RESULTS)

    def user_with(self, scope, role):
        user = structure_factories.UserFactory()
        scope.add_user(user, role)
        return user

    def call_manager(self):
        return self.user_with(self.call, CallRole.MANAGER)

    def call_organiser(self):
        return self.user_with(self.organisation, self.organizer_role)

    def owner(self):
        return self.user_with(self.customer, CustomerRole.OWNER)

    def reader(self):
        return self.user_with(self.customer, CustomerRole.READER)

    def uuids(self, user, url):
        self.client.force_authenticate(user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {item["uuid"] for item in response.data}


class ReviewerPoolVisibilityTest(ReviewRolesMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.accepted = factories.CallReviewerPoolFactory(call=self.call)
        self.invited_user = structure_factories.UserFactory(
            email="invited-user@example.com"
        )
        self.by_user = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=None,
            invited_user=self.invited_user,
            invited_email=self.invited_user.email,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.by_email = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=None,
            invited_email="Invited-Email@Example.com",
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.other_call = factories.CallReviewerPoolFactory()
        self.call_entries = {
            self.accepted.uuid.hex,
            self.by_user.uuid.hex,
            self.by_email.uuid.hex,
        }
        self.url = factories.CallReviewerPoolFactory.get_list_url()

    def listed(self, user):
        return self.uuids(user, self.url)

    def retrieve_status(self, user, entry):
        self.client.force_authenticate(user)
        return self.client.get(
            factories.CallReviewerPoolFactory.get_url(entry)
        ).status_code

    def assert_sees_call_pool(self, user):
        self.assertEqual(self.listed(user), self.call_entries)
        self.assertEqual(self.retrieve_status(user, self.accepted), status.HTTP_200_OK)

    def assert_sees_nothing(self, user):
        self.assertEqual(self.listed(user), set())
        self.assertEqual(
            self.retrieve_status(user, self.accepted), status.HTTP_404_NOT_FOUND
        )

    # Those who manage the call's review

    def test_call_manager_sees_the_call_pool(self):
        self.assert_sees_call_pool(self.call_manager())

    def test_call_organiser_sees_the_call_pool(self):
        self.assert_sees_call_pool(self.call_organiser())

    def test_owner_who_organises_the_calls_sees_the_call_pool(self):
        user = self.owner()
        self.organisation.add_user(user, self.organizer_role)
        self.assert_sees_call_pool(user)

    # Every entry

    def test_staff_sees_every_entry(self):
        user = structure_factories.UserFactory(is_staff=True)
        self.assertEqual(
            self.listed(user), self.call_entries | {self.other_call.uuid.hex}
        )

    def test_support_sees_every_entry(self):
        user = structure_factories.UserFactory(is_support=True)
        self.assertEqual(
            self.listed(user), self.call_entries | {self.other_call.uuid.hex}
        )

    def test_site_wide_call_list_grant_sees_every_entry(self):
        user = structure_factories.UserFactory()
        role = GlobalRole.get("GLOBAL.POOL_CALL_READER")
        role.add_permission(PermissionEnum.LIST_CALLS)
        grant_global_role(user, role)
        self.assertEqual(
            self.listed(user), self.call_entries | {self.other_call.uuid.hex}
        )

    # The addressee of an entry sees that entry only

    def test_invited_user_sees_their_own_entry(self):
        self.assertEqual(self.listed(self.invited_user), {self.by_user.uuid.hex})
        self.assertEqual(
            self.retrieve_status(self.invited_user, self.by_user), status.HTTP_200_OK
        )
        self.assertEqual(
            self.retrieve_status(self.invited_user, self.accepted),
            status.HTTP_404_NOT_FOUND,
        )

    def test_holder_of_the_invited_email_sees_their_own_entry(self):
        user = structure_factories.UserFactory(email="invited-email@example.com")
        self.assertEqual(self.listed(user), {self.by_email.uuid.hex})

    def test_reviewer_whose_profile_is_linked_sees_their_own_entry(self):
        self.assertEqual(
            self.listed(self.accepted.reviewer.user), {self.accepted.uuid.hex}
        )

    def test_another_reviewer_of_the_call_sees_only_their_own_entry(self):
        user = self.user_with(self.call, CallRole.REVIEWER)
        own = factories.CallReviewerPoolFactory(
            call=self.call, reviewer=factories.ReviewerProfileFactory(user=user)
        )
        # Granting the reviewer role invites the user too: both entries are
        # theirs, and no one else's is listed.
        own_entries = {
            entry.uuid.hex
            for entry in models.CallReviewerPool.objects.filter(
                models.CallReviewerPool.addressed_to(user)
            )
        }
        self.assertIn(own.uuid.hex, own_entries)
        self.assertEqual(self.listed(user), own_entries)
        self.assertFalse(own_entries & self.call_entries)

    # Other roles on the managing organisation's customer see nothing

    def test_organisation_reader_sees_nothing(self):
        self.assert_sees_nothing(self.reader())

    def test_results_reader_of_the_call_sees_nothing(self):
        results_reader = RoleFactory(
            content_type=ContentType.objects.get_for_model(models.Call)
        )
        results_reader.add_permission(PermissionEnum.VIEW_CALL_RESULTS)
        results_reader.add_permission(PermissionEnum.LIST_CALLS)
        self.assert_sees_nothing(self.user_with(self.call, results_reader))

    def test_customer_support_sees_nothing(self):
        self.assert_sees_nothing(self.user_with(self.customer, CustomerRole.SUPPORT))

    def test_owner_without_the_organiser_role_sees_nothing(self):
        # The owner holds the call team permissions, not PROPOSAL.MANAGE_REVIEW.
        self.assert_sees_nothing(self.owner())

    def test_project_member_sees_nothing(self):
        project = structure_factories.ProjectFactory(customer=self.customer)
        self.assert_sees_nothing(self.user_with(project, ProjectRole.ADMIN))

    def test_manager_of_another_call_of_the_organisation_sees_nothing(self):
        other_call = factories.CallFactory(manager=self.organisation)
        self.assert_sees_nothing(self.user_with(other_call, CallRole.MANAGER))

    def test_organiser_of_another_organisation_sees_nothing(self):
        other = factories.CallManagingOrganisationFactory()
        self.assert_sees_nothing(self.user_with(other, self.organizer_role))


class ReviewDataAboutPoolReviewersTest(ReviewRolesMixin, test.APITestCase):
    """The endpoints that name the call's reviewers -- their conflicts,
    disclosures, bids and the suggested candidates -- follow the pool's rule."""

    def setUp(self):
        super().setUp()
        proposal = factories.ProposalFactory(
            round=factories.RoundFactory(call=self.call)
        )
        reviewer = factories.CallReviewerPoolFactory(call=self.call).reviewer
        self.conflict = factories.ConflictOfInterestFactory(
            proposal=proposal, reviewer=reviewer
        )
        self.disclosure = factories.COIDisclosureFormFactory(
            call=self.call, reviewer=reviewer
        )
        self.bid = models.ReviewerBid.objects.create(
            call=self.call, reviewer=reviewer, proposal=proposal, bid="eager"
        )
        self.suggestion = models.ReviewerSuggestion.objects.create(
            call=self.call,
            reviewer=factories.ReviewerProfileFactory(),
            affinity_score=0.8,
        )
        self.lists = {
            reverse("conflict-of-interest-list"): self.conflict,
            reverse("coi-disclosure-list"): self.disclosure,
            reverse("reviewer-bid-list"): self.bid,
            reverse("reviewer-suggestion-list"): self.suggestion,
        }

    def assert_sees(self, user, expected):
        for url, item in self.lists.items():
            with self.subTest(url=url):
                visible = item.uuid.hex in self.uuids(user, url)
                self.assertEqual(visible, expected)

    def test_call_manager_sees_them(self):
        self.assert_sees(self.call_manager(), True)

    def test_call_organiser_sees_them(self):
        self.assert_sees(self.call_organiser(), True)

    def test_organisation_reader_does_not(self):
        self.assert_sees(self.reader(), False)

    def test_owner_without_the_organiser_role_does_not(self):
        self.assert_sees(self.owner(), False)

    def test_customer_support_role_holder_does_not(self):
        self.assert_sees(self.user_with(self.customer, CustomerRole.SUPPORT), False)

    def suggestion_action(self, user, action):
        self.client.force_authenticate(user)
        return self.client.post(
            reverse(
                "reviewer-suggestion-detail", kwargs={"uuid": self.suggestion.uuid.hex}
            )
            + action
            + "/",
            {"reason": "Not a fit"},
        )

    def test_call_manager_confirms_a_suggestion(self):
        response = self.suggestion_action(self.call_manager(), "confirm")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, ReviewerSuggestionStatuses.CONFIRMED)

    def test_call_organiser_rejects_a_suggestion(self):
        response = self.suggestion_action(self.call_organiser(), "reject")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, ReviewerSuggestionStatuses.REJECTED)

    def test_owner_without_the_organiser_role_cannot_decide_a_suggestion(self):
        for action in ("confirm", "reject"):
            with self.subTest(action=action):
                response = self.suggestion_action(self.owner(), action)
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, ReviewerSuggestionStatuses.PENDING)

    def test_support_reads_suggestions_but_cannot_decide_them(self):
        user = structure_factories.UserFactory(is_support=True)
        self.assertIn(
            self.suggestion.uuid.hex,
            self.uuids(user, reverse("reviewer-suggestion-list")),
        )
        for action in ("confirm", "reject"):
            with self.subTest(action=action):
                response = self.suggestion_action(user, action)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, ReviewerSuggestionStatuses.PENDING)
