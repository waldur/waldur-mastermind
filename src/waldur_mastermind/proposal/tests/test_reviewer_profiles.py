"""Tests for reviewer profile functionality."""

from django.test import TestCase
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    ExpertiseProficiencyLevels,
    ReviewerAffiliationTypes,
    ReviewerPoolInvitationStatuses,
)
from waldur_mastermind.proposal.tests import factories


class ReviewerProfileModelTest(TestCase):
    """Test ReviewerProfile model."""

    def test_create_reviewer_profile(self):
        """Can create a reviewer profile linked to a user."""
        user = structure_factories.UserFactory()
        profile = models.ReviewerProfile.objects.create(
            user=user,
            orcid_id="0000-0001-2345-6789",
            biography="Test biography",
        )

        self.assertEqual(profile.user, user)
        self.assertEqual(profile.orcid_id, "0000-0001-2345-6789")
        self.assertIsNotNone(profile.uuid)

    def test_one_profile_per_user(self):
        """Each user can only have one reviewer profile."""
        user = structure_factories.UserFactory()
        models.ReviewerProfile.objects.create(user=user)

        with self.assertRaises(Exception):
            models.ReviewerProfile.objects.create(user=user)

    def test_alternative_names_stored_as_list(self):
        """Alternative names are stored as a JSON list."""
        profile = factories.ReviewerProfileFactory(
            alternative_names=["John Smith", "J. Smith", "John A. Smith"]
        )

        profile.refresh_from_db()
        self.assertEqual(len(profile.alternative_names), 3)
        self.assertIn("John Smith", profile.alternative_names)


class ReviewerAffiliationModelTest(TestCase):
    """Test ReviewerAffiliation model."""

    def test_create_affiliation(self):
        """Can create an affiliation linked to a profile."""
        profile = factories.ReviewerProfileFactory()
        org = structure_factories.CustomerFactory()

        affiliation = models.ReviewerAffiliation.objects.create(
            reviewer_profile=profile,
            organization=org,
            organization_name=org.name,
            position_title="Professor",
            start_date="2020-01-01",
            affiliation_type=ReviewerAffiliationTypes.EMPLOYMENT,
        )

        self.assertEqual(affiliation.reviewer_profile, profile)
        self.assertEqual(affiliation.organization, org)

    def test_current_affiliation_has_no_end_date(self):
        """Current affiliations have null end_date."""
        affiliation = factories.ReviewerAffiliationFactory(end_date=None)
        self.assertIsNone(affiliation.end_date)

    def test_affiliation_types(self):
        """All affiliation types can be used."""
        profile = factories.ReviewerProfileFactory()

        for aff_type, _ in ReviewerAffiliationTypes.CHOICES:
            affiliation = factories.ReviewerAffiliationFactory(
                reviewer_profile=profile,
                affiliation_type=aff_type,
            )
            self.assertEqual(affiliation.affiliation_type, aff_type)


class ReviewerExpertiseModelTest(TestCase):
    """Test ReviewerExpertise model."""

    def test_create_expertise(self):
        """Can create expertise entries."""
        profile = factories.ReviewerProfileFactory()

        expertise = models.ReviewerExpertise.objects.create(
            reviewer_profile=profile,
            expertise_keyword="machine learning",
            proficiency_level=ExpertiseProficiencyLevels.EXPERT,
            years_experience=10,
        )

        self.assertEqual(expertise.expertise_keyword, "machine learning")
        self.assertEqual(expertise.proficiency_level, ExpertiseProficiencyLevels.EXPERT)

    def test_proficiency_levels(self):
        """All proficiency levels can be used."""
        profile = factories.ReviewerProfileFactory()

        for level, _ in ExpertiseProficiencyLevels.CHOICES:
            expertise = factories.ReviewerExpertiseFactory(
                reviewer_profile=profile,
                proficiency_level=level,
            )
            self.assertEqual(expertise.proficiency_level, level)

    def test_expertise_with_category(self):
        """Expertise can be linked to a category."""
        category = factories.ExpertiseCategoryFactory()
        expertise = factories.ReviewerExpertiseFactory(expertise_category=category)

        self.assertEqual(expertise.expertise_category, category)


