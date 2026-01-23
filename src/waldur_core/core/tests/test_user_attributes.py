from unittest.mock import patch

from django.test import TestCase

from waldur_core.core.user_attributes import (
    ALL_PROFILE_ATTRIBUTES,
    CORE_USER_ATTRIBUTES,
    get_enabled_idp_sync_fields,
    get_enabled_profile_attributes,
    is_attribute_enabled,
)


class TestCoreUserAttributes(TestCase):
    """Test that core user attributes constants are correctly defined."""

    def test_core_attributes_are_defined(self):
        expected = {"username", "email", "first_name", "last_name", "full_name"}
        self.assertEqual(CORE_USER_ATTRIBUTES, expected)

    def test_all_profile_attributes_are_defined(self):
        expected = {
            "phone_number",
            "organization",
            "job_title",
            "affiliations",
            "gender",
            "personal_title",
            "birth_date",
            "place_of_birth",
            "country_of_residence",
            "nationality",
            "nationalities",
            "organization_country",
            "organization_type",
            "eduperson_assurance",
            "civil_number",
            "identity_source",
        }
        self.assertEqual(ALL_PROFILE_ATTRIBUTES, expected)


class TestGetEnabledProfileAttributes(TestCase):
    """Test get_enabled_profile_attributes function."""

    @patch("waldur_core.core.user_attributes.config")
    def test_core_attributes_always_included(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = []
        result = get_enabled_profile_attributes()
        for attr in CORE_USER_ATTRIBUTES:
            self.assertIn(attr, result)

    @patch("waldur_core.core.user_attributes.config")
    def test_configured_profile_attributes_included(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = ["phone_number", "organization"]
        result = get_enabled_profile_attributes()
        self.assertIn("phone_number", result)
        self.assertIn("organization", result)

    @patch("waldur_core.core.user_attributes.config")
    def test_invalid_attributes_filtered_out(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = [
            "phone_number",
            "invalid_attribute",
        ]
        result = get_enabled_profile_attributes()
        self.assertIn("phone_number", result)
        self.assertNotIn("invalid_attribute", result)

    @patch("waldur_core.core.user_attributes.config")
    def test_empty_config_returns_only_core_attributes(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = []
        result = get_enabled_profile_attributes()
        self.assertEqual(result, CORE_USER_ATTRIBUTES)

    @patch("waldur_core.core.user_attributes.config")
    def test_none_config_returns_only_core_attributes(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = None
        result = get_enabled_profile_attributes()
        self.assertEqual(result, CORE_USER_ATTRIBUTES)


class TestGetEnabledIdpSyncFields(TestCase):
    """Test get_enabled_idp_sync_fields function."""

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_intersection_with_writable_fields(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = [
            "phone_number",
            "organization",
            "job_title",  # This is NOT in WRITABLE_USER_FIELDS
        ]
        result = get_enabled_idp_sync_fields()
        self.assertIn("phone_number", result)
        self.assertIn("organization", result)
        self.assertNotIn("job_title", result)  # Not in WRITABLE_USER_FIELDS

    @patch("waldur_core.core.user_attributes.config")
    def test_core_writable_fields_included(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = []
        result = get_enabled_idp_sync_fields()
        # Core fields that are also in WRITABLE_USER_FIELDS
        self.assertIn("first_name", result)
        self.assertIn("last_name", result)
        self.assertIn("email", result)


class TestIsAttributeEnabled(TestCase):
    """Test is_attribute_enabled function."""

    @patch("waldur_core.core.user_attributes.config")
    def test_core_attribute_always_enabled(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = []
        self.assertTrue(is_attribute_enabled("username"))
        self.assertTrue(is_attribute_enabled("email"))
        self.assertTrue(is_attribute_enabled("first_name"))
        self.assertTrue(is_attribute_enabled("last_name"))

    @patch("waldur_core.core.user_attributes.config")
    def test_configured_attribute_enabled(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = ["phone_number"]
        self.assertTrue(is_attribute_enabled("phone_number"))

    @patch("waldur_core.core.user_attributes.config")
    def test_unconfigured_attribute_disabled(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = ["phone_number"]
        self.assertFalse(is_attribute_enabled("organization"))

    @patch("waldur_core.core.user_attributes.config")
    def test_invalid_attribute_disabled(self, mock_config):
        mock_config.ENABLED_USER_PROFILE_ATTRIBUTES = ["invalid_attr"]
        self.assertFalse(is_attribute_enabled("invalid_attr"))
