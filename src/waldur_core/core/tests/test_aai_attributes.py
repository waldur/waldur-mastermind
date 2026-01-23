"""
Tests for extended user profile attributes on User model.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from waldur_core.core import models
from waldur_core.core.validators import (
    validate_iso_3166_alpha2,
    validate_refeds_assurance_list,
    validate_schac_organization_type,
)


class ISO3166Alpha2ValidatorTest(TestCase):
    """Test ISO 3166-1 alpha-2 country code validator."""

    def test_valid_country_codes(self):
        """Test that valid country codes pass validation."""
        valid_codes = ["US", "DE", "EE", "FI", "GB", "FR", "EU"]
        for code in valid_codes:
            try:
                validate_iso_3166_alpha2(code)
            except ValidationError:
                self.fail(f"Valid country code '{code}' failed validation")

    def test_invalid_country_codes(self):
        """Test that invalid country codes raise ValidationError."""
        invalid_codes = ["XX", "ZZ", "1A", "ABC", "123"]
        for code in invalid_codes:
            with self.assertRaises(ValidationError, msg=f"'{code}' should be invalid"):
                validate_iso_3166_alpha2(code)

    def test_empty_and_none_values(self):
        """Test that empty and None values are allowed (nullable field)."""
        # Empty string is allowed (blank=True)
        validate_iso_3166_alpha2("")
        # None should not raise error for nullable field
        validate_iso_3166_alpha2(None)

    def test_case_insensitive(self):
        """Test that validation is case-insensitive."""
        validate_iso_3166_alpha2("us")
        validate_iso_3166_alpha2("De")


class SCHACOrganizationTypeValidatorTest(TestCase):
    """Test SCHAC organization type validator."""

    def test_valid_schac_urns(self):
        """Test valid SCHAC URN formats."""
        valid_values = [
            "urn:schac:homeOrganizationType:int:university",
            "urn:schac:homeOrganizationType:de:research-institution",
            "urn:schac:homeOrganizationType:ee:government",
        ]
        for value in valid_values:
            try:
                validate_schac_organization_type(value)
            except ValidationError:
                self.fail(f"Valid SCHAC URN '{value}' failed validation")

    def test_valid_simple_identifiers(self):
        """Test valid simple organization type identifiers."""
        valid_values = ["university", "research-institution", "government_org"]
        for value in valid_values:
            try:
                validate_schac_organization_type(value)
            except ValidationError:
                self.fail(f"Valid identifier '{value}' failed validation")

    def test_invalid_formats(self):
        """Test that invalid formats raise ValidationError."""
        invalid_values = [
            "urn:invalid:format",
            "http://example.com",
            "spaces not allowed",
        ]
        for value in invalid_values:
            with self.assertRaises(ValidationError, msg=f"'{value}' should be invalid"):
                validate_schac_organization_type(value)

    def test_empty_value(self):
        """Test that empty value is allowed."""
        validate_schac_organization_type("")
        validate_schac_organization_type(None)


class REFEDSAssuranceListValidatorTest(TestCase):
    """Test REFEDS assurance list validator."""

    def test_valid_assurance_uris(self):
        """Test valid REFEDS assurance URIs."""
        valid_values = [
            ["https://refeds.org/assurance/IAP/high"],
            ["https://refeds.org/assurance/ID/eppn-unique-no-reassign"],
            ["urn:oasis:names:tc:SAML:2.0:ac:classes:Password"],
            [
                "https://refeds.org/assurance/IAP/high",
                "https://refeds.org/assurance/ID/unique",
            ],
        ]
        for value in valid_values:
            try:
                validate_refeds_assurance_list(value)
            except ValidationError:
                self.fail(f"Valid assurance list '{value}' failed validation")

    def test_invalid_assurance_uris(self):
        """Test that invalid URIs raise ValidationError."""
        invalid_values = [
            ["invalid-uri"],
            ["ftp://example.com/assurance"],
            [123],  # Non-string item
        ]
        for value in invalid_values:
            with self.assertRaises(ValidationError, msg=f"'{value}' should be invalid"):
                validate_refeds_assurance_list(value)

    def test_empty_list(self):
        """Test that empty list is allowed."""
        validate_refeds_assurance_list([])
        validate_refeds_assurance_list(None)

    def test_non_list_type_raises_error(self):
        """Test that non-list type raises ValidationError."""
        with self.assertRaises(ValidationError):
            validate_refeds_assurance_list("not-a-list")


class UserExtendedProfileFieldsTest(TestCase):
    """Test extended user profile fields on User model."""

    def setUp(self):
        self.user = models.User.objects.create_user(
            username="testuser",
            email="test@example.com",
        )

    def test_gender_field(self):
        """Test gender field with ISO 5218 codes."""
        for code, label in models.GENDER_CHOICES:
            self.user.gender = code
            self.user.save()
            self.user.refresh_from_db()
            self.assertEqual(self.user.gender, code)

    def test_personal_title_field(self):
        """Test personal title field."""
        titles = ["Mr", "Ms", "Dr", "Prof", "Mx"]
        for title in titles:
            self.user.personal_title = title
            self.user.save()
            self.user.refresh_from_db()
            self.assertEqual(self.user.personal_title, title)

    def test_country_fields(self):
        """Test country code fields."""
        self.user.country_of_residence = "EE"
        self.user.nationality = "FI"
        self.user.organization_country = "DE"
        self.user.save()
        self.user.refresh_from_db()

        self.assertEqual(self.user.country_of_residence, "EE")
        self.assertEqual(self.user.nationality, "FI")
        self.assertEqual(self.user.organization_country, "DE")

    def test_nationalities_json_field(self):
        """Test nationalities JSON field stores list of country codes."""
        nationalities = ["FI", "EE", "SE"]
        self.user.nationalities = nationalities
        self.user.save()
        self.user.refresh_from_db()

        self.assertEqual(self.user.nationalities, nationalities)

    def test_organization_type_field(self):
        """Test organization type field."""
        self.user.organization_type = "urn:schac:homeOrganizationType:int:university"
        self.user.save()
        self.user.refresh_from_db()

        self.assertEqual(
            self.user.organization_type,
            "urn:schac:homeOrganizationType:int:university",
        )

    def test_eduperson_assurance_field(self):
        """Test eduPerson assurance JSON field."""
        assurance = [
            "https://refeds.org/assurance/IAP/high",
            "https://refeds.org/assurance/ID/unique",
        ]
        self.user.eduperson_assurance = assurance
        self.user.save()
        self.user.refresh_from_db()

        self.assertEqual(self.user.eduperson_assurance, assurance)

    def test_extended_profile_fields_in_whitelist(self):
        """Test that extended profile fields are in WHITELIST_FIELDS for logging."""
        extended_fields = [
            "gender",
            "personal_title",
            "place_of_birth",
            "country_of_residence",
            "nationality",
            "nationalities",
            "organization_country",
            "organization_type",
            "eduperson_assurance",
        ]
        for field in extended_fields:
            self.assertIn(
                field,
                models.User.WHITELIST_FIELDS,
                f"Extended profile field '{field}' should be in WHITELIST_FIELDS",
            )

    def test_invalid_country_code_raises_validation_error(self):
        """Test that invalid country codes raise ValidationError."""
        self.user.country_of_residence = "XX"
        with self.assertRaises(ValidationError):
            self.user.full_clean()