class ReviewerPublicationModelTest(TestCase):
    """Test ReviewerPublication model."""

    def test_create_publication(self):
        """Can create publication entries."""
        profile = factories.ReviewerProfileFactory()

        publication = models.ReviewerPublication.objects.create(
            reviewer_profile=profile,
            title="A Novel Approach to Machine Learning",
            doi="10.1234/test.123456",
            publication_year=2023,
            venue="Journal of AI Research",
            venue_type="journal",
        )

        self.assertEqual(publication.title, "A Novel Approach to Machine Learning")
        self.assertEqual(publication.doi, "10.1234/test.123456")

    def test_coauthors_stored_as_json(self):
        """Coauthors are stored as JSON list."""
        publication = factories.ReviewerPublicationFactory(
            coauthors=[
                {"name": "Alice Smith", "orcid": "0000-0001-1111-1111"},
                {"name": "Bob Jones", "orcid": None},
            ]
        )

        publication.refresh_from_db()
        self.assertEqual(len(publication.coauthors), 2)
        self.assertEqual(publication.coauthors[0]["name"], "Alice Smith")


class ExpertiseCategoryModelTest(TestCase):
    """Test hierarchical ExpertiseCategory model."""

    def test_create_root_category(self):
        """Can create a root category."""
        category = models.ExpertiseCategory.objects.create(
            name="Computer Science",
            code="CS",
            level=0,
        )

        self.assertIsNone(category.parent)
        self.assertEqual(category.level, 0)

    def test_create_child_category(self):
        """Can create child categories."""
        parent = factories.ExpertiseCategoryFactory(level=0)
        child = models.ExpertiseCategory.objects.create(
            name="Machine Learning",
            code="CS-ML",
            parent=parent,
            level=1,
        )

        self.assertEqual(child.parent, parent)
        self.assertEqual(child.level, 1)


class ReviewerStatsModelTest(TestCase):
    """Test ReviewerStats model."""

    def test_create_stats(self):
        """Can create stats for a reviewer."""
        profile = factories.ReviewerProfileFactory()

        stats = models.ReviewerStats.objects.create(
            reviewer_profile=profile,
            total_reviews_completed=10,
            total_reviews_declined=2,
            average_review_time_days=14.5,
        )

        self.assertEqual(stats.total_reviews_completed, 10)
        self.assertEqual(stats.average_review_time_days, 14.5)


class CallReviewerPoolModelTest(TestCase):
    """Test CallReviewerPool model."""

    def test_create_pool_member(self):
        """Can add a reviewer to a call's pool."""
        call = factories.CallFactory()
        reviewer = factories.ReviewerProfileFactory()

        pool_member = models.CallReviewerPool.objects.create(
            call=call,
            reviewer=reviewer,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
            max_assignments=5,
        )

        self.assertEqual(pool_member.call, call)
        self.assertEqual(pool_member.reviewer, reviewer)
        self.assertIsNotNone(pool_member.invitation_token)

    def test_invitation_token_generated(self):
        """Invitation token is auto-generated."""
        pool_member = factories.CallReviewerPoolFactory()
        self.assertIsNotNone(pool_member.invitation_token)
        self.assertGreater(len(pool_member.invitation_token), 20)

    def test_invitation_statuses(self):
        """All invitation statuses can be used."""
        for status_val, _ in ReviewerPoolInvitationStatuses.CHOICES:
            pool_member = factories.CallReviewerPoolFactory(
                invitation_status=status_val,
            )
            self.assertEqual(pool_member.invitation_status, status_val)


class ReviewerProfileAPITest(test.APITestCase):
    """Test reviewer profile API endpoints."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)

    def test_create_own_profile(self):
        """User can create their own profile."""
        self.client.force_authenticate(self.user)

        url = factories.ReviewerProfileFactory.get_list_url(action="me")
        response = self.client.post(
            url,
            {
                "orcid_id": "0000-0001-2345-6789",
                "biography": "I am a researcher.",
            },
        )

        self.assertIn(
            response.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED]
        )
        self.assertTrue(models.ReviewerProfile.objects.filter(user=self.user).exists())

    def test_get_own_profile(self):
        """User can retrieve their own profile."""
        profile = factories.ReviewerProfileFactory(user=self.user)
        self.client.force_authenticate(self.user)

        url = factories.ReviewerProfileFactory.get_list_url(action="me")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], profile.uuid.hex)

    def test_staff_can_list_profiles(self):
        """Staff can list all reviewer profiles."""
        factories.ReviewerProfileFactory()
        factories.ReviewerProfileFactory()

        self.client.force_authenticate(self.staff)

        url = factories.ReviewerProfileFactory.get_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 2)


class ReviewerPoolAPITest(test.APITestCase):
    """Test reviewer pool API endpoints."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call = factories.CallFactory()
        self.call.add_user(self.staff, CallRole.MANAGER)

    def test_pool_member_exists(self):
        """Pool members can be created via factory."""
        pool_member = factories.CallReviewerPoolFactory(call=self.call)
        self.assertEqual(pool_member.call, self.call)
        self.assertIsNotNone(pool_member.reviewer)


