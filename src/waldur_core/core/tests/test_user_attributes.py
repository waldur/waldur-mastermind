from unittest.mock import MagicMock, patch

from django.test import TestCase

from waldur_core.core.user_attributes import (
    ALL_PROFILE_ATTRIBUTES,
    CORE_USER_ATTRIBUTES,
    get_enabled_idp_sync_fields,
    get_enabled_profile_attributes,
    get_mandatory_attributes,
    get_profile_completeness_details,
    get_user_missing_mandatory_attributes,
    is_attribute_enabled,
    is_user_profile_complete,
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
            "address",
            "country_of_residence",
            "nationality",
            "nationalities",
            "organization_country",
            "organization_type",
            "organization_registry_code",
            "eduperson_assurance",
            "civil_number",
            "identity_source",
            "active_isds",
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


class TestGetMandatoryAttributes(TestCase):
    """Test get_mandatory_attributes function."""

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_configured_mandatory_attributes(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number", "organization"]
        result = get_mandatory_attributes()
        self.assertEqual(result, ["phone_number", "organization"])

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_empty_list_when_not_configured(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = []
        result = get_mandatory_attributes()
        self.assertEqual(result, [])

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_empty_list_when_none(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = None
        result = get_mandatory_attributes()
        self.assertEqual(result, [])


class TestGetUserMissingMandatoryAttributes(TestCase):
    """Test get_user_missing_mandatory_attributes function."""

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_empty_when_no_mandatory_attributes(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = []
        user = MagicMock()
        result = get_user_missing_mandatory_attributes(user)
        self.assertEqual(result, [])

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_missing_fields_when_none(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number", "organization"]
        user = MagicMock()
        user.phone_number = None
        user.organization = "Test Org"
        result = get_user_missing_mandatory_attributes(user)
        self.assertEqual(result, ["phone_number"])

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_missing_fields_when_empty_string(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number", "job_title"]
        user = MagicMock()
        user.phone_number = ""
        user.job_title = "Developer"
        result = get_user_missing_mandatory_attributes(user)
        self.assertEqual(result, ["phone_number"])

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_missing_fields_when_empty_list(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["affiliations"]
        user = MagicMock()
        user.affiliations = []
        result = get_user_missing_mandatory_attributes(user)
        self.assertEqual(result, ["affiliations"])

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_empty_when_all_mandatory_filled(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number", "organization"]
        user = MagicMock()
        user.phone_number = "+1234567890"
        user.organization = "Test Org"
        result = get_user_missing_mandatory_attributes(user)
        self.assertEqual(result, [])

    @patch("waldur_core.core.user_attributes.config")
    def test_handles_missing_attribute_on_user(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["nonexistent_field"]
        user = MagicMock(spec=["phone_number"])
        result = get_user_missing_mandatory_attributes(user)
        self.assertEqual(result, ["nonexistent_field"])


class TestIsUserProfileComplete(TestCase):
    """Test is_user_profile_complete function."""

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_true_when_no_mandatory_attributes(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = []
        user = MagicMock()
        self.assertTrue(is_user_profile_complete(user))

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_true_when_all_mandatory_filled(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number"]
        user = MagicMock()
        user.phone_number = "+1234567890"
        self.assertTrue(is_user_profile_complete(user))

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_false_when_mandatory_missing(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number"]
        user = MagicMock()
        user.phone_number = None
        self.assertFalse(is_user_profile_complete(user))


class TestGetProfileCompletenessDetails(TestCase):
    """Test get_profile_completeness_details function."""

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_complete_details(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number", "organization"]
        mock_config.ENFORCE_MANDATORY_USER_ATTRIBUTES = True
        user = MagicMock()
        user.phone_number = "+1234567890"
        user.organization = None

        result = get_profile_completeness_details(user)

        self.assertFalse(result["is_complete"])
        self.assertEqual(result["missing_fields"], ["organization"])
        self.assertEqual(result["mandatory_fields"], ["phone_number", "organization"])
        self.assertTrue(result["enforcement_enabled"])

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_complete_when_all_filled(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number"]
        mock_config.ENFORCE_MANDATORY_USER_ATTRIBUTES = False
        user = MagicMock()
        user.phone_number = "+1234567890"

        result = get_profile_completeness_details(user)

        self.assertTrue(result["is_complete"])
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["mandatory_fields"], ["phone_number"])
        self.assertFalse(result["enforcement_enabled"])

    @patch("waldur_core.core.user_attributes.config")
    def test_returns_complete_when_no_mandatory(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = []
        mock_config.ENFORCE_MANDATORY_USER_ATTRIBUTES = False
        user = MagicMock()

        result = get_profile_completeness_details(user)

        self.assertTrue(result["is_complete"])
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["mandatory_fields"], [])
