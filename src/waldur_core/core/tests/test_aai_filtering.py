"""
Tests for AAI-based invitation/access filtering.
"""

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from waldur_core.core import models as core_models
from waldur_core.permissions.utils import validate_user_restrictions
from waldur_core.structure.tests import factories


class UserDetailsMatchMixinAAIFilteringTest(TestCase):
    """Test AAI-based filtering in UserDetailsMatchMixin."""

    def setUp(self):
        self.user = core_models.User.objects.create_user(
            username="testuser",
            email="test@example.com",
        )
        self.customer = factories.CustomerFactory()

    def test_nationality_filter_matches_primary_nationality(self):
        """Test that nationality filter matches user's primary nationality."""
        self.user.nationality = "EE"
        self.user.save()

        self.customer.user_nationalities = ["EE", "FI"]
        self.customer.save()

        # Should not raise - user nationality matches
        validate_user_restrictions(self.customer, self.user)

    def test_nationality_filter_matches_from_nationalities_list(self):
        """Test that nationality filter matches from user's nationalities list."""
        self.user.nationality = ""  # No primary nationality
        self.user.nationalities = ["EE", "SE"]
        self.user.save()

        self.customer.user_nationalities = ["FI", "EE"]  # EE matches
        self.customer.save()

        # Should not raise - user has EE in nationalities list
        validate_user_restrictions(self.customer, self.user)

    def test_nationality_filter_rejects_non_matching_nationality(self):
        """Test that nationality filter rejects users without matching nationality."""
        self.user.nationality = "US"
        self.user.nationalities = ["CA"]
        self.user.save()

        self.customer.user_nationalities = ["EE", "FI"]
        self.customer.save()

        # Should raise - user nationality doesn't match
        with self.assertRaises(ValidationError) as context:
            validate_user_restrictions(self.customer, self.user)
        self.assertIn("nationality", str(context.exception))

    def test_organization_type_filter_matches(self):
        """Test that organization type filter works correctly."""
        self.user.organization_type = "urn:schac:homeOrganizationType:int:university"
        self.user.save()

        self.customer.user_organization_types = [
            "urn:schac:homeOrganizationType:int:university",
            "urn:schac:homeOrganizationType:int:research",
        ]
        self.customer.save()

        # Should not raise - organization type matches
        validate_user_restrictions(self.customer, self.user)

    def test_organization_type_filter_rejects_non_matching(self):
        """Test that organization type filter rejects non-matching users."""
        self.user.organization_type = "urn:schac:homeOrganizationType:int:government"
        self.user.save()

        self.customer.user_organization_types = [
            "urn:schac:homeOrganizationType:int:university",
        ]
        self.customer.save()

        # Should raise - organization type doesn't match
        with self.assertRaises(ValidationError) as context:
            validate_user_restrictions(self.customer, self.user)
        self.assertIn("organization type", str(context.exception))

    def test_assurance_level_filter_requires_all(self):
        """Test that assurance level filter requires ALL specified levels (AND logic)."""
        self.user.eduperson_assurance = [
            "https://refeds.org/assurance/IAP/high",
            "https://refeds.org/assurance/ID/unique",
            "https://refeds.org/assurance/ATP/ePA",
        ]
        self.user.save()

        # Require two levels that user has
        self.customer.user_assurance_levels = [
            "https://refeds.org/assurance/IAP/high",
            "https://refeds.org/assurance/ID/unique",
        ]
        self.customer.save()

        # Should not raise - user has all required levels
        validate_user_restrictions(self.customer, self.user)

    def test_assurance_level_filter_rejects_missing_level(self):
        """Test that assurance level filter rejects users missing required levels."""
        self.user.eduperson_assurance = [
            "https://refeds.org/assurance/IAP/low",
        ]
        self.user.save()

        self.customer.user_assurance_levels = [
            "https://refeds.org/assurance/IAP/high",
        ]
        self.customer.save()

        # Should raise - user doesn't have required level
        with self.assertRaises(ValidationError) as context:
            validate_user_restrictions(self.customer, self.user)
        self.assertIn("assurance", str(context.exception))

    def test_combined_aai_filters(self):
        """Test that all AAI filters work together."""
        self.user.nationality = "EE"
        self.user.organization_type = "urn:schac:homeOrganizationType:int:university"
        self.user.eduperson_assurance = ["https://refeds.org/assurance/IAP/high"]
        self.user.save()

        self.customer.user_nationalities = ["EE"]
        self.customer.user_organization_types = [
            "urn:schac:homeOrganizationType:int:university"
        ]
        self.customer.user_assurance_levels = ["https://refeds.org/assurance/IAP/high"]
        self.customer.save()

        # Should not raise - all filters match
        validate_user_restrictions(self.customer, self.user)

    def test_no_extended_profile_filters_allows_all(self):
        """Test that no extended profile filters allows all users."""
        # User has no extended profile attributes set
        self.user.nationality = ""
        self.user.organization_type = ""
        self.user.eduperson_assurance = []
        self.user.save()

        # Customer has no AAI restrictions
        self.customer.user_nationalities = []
        self.customer.user_organization_types = []
        self.customer.user_assurance_levels = []
        self.customer.save()

        # Should not raise - no restrictions
        validate_user_restrictions(self.customer, self.user)

    def test_aai_filters_combined_with_basic_filters(self):
        """Test that AAI filters work with email/affiliation/identity_source filters."""
        self.user.email = "user@university.edu"
        self.user.affiliations = ["member@university.edu"]
        self.user.nationality = "EE"
        self.user.save()

        # Set both basic and AAI filters
        self.customer.user_email_patterns = [".*@university\\.edu$"]
        self.customer.user_nationalities = ["EE"]
        self.customer.save()

        # Should not raise - both basic and AAI filters match
        validate_user_restrictions(self.customer, self.user)

    def test_basic_filter_fails_aai_not_checked(self):
        """Test that if basic filter fails, AAI filter is not checked."""
        self.user.email = "user@other.com"
        self.user.affiliations = []
        self.user.nationality = "EE"
        self.user.save()

        self.customer.user_email_patterns = [".*@university\\.edu$"]
        self.customer.user_nationalities = ["EE"]
        self.customer.save()

        # Should raise - basic filter fails (even though AAI would match)
        with self.assertRaises(ValidationError) as context:
            validate_user_restrictions(self.customer, self.user)
        self.assertIn("email", str(context.exception))


