"""
Tests for Call eligibility restrictions (Option B: AAI integration).

Tests the ability to restrict proposal submissions based on user attributes
such as nationality, organization type, and assurance levels.
"""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.tests import factories, fixtures


class CallEligibilityAPITest(test.APITestCase):
    """Test the check_eligibility endpoint on calls."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.user = structure_factories.UserFactory()

    def test_call_without_restrictions_accepts_all_users(self):
        """Calls without eligibility restrictions accept any user."""
        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_eligible"])
        self.assertEqual(response.data["restrictions"], [])

    def test_nationality_restriction_blocks_ineligible_user(self):
        """User without matching nationality cannot submit proposal."""
        self.user.nationality = "DE"
        self.user.save()

        self.call.user_nationalities = ["FR", "IT"]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_eligible"])
        self.assertTrue(len(response.data["restrictions"]) > 0)

    def test_nationality_restriction_allows_eligible_user(self):
        """User with matching nationality can submit proposal."""
        self.user.nationality = "DE"
        self.user.save()

        self.call.user_nationalities = ["DE", "FR"]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_eligible"])

    def test_nationalities_list_allows_eligible_user(self):
        """User with matching nationality in nationalities list can submit."""
        self.user.nationalities = ["DE", "US"]
        self.user.save()

        self.call.user_nationalities = ["DE"]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_eligible"])

    def test_organization_type_restriction_works(self):
        """Only users from allowed organization types can submit."""
        self.user.organization_type = "urn:schac:homeOrganizationType:int:university"
        self.user.save()

        self.call.user_organization_types = [
            "urn:schac:homeOrganizationType:int:university"
        ]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_eligible"])

    def test_organization_type_restriction_blocks_ineligible_user(self):
        """User from non-allowed organization type cannot submit."""
        self.user.organization_type = "urn:schac:homeOrganizationType:int:company"
        self.user.save()

        self.call.user_organization_types = [
            "urn:schac:homeOrganizationType:int:university"
        ]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_eligible"])

    def test_assurance_level_requires_all(self):
        """User must have ALL specified assurance levels (AND logic)."""
        self.user.eduperson_assurance = ["https://refeds.org/assurance/IAP/high"]
        self.user.save()

        self.call.user_assurance_levels = [
            "https://refeds.org/assurance/IAP/high",
            "https://refeds.org/assurance/ID/eppn-unique-no-reassign",
        ]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_eligible"])

    def test_assurance_level_allows_user_with_all_required(self):
        """User with all required assurance levels can submit."""
        self.user.eduperson_assurance = [
            "https://refeds.org/assurance/IAP/high",
            "https://refeds.org/assurance/ID/eppn-unique-no-reassign",
        ]
        self.user.save()

        self.call.user_assurance_levels = [
            "https://refeds.org/assurance/IAP/high",
        ]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_eligible"])

    def test_combined_restrictions(self):
        """Multiple restriction types work together correctly."""
        self.user.nationality = "DE"
        self.user.organization_type = "urn:schac:homeOrganizationType:int:university"
        self.user.save()

        self.call.user_nationalities = ["DE"]
        self.call.user_organization_types = [
            "urn:schac:homeOrganizationType:int:university"
        ]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_eligible"])

    def test_unauthenticated_user_cannot_check_eligibility(self):
        """Unauthenticated users cannot check eligibility."""
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_email_pattern_restriction_allows_matching_user(self):
        """User with matching email pattern can submit."""
        self.user.email = "researcher@university.edu"
        self.user.save()

        self.call.user_email_patterns = [r".*@university\.edu$"]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_eligible"])

    def test_affiliation_restriction_allows_matching_user(self):
        """User with matching affiliation can submit."""
        self.user.affiliations = ["member", "faculty"]
        self.user.save()

        self.call.user_affiliations = ["faculty"]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.CallFactory.get_public_url(self.call) + "check_eligibility/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_eligible"])


class CallEligibilitySerializerTest(test.APITestCase):
    """Test that eligibility fields are properly serialized."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call

    def test_has_eligibility_restrictions_false_when_no_restrictions(self):
        """has_eligibility_restrictions is False when no restrictions configured."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CallFactory.get_public_url(self.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["has_eligibility_restrictions"])

    def test_has_eligibility_restrictions_true_when_nationality_set(self):
        """has_eligibility_restrictions is True when nationality restrictions set."""
        self.call.user_nationalities = ["DE"]
        self.call.save()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.CallFactory.get_public_url(self.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["has_eligibility_restrictions"])

    def test_eligibility_fields_in_protected_call_serializer(self):
        """Protected call serializer includes eligibility configuration fields."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("user_nationalities", response.data)
        self.assertIn("user_organization_types", response.data)
        self.assertIn("user_assurance_levels", response.data)

    def test_call_manager_can_set_eligibility_restrictions(self):
        """Call manager can update eligibility restrictions."""
        self.client.force_authenticate(self.fixture.call_manager)
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.patch(
            url,
            {
                "user_nationalities": ["DE", "FR"],
                "user_organization_types": [
                    "urn:schac:homeOrganizationType:int:university"
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.call.refresh_from_db()
        self.assertEqual(self.call.user_nationalities, ["DE", "FR"])


class ProposalCreationEligibilityTest(test.APITestCase):
    """Test that proposal creation validates eligibility."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.round = self.fixture.round
        self.user = structure_factories.UserFactory()

    def test_proposal_creation_validates_eligibility(self):
        """Proposal creation fails if user doesn't meet call requirements."""
        self.user.nationality = "DE"
        self.user.save()

        self.call.user_nationalities = ["FR"]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.ProposalFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "round_uuid": self.round.uuid.hex,
                "name": "Test Proposal",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_proposal_creation_succeeds_for_eligible_user(self):
        """Proposal creation succeeds for eligible user."""
        self.user.nationality = "DE"
        self.user.save()

        self.call.user_nationalities = ["DE"]
        self.call.save()

        self.client.force_authenticate(self.user)
        url = factories.ProposalFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "round_uuid": self.round.uuid.hex,
                "name": "Test Proposal",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_proposal_creation_succeeds_without_restrictions(self):
        """Proposal creation succeeds when no eligibility restrictions configured."""
        self.client.force_authenticate(self.user)
        url = factories.ProposalFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "round_uuid": self.round.uuid.hex,
                "name": "Test Proposal",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