class ReviewerInvitationAPITest(test.APITestCase):
    """Test reviewer invitation token-based endpoints."""

    def test_accept_invitation_with_valid_token(self):
        """Reviewer can accept invitation with valid token."""
        pool_member = factories.CallReviewerPoolFactory(
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        token = pool_member.invitation_token

        url = f"http://testserver/api/reviewer-invitations/{token}/accept/"
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pool_member.refresh_from_db()
        self.assertEqual(
            pool_member.invitation_status,
            ReviewerPoolInvitationStatuses.ACCEPTED,
        )

    def test_decline_invitation_with_valid_token(self):
        """Reviewer can decline invitation with valid token."""
        pool_member = factories.CallReviewerPoolFactory(
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        token = pool_member.invitation_token

        url = f"http://testserver/api/reviewer-invitations/{token}/decline/"
        response = self.client.post(url, {"reason": "Too busy"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pool_member.refresh_from_db()
        self.assertEqual(
            pool_member.invitation_status,
            ReviewerPoolInvitationStatuses.DECLINED,
        )

    def test_invalid_token_returns_404(self):
        """Invalid token returns 404."""
        url = "http://testserver/api/reviewer-invitations/invalid-token/accept/"
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class COIDisclosureModelTest(TestCase):
    """Test COI disclosure form model."""

    def test_create_disclosure(self):
        """Can create a COI disclosure form via factory."""
        disclosure = factories.COIDisclosureFormFactory()

        self.assertIsNotNone(disclosure.uuid)
        self.assertIsNotNone(disclosure.reviewer)

    def test_disclosure_factory(self):
        """COI disclosure can be created via factory."""
        disclosure = factories.COIDisclosureFormFactory()

        self.assertIsNotNone(disclosure.uuid)
        self.assertIsNotNone(disclosure.reviewer)

    def test_disclosure_with_call(self):
        """Disclosure can be linked to a call."""
        call = factories.CallFactory()
        disclosure = factories.COIDisclosureFormFactory(call=call)

        self.assertEqual(disclosure.call, call)


class ConflictOfInterestAPITest(test.APITestCase):
    """Test COI management API endpoints."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call = factories.CallFactory()
        self.call.add_user(self.staff, CallRole.MANAGER)
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)
        self.reviewer = factories.ReviewerProfileFactory()

    def test_manager_can_view_conflicts(self):
        """Call manager can view conflicts for their call."""
        factories.ConflictOfInterestFactory(
            reviewer=self.reviewer,
            proposal=self.proposal,
            call=self.call,
        )

        self.client.force_authenticate(self.staff)

        url = factories.CallFactory.get_protected_url(self.call, action="conflicts")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_manager_can_filter_conflicts_by_reviewer_name(self):
        """Conflicts can be filtered by the reviewer's name."""
        alice = structure_factories.UserFactory(
            first_name="Alice", last_name="Reviewer"
        )
        conflict = factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=alice),
            proposal=self.proposal,
            call=self.call,
        )
        factories.ConflictOfInterestFactory(
            reviewer=self.reviewer,
            proposal=self.proposal,
            call=self.call,
        )

        self.client.force_authenticate(self.staff)

        response = self.client.get(
            "http://testserver/api/conflicts-of-interest/",
            {"reviewer_name": "alice"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], conflict.uuid.hex)

    def test_manager_can_dismiss_conflict(self):
        """Call manager can dismiss a conflict."""
        conflict = factories.ConflictOfInterestFactory(
            reviewer=self.reviewer,
            proposal=self.proposal,
            call=self.call,
        )

        self.client.force_authenticate(self.staff)

        url = (
            f"http://testserver/api/conflicts-of-interest/{conflict.uuid.hex}/dismiss/"
        )
        response = self.client.post(
            url, {"status": "dismissed", "review_notes": "Not a real conflict"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        conflict.refresh_from_db()
        self.assertEqual(conflict.status, "dismissed")

    def test_manager_can_waive_conflict(self):
        """Call manager can waive a conflict with management plan."""
        conflict = factories.ConflictOfInterestFactory(
            reviewer=self.reviewer,
            proposal=self.proposal,
            call=self.call,
        )

        self.client.force_authenticate(self.staff)

        url = f"http://testserver/api/conflicts-of-interest/{conflict.uuid.hex}/waive/"
        response = self.client.post(
            url,
            {
                "status": "waived",
                "review_notes": "Minor conflict",
                "management_plan": "Reviewer will be supervised during review",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        conflict.refresh_from_db()
        self.assertEqual(conflict.status, "waived")
        self.assertIsNotNone(conflict.management_plan)
