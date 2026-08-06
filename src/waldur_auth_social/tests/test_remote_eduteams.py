from unittest.mock import patch

from django.test import TestCase
from rest_framework.exceptions import NotFound

from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
from waldur_auth_social.models import IdentityProvider
from waldur_auth_social.utils import pull_remote_eduteams_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import BASIC_OFFERING
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class RemoteEduteamsUserTest(TestCase):
    def setUp(self):
        self.username = "test_user"
        self.remote_user_info = {
            "family_name": "Johnson",
            "given_name": "Alex",
            "mail": ["alex.johnson@example.edu"],
            "name": "Alex Johnson",
            "preferred_username": "",
            "ssh_public_key": [
                "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDhs9Hzz2FV3eRDhuhKMIuP8I+hyK6nLWus247rvWQpbSWvPWky530MTPqWFWFnEeJydl0HfEAiCl8F3z7yRRmv+JkUxxt6HhZhaqhPQX+YzpUT07tZCUhuV/k2ObPm+UfxVA/QHdP2xuH5eKhOhpW/5L7fsyln2wMDDZZcdfTcFp/uC5ysUsxgkH/zFwHJD8srk5i4HyvSgUbf0PZjPLjD19o4apxuswjdNyL3VfxDl8gsMlCfmxKWfDyOkYi6OnZsGQtj4t+h++dKOqxuCohkg+RAyEm4IIZrtTGhnGcKtlX1PBMtUhWhPRRi3787TgQm98FGwwNPpHA4cakx7cit testuser@test-machine.local"
            ],
            "voperson_external_affiliation": [
                "staff@example.edu",
                "member@dept.example.edu",
                "faculty@example.edu",
                "faculty@dept.example.edu",
                "employee@dept.example.edu",
                "alum@dept.example.edu",
                "staff@dept.example.edu",
            ],
            "voperson_id": "12345678-abcd-4c41-99d3-27b24fa74878@testdomain.org",
        }

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    @patch("waldur_auth_social.utils.create_or_update_oauth_user")
    def test_pull_remote_eduteams_user_success_new_user(
        self, mock_create_or_update, mock_get_user_info
    ):
        """Test successful user creation with email list conversion"""
        mock_get_user_info.return_value = self.remote_user_info.copy()
        mock_user = structure_factories.UserFactory()
        mock_create_or_update.return_value = (mock_user, True)

        result, _ = pull_remote_eduteams_user(self.username)

        # Verify get_remote_eduteams_user_info was called
        mock_get_user_info.assert_called_once_with(self.username)

        # Verify email conversion from list to string
        call_args = mock_create_or_update.call_args
        user_info_passed = call_args[0][1]
        self.assertEqual(user_info_passed["mail"], "alex.johnson@example.edu")
        self.assertNotIsInstance(user_info_passed["mail"], list)

        # Verify REMOTE_EDUTEAMS provider config is used
        config_passed = call_args[0][0]
        self.assertEqual(config_passed.provider, ProviderChoices.REMOTE_EDUTEAMS)
        self.assertEqual(config_passed.user_claim, "voperson_id")

        # Verify result
        self.assertEqual(result, mock_user)

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    @patch("waldur_auth_social.utils.create_or_update_oauth_user")
    def test_pull_remote_eduteams_user_email_already_string(
        self, mock_create_or_update, mock_get_user_info
    ):
        """Test when email is already a string (not a list)"""
        user_info = self.remote_user_info.copy()
        user_info["mail"] = "alex.johnson@example.edu"  # Already string
        mock_get_user_info.return_value = user_info
        mock_user = structure_factories.UserFactory()
        mock_create_or_update.return_value = (mock_user, True)

        result, _ = pull_remote_eduteams_user(self.username)

        # Verify email remains unchanged
        call_args = mock_create_or_update.call_args
        user_info_passed = call_args[0][1]
        self.assertEqual(user_info_passed["mail"], "alex.johnson@example.edu")

        self.assertEqual(result, mock_user)

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    @patch("waldur_auth_social.utils.create_or_update_oauth_user")
    def test_pull_remote_eduteams_user_empty_email_list(
        self, mock_create_or_update, mock_get_user_info
    ):
        """Test when email list is empty"""
        user_info = self.remote_user_info.copy()
        user_info["mail"] = []  # Empty list
        mock_get_user_info.return_value = user_info
        mock_user = structure_factories.UserFactory()
        mock_create_or_update.return_value = (mock_user, True)

        result, _ = pull_remote_eduteams_user(self.username)

        # Verify email list is converted to empty string when empty
        call_args = mock_create_or_update.call_args
        user_info_passed = call_args[0][1]
        self.assertEqual(user_info_passed["mail"], "")

        self.assertEqual(result, mock_user)

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    @patch("waldur_auth_social.utils.create_or_update_oauth_user")
    def test_pull_remote_eduteams_user_no_email_field(
        self, mock_create_or_update, mock_get_user_info
    ):
        """Test when email field is missing"""
        user_info = self.remote_user_info.copy()
        del user_info["mail"]  # Remove email field
        mock_get_user_info.return_value = user_info
        mock_user = structure_factories.UserFactory()
        mock_create_or_update.return_value = (mock_user, True)

        result, _ = pull_remote_eduteams_user(self.username)

        # Verify function works without email field
        call_args = mock_create_or_update.call_args
        user_info_passed = call_args[0][1]
        self.assertNotIn("mail", user_info_passed)

        self.assertEqual(result, mock_user)

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    def test_pull_remote_eduteams_user_not_found_deactivates_existing_user(
        self, mock_get_user_info
    ):
        """Test that existing user is deactivated when not found remotely"""
        mock_get_user_info.side_effect = NotFound("User not found")

        # Create existing active user
        existing_user = structure_factories.UserFactory(
            username=self.username, is_active=True
        )
        original_last_sync = existing_user.last_sync

        pull_remote_eduteams_user(self.username)

        # Verify user was deactivated
        existing_user.refresh_from_db()
        self.assertFalse(existing_user.is_active)
        self.assertIsNotNone(existing_user.last_sync)
        self.assertNotEqual(existing_user.last_sync, original_last_sync)

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    def test_pull_remote_eduteams_user_not_found_clears_details_json_field(
        self, mock_get_user_info
    ):
        """Remote user removal must not set JSONField details to an empty string."""
        mock_get_user_info.side_effect = NotFound("User not found")

        offering = marketplace_factories.OfferingFactory(
            type=BASIC_OFFERING,
            plugin_options={
                "username_generation_policy": (
                    marketplace_utils.UsernameGenerationPolicy.IDENTITY_CLAIM.value
                )
            },
        )
        existing_user = structure_factories.UserFactory(
            username=self.username,
            is_active=True,
            details={"site_username": "remote-user"},
            active_isds=["isd:eduteams"],
            attribute_sources={
                "details": {
                    "source": "isd:eduteams",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
            },
        )
        marketplace_factories.OfferingUserFactory(
            offering=offering,
            user=existing_user,
            username="remote-user",
        )

        pull_remote_eduteams_user(self.username)

        existing_user.refresh_from_db()
        self.assertFalse(existing_user.is_active)
        self.assertEqual(existing_user.details, {})

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    def test_pull_remote_eduteams_user_not_found_no_existing_user(
        self, mock_get_user_info
    ):
        """Test when user not found remotely and doesn't exist locally"""
        mock_get_user_info.side_effect = NotFound("User not found")

        result, _ = pull_remote_eduteams_user(self.username)

        # Verify function returns None
        self.assertIsNone(result)

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    @patch("waldur_auth_social.utils.create_or_update_oauth_user")
    def test_pull_remote_eduteams_user_uses_existing_provider_config(
        self, mock_create_or_update, mock_get_user_info
    ):
        """Test that existing REMOTE_EDUTEAMS provider config is used"""
        mock_get_user_info.return_value = self.remote_user_info.copy()
        mock_user = structure_factories.UserFactory()
        mock_create_or_update.return_value = (mock_user, True)

        # Create existing provider config
        existing_provider = IdentityProvider.objects.create(
            provider=ProviderChoices.REMOTE_EDUTEAMS,
            client_id="test_client",
            **PROVIDER_DEFAULTS[ProviderChoices.REMOTE_EDUTEAMS],
        )

        pull_remote_eduteams_user(self.username)

        # Verify existing provider config is used
        call_args = mock_create_or_update.call_args
        config_passed = call_args[0][0]
        self.assertEqual(config_passed.id, existing_provider.id)
        self.assertEqual(config_passed.client_id, "test_client")

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    @patch("waldur_auth_social.utils.create_or_update_oauth_user")
    def test_pull_remote_eduteams_user_creates_default_provider_config(
        self, mock_create_or_update, mock_get_user_info
    ):
        """Test that default provider config is created when none exists"""
        mock_get_user_info.return_value = self.remote_user_info.copy()
        mock_user = structure_factories.UserFactory()
        mock_create_or_update.return_value = (mock_user, True)

        # Ensure no existing provider config
        self.assertFalse(
            IdentityProvider.objects.filter(
                provider=ProviderChoices.REMOTE_EDUTEAMS
            ).exists()
        )

        pull_remote_eduteams_user(self.username)

        # Verify default provider config is used
        call_args = mock_create_or_update.call_args
        config_passed = call_args[0][0]
        self.assertEqual(config_passed.provider, ProviderChoices.REMOTE_EDUTEAMS)
        self.assertEqual(config_passed.user_claim, "voperson_id")
        self.assertEqual(config_passed.user_field, "username")

        # Verify attribute mapping for email
        expected_mapping = PROVIDER_DEFAULTS[ProviderChoices.REMOTE_EDUTEAMS][
            "attribute_mapping"
        ]
        self.assertEqual(config_passed.attribute_mapping, expected_mapping)
        self.assertEqual(config_passed.attribute_mapping["email"], "mail")

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    @patch("waldur_auth_social.utils.create_or_update_oauth_user")
    def test_pull_remote_eduteams_user_multiple_emails_uses_first(
        self, mock_create_or_update, mock_get_user_info
    ):
        """Test that first email is used when multiple emails in list"""
        user_info = self.remote_user_info.copy()
        user_info["mail"] = [
            "first@example.com",
            "second@example.com",
            "third@example.com",
        ]
        mock_get_user_info.return_value = user_info
        mock_user = structure_factories.UserFactory()
        mock_create_or_update.return_value = (mock_user, True)

        result, _ = pull_remote_eduteams_user(self.username)

        # Verify first email is used
        call_args = mock_create_or_update.call_args
        user_info_passed = call_args[0][1]
        self.assertEqual(user_info_passed["mail"], "first@example.com")

        self.assertEqual(result, mock_user)

    @patch("waldur_auth_social.utils.get_remote_eduteams_user_info")
    @patch("waldur_auth_social.utils.create_or_update_oauth_user")
    def test_pull_remote_eduteams_user_preserves_other_fields(
        self, mock_create_or_update, mock_get_user_info
    ):
        """Test that other user info fields are preserved during email conversion"""
        mock_get_user_info.return_value = self.remote_user_info.copy()
        mock_user = structure_factories.UserFactory()
        mock_create_or_update.return_value = (mock_user, True)

        pull_remote_eduteams_user(self.username)

        # Verify other fields are preserved
        call_args = mock_create_or_update.call_args
        user_info_passed = call_args[0][1]
        self.assertEqual(user_info_passed["family_name"], "Johnson")
        self.assertEqual(user_info_passed["given_name"], "Alex")
        self.assertEqual(
            user_info_passed["voperson_id"],
            "12345678-abcd-4c41-99d3-27b24fa74878@testdomain.org",
        )
        self.assertEqual(
            user_info_passed["voperson_external_affiliation"],
            self.remote_user_info["voperson_external_affiliation"],
        )
        self.assertEqual(
            user_info_passed["ssh_public_key"], self.remote_user_info["ssh_public_key"]
        )
