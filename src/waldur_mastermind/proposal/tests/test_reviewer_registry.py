"""The operator's reviewer registry (#758): a reviewer's standing, enforced
wherever reviewers are selected or take on work; publication approval; the
call's own reviewer eligibility; and the reviewer settings."""

from django.test import override_settings
from rest_framework import status, test
from rest_framework.exceptions import ValidationError

from waldur_core.configuration.testing import override_config
from waldur_core.core.enums import ReviewStates
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.utils import RoleChange, validate_role_change
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    affinity_scoring,
    models,
    proposal_roles,
    reviewer_pool,
    reviewer_registry,
)
from waldur_mastermind.proposal.enums import (
    ProposalRoleEligibility,
    ProposalStates,
    ReviewerPoolInvitationStatuses,
    ReviewerStandings,
)

from . import factories

REGISTRY_URL = "/api/reviewer-registry/"


def suspend(profile, by=None):
    return reviewer_registry.set_standing(
        profile, ReviewerStandings.SUSPENDED, "Under investigation", by
    )


class CallSetupMixin:
    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call = factories.CallFactory()
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        self.call.manager.add_user(self.manager, CallRole.MANAGER)
        structure_factories.NotificationFactory(key="proposal.reviewer_invitation")
        self.profile = factories.ReviewerProfileFactory(is_published=True)
        self.reviewer = self.profile.user


class StandingQuerysetTest(test.APITestCase):
    def test_only_active_reviewers_with_active_accounts_are_eligible(self):
        active = factories.ReviewerProfileFactory()
        suspended = factories.ReviewerProfileFactory(
            standing=ReviewerStandings.SUSPENDED
        )
        blocked = factories.ReviewerProfileFactory(standing=ReviewerStandings.BLOCKED)
        deactivated = factories.ReviewerProfileFactory()
        deactivated.user.is_active = False
        deactivated.user.save()

        eligible = set(models.ReviewerProfile.objects.eligible())
        self.assertIn(active, eligible)
        for profile in (suspended, blocked, deactivated):
            self.assertNotIn(profile, eligible)

        call = factories.CallFactory()
        pools = {
            profile: factories.CallReviewerPoolFactory(call=call, reviewer=profile)
            for profile in (active, suspended, deactivated)
        }
        self.assertEqual(
            list(models.CallReviewerPool.objects.eligible().filter(call=call)),
            [pools[active]],
        )

    def test_a_new_profile_is_active(self):
        self.assertEqual(
            factories.ReviewerProfileFactory().standing, ReviewerStandings.ACTIVE
        )


@override_settings(task_always_eager=True)
class SelectionExcludesTest(CallSetupMixin, test.APITestCase):
    """Every path picking reviewers leaves a non-active one out."""

    def test_call_suggestions(self):
        suspended = factories.ReviewerProfileFactory(is_published=True)
        suspend(suspended)
        factories.ProposalFactory(round=factories.RoundFactory(call=self.call))
        affinity_scoring.compute_suggestions_for_call_configurable(
            self.call, min_threshold=0
        )
        suggested = set(
            models.ReviewerSuggestion.objects.filter(call=self.call).values_list(
                "reviewer_id", flat=True
            )
        )
        self.assertNotIn(suspended.id, suggested)

    def test_affinity_computation(self):
        factories.CallReviewerPoolFactory(call=self.call, reviewer=self.profile)
        factories.ProposalFactory(round=factories.RoundFactory(call=self.call))
        suspend(self.profile)
        affinity_scoring.compute_affinities_for_call(self.call)
        self.assertFalse(
            models.ReviewerProposalAffinity.objects.filter(
                reviewer=self.profile
            ).exists()
        )

    def test_invite_by_profile(self):
        suspend(self.profile)
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.CallFactory.get_protected_url(self.call, action="reviewer-pool"),
            {"reviewer_uuids": [self.profile.uuid.hex]},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not in good standing", str(response.data))
        self.assertFalse(
            models.CallReviewerPool.objects.filter(call=self.call).exists()
        )

    def test_invite_by_email(self):
        suspend(self.profile)
        with self.assertRaises(ValidationError):
            reviewer_pool.invite_by_email(self.call, self.reviewer.email, self.manager)

    def test_assignment_generation_and_manual_assignment(self):
        entry = factories.CallReviewerPoolFactory(call=self.call, reviewer=self.profile)
        suspend(self.profile)
        self.assertNotIn(
            entry, models.CallReviewerPool.objects.eligible().filter(call=self.call)
        )