class ProjectAAIFilteringTest(TestCase):
    """Test that Project inherits Customer AAI restrictions."""

    def setUp(self):
        self.user = core_models.User.objects.create_user(
            username="testuser",
            email="test@example.com",
        )
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)

    def test_project_inherits_customer_nationality_filter(self):
        """Test that Project inherits Customer nationality restrictions."""
        self.user.nationality = "US"
        self.user.save()

        # Set restriction on customer
        self.customer.user_nationalities = ["EE", "FI"]
        self.customer.save()

        # Project has no restrictions itself
        self.project.user_nationalities = []
        self.project.save()

        # Should raise - customer nationality restriction not met
        with self.assertRaises(ValidationError) as context:
            validate_user_restrictions(self.project, self.user)
        self.assertIn("nationality", str(context.exception))

    def test_project_adds_own_aai_restrictions(self):
        """Test that Project can add its own AAI restrictions on top of Customer."""
        self.user.nationality = "EE"
        self.user.organization_type = "government"
        self.user.save()

        # Customer allows EE nationality
        self.customer.user_nationalities = ["EE"]
        self.customer.user_organization_types = []
        self.customer.save()

        # Project requires university organization type
        self.project.user_organization_types = [
            "urn:schac:homeOrganizationType:int:university"
        ]
        self.project.save()

        # Should raise - project organization type restriction not met
        with self.assertRaises(ValidationError) as context:
            validate_user_restrictions(self.project, self.user)
        self.assertIn("organization type", str(context.exception))