class TakingOnWorkTest(CallSetupMixin, test.APITestCase):
    """Pending invitations and assignments can no longer be accepted; work
    already accepted stays."""

    def test_accepting_a_pending_invitation(self):
        entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.profile,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        suspend(self.profile)
        self.client.force_authenticate(self.reviewer)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(entry, "accept"),
            [],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        entry.refresh_from_db()
        self.assertEqual(
            entry.invitation_status, ReviewerPoolInvitationStatuses.PENDING
        )

    def test_force_accept_is_refused_and_otherwise_logged(self):
        entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.profile,
            invitation_status=ReviewerPoolInvitationStatuses.DECLINED,
        )
        url = factories.CallReviewerPoolFactory.get_url(entry, "force-accept")
        self.client.force_authenticate(self.manager)
        suspend(self.profile)
        response = self.client.post(url, {"override_reason": "Needed"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        reviewer_registry.set_standing(
            self.profile, ReviewerStandings.ACTIVE, "", self.staff
        )
        response = self.client.post(url, {"override_reason": "Needed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.REVIEWER_INVITATION_FORCE_ACCEPTED
            ).exists()
        )

    def _item(self):
        entry = factories.CallReviewerPoolFactory(call=self.call, reviewer=self.profile)
        proposal = factories.ProposalFactory(
            round=factories.RoundFactory(call=self.call),
            state=ProposalStates.SUBMITTED,
        )
        batch = factories.AssignmentBatchFactory(
            call=self.call, reviewer_pool_entry=entry
        )
        return factories.AssignmentItemFactory(batch=batch, proposal=proposal)

    def test_accepting_an_assignment(self):
        item = self._item()
        suspend(self.profile)
        with self.assertRaises(ValidationError):
            item.accept(self.reviewer)
        self.assertFalse(models.Review.objects.filter(reviewer=self.reviewer).exists())

    @override_config(COI_DISCLOSURE_REQUIRED=True)
    def test_accepting_an_assignment_needs_a_disclosure_when_required(self):
        item = self._item()
        with self.assertRaises(ValidationError):
            item.accept(self.reviewer)
        factories.COIDisclosureFormFactory(
            reviewer=self.profile, call=self.call, certified=True
        )
        self.assertIsNotNone(item.accept(self.reviewer))

    def test_accepted_work_stays(self):
        review = self._item().accept(self.reviewer)
        suspend(self.profile)
        review.refresh_from_db()
        self.assertEqual(review.state, models.Review.States.IN_REVIEW)
        self.assertTrue(
            models.ReviewerProfile.objects.filter(pk=self.profile.pk).exists()
        )


class RoleGrantTest(CallSetupMixin, test.APITestCase):
    def test_the_call_reviewer_role(self):
        suspend(self.profile)
        with self.assertRaises(ValidationError):
            validate_role_change(
                self.call,
                CallRole.REVIEWER,
                self.staff,
                RoleChange.GRANT,
                self.reviewer,
            )
        # Revoking stays possible.
        validate_role_change(
            self.call, CallRole.REVIEWER, self.staff, RoleChange.REVOKE, self.reviewer
        )

    def test_an_evaluator_role(self):
        role = models.CallProposalRole.objects.create(
            call=self.call,
            key="rapporteur",
            name="Rapporteur",
            eligible_from=ProposalRoleEligibility.ANY_USER,
            max_holders=3,
        )
        proposal = factories.ProposalFactory(
            round=factories.RoundFactory(call=self.call),
            state=ProposalStates.SUBMITTED,
        )
        suspend(self.profile)
        with self.assertRaises(ValidationError):
            proposal_roles.assign(proposal, role, self.reviewer, assigned_by=self.staff)
        # Someone without a reviewer profile has no standing to lose.
        other = structure_factories.UserFactory()
        self.assertIsNotNone(
            proposal_roles.assign(proposal, role, other, assigned_by=self.staff)
        )


@override_settings(task_always_eager=True)
class ReviewerEligibilityTest(CallSetupMixin, test.APITestCase):
    def test_the_call_reviewer_rules_apply_and_the_applicant_rules_do_not(self):
        # Applicants are restricted; reviewers are not -- the reviewer passes.
        self.call.user_email_patterns = [r".*@applicants\.example$"]
        self.call.save()
        self.client.force_authenticate(self.manager)
        url = factories.CallFactory.get_protected_url(self.call, action="reviewer-pool")
        response = self.client.post(url, {"reviewer_uuids": [self.profile.uuid.hex]})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        other = factories.ReviewerProfileFactory(is_published=True)
        models.CallReviewerEligibility.objects.create(
            call=self.call, user_email_patterns=[r".*@reviewers\.example$"]
        )
        response = self.client.post(url, {"reviewer_uuids": [other.uuid.hex]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("eligibility", str(response.data))

    def test_set_through_the_call(self):
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            factories.CallFactory.get_protected_url(self.call),
            {"reviewer_eligibility": {"user_affiliations": ["member@uni.example"]}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["reviewer_eligibility"]["user_affiliations"],
            ["member@uni.example"],
        )
        self.assertEqual(response.data["user_affiliations"], [])


class RegistryTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.profile = factories.ReviewerProfileFactory()
        self.url = f"{REGISTRY_URL}{self.profile.uuid.hex}/"

    def test_staff_only(self):
        support = structure_factories.UserFactory(is_support=True)
        manager = structure_factories.UserFactory()
        for user in (self.profile.user, support, manager):
            self.client.force_authenticate(user)
            self.assertEqual(
                self.client.get(REGISTRY_URL).status_code, status.HTTP_403_FORBIDDEN
            )
            response = self.client.post(
                self.url + "set-standing/",
                {"standing": "blocked", "reason": "x"},
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(REGISTRY_URL).status_code, status.HTTP_200_OK)

    def test_set_standing_records_who_when_and_why_and_logs_it(self):
        entry = factories.CallReviewerPoolFactory(reviewer=self.profile)
        coi = factories.ConflictOfInterestFactory(reviewer=self.profile)
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url + "set-standing/", {"standing": "blocked"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.post(
            self.url + "set-standing/",
            {"standing": "blocked", "reason": "Plagiarism in reviews"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["standing"], "blocked")
        self.assertEqual(response.data["standing_reason"], "Plagiarism in reviews")
        self.assertEqual(
            response.data["standing_changed_by_name"], self.staff.full_name
        )
        self.assertEqual(response.data["pools"][0]["call_uuid"], entry.call.uuid.hex)
        event = Event.objects.get(event_type=EventType.REVIEWER_STANDING_CHANGED)
        self.assertEqual(event.context["reason"], "Plagiarism in reviews")
        self.assertEqual(event.context["standing"], "blocked")
        # Nothing is deleted.
        self.assertTrue(models.CallReviewerPool.objects.filter(pk=entry.pk).exists())
        self.assertTrue(models.ConflictOfInterest.objects.filter(pk=coi.pk).exists())

        response = self.client.get(REGISTRY_URL, {"standing": "blocked"})
        self.assertEqual(
            [row["uuid"] for row in response.data], [self.profile.uuid.hex]
        )

    def test_a_suspended_reviewer_cannot_delete_their_profile(self):
        suspend(self.profile)
        self.client.force_authenticate(self.profile.user)
        response = self.client.delete(
            factories.ReviewerProfileFactory.get_url(self.profile)
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(self.staff)
        response = self.client.delete(
            factories.ReviewerProfileFactory.get_url(self.profile)
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


class PublicationApprovalTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.profile = factories.ReviewerProfileFactory()
        self.publish_url = factories.ReviewerProfileFactory.get_list_url("publish")
        self.me_url = factories.ReviewerProfileFactory.get_list_url("me")
        self.registry_url = f"{REGISTRY_URL}{self.profile.uuid.hex}/"

    def publish(self):
        self.client.force_authenticate(self.profile.user)
        return self.client.post(self.publish_url)

    def test_off_publishes_at_once(self):
        response = self.publish()
        self.assertTrue(response.data["is_published"])
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_published)

    @override_config(REVIEWER_PUBLICATION_REQUIRES_APPROVAL=True)
    def test_on_waits_for_approval(self):
        response = self.publish()
        self.assertFalse(response.data["is_published"])
        self.assertTrue(response.data["publication_pending"])
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_published)
        # Not discoverable while pending.
        self.assertFalse(
            models.ReviewerProfile.objects.eligible()
            .filter(is_published=True, pk=self.profile.pk)
            .exists()
        )
        me = self.client.get(self.me_url).data
        self.assertEqual(me["publication_request"]["state"], "pending")

        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.registry_url + "approve-publication/", {"comment": "Welcome"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["is_published"])
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.REVIEWER_PUBLICATION_APPROVED
            ).exists()
        )

    @override_config(REVIEWER_PUBLICATION_REQUIRES_APPROVAL=True)
    def test_rejection_is_explained_to_the_reviewer(self):
        self.publish()
        self.client.force_authenticate(self.staff)
        self.client.post(
            self.registry_url + "reject-publication/",
            {"comment": "Add your affiliation"},
        )
        self.client.force_authenticate(self.profile.user)
        me = self.client.get(self.me_url).data
        self.assertFalse(me["is_published"])
        self.assertEqual(me["publication_request"]["state"], "rejected")
        self.assertEqual(
            me["publication_request"]["review_comment"], "Add your affiliation"
        )

    def test_turning_it_off_strands_nobody(self):
        with override_config(REVIEWER_PUBLICATION_REQUIRES_APPROVAL=True):
            self.publish()
        request = models.ReviewerPublicationRequest.objects.get(profile=self.profile)
        # Staff can still resolve it ...
        self.client.force_authenticate(self.staff)
        self.assertEqual(
            self.client.get(REGISTRY_URL, {"publication_pending": True}).data[0][
                "uuid"
            ],
            self.profile.uuid.hex,
        )
        # ... and the reviewer's next publish goes through, overtaking it.
        response = self.publish()
        self.assertTrue(response.data["is_published"])
        request.refresh_from_db()
        self.assertEqual(request.state, ReviewStates.CANCELED)


class SettingsTest(CallSetupMixin, test.APITestCase):
    @override_config(REVIEWER_PROFILES_ENABLED=False)
    def test_reviewer_profiles_off(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.post(
            factories.ReviewerProfileFactory.get_list_url("me"), {"biography": "x"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(AUTOMATED_MATCHING_ENABLED=False)
    def test_automated_matching_off(self):
        self.client.force_authenticate(self.manager)
        for action in (
            "generate-suggestions",
            "compute-affinities",
            "generate-assignments",
        ):
            response = self.client.post(
                factories.CallFactory.get_protected_url(self.call, action=action)
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_400_BAD_REQUEST,
                (action, response.data),
            )

    @override_config(COI_DETECTION_ENABLED=False)
    def test_coi_detection_off(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="detect-conflicts"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
