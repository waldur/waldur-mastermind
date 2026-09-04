from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import responses
from constance.test.unittest import override_config
from django.utils import timezone
from rest_framework import status, test
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from rest_framework.reverse import reverse

from waldur_auth_social import models
from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
from waldur_auth_social.serializers import IdentityProviderSerializer
from waldur_auth_social.utils import (
    create_or_update_oauth_user,
    parse_schac_personal_unique_id,
)
from waldur_auth_social.views import (
    OIDC_CODE_VERIFIER_KEY,
    OIDC_REFERRER_KEY,
    OIDC_RETURN_URL_KEY,
    OIDC_STATE_KEY,
)
from waldur_autoprovisioning.tests import factories as autoprovisioning_factories
from waldur_core.core.models import User
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.enums import InvitationState
from waldur_core.users.tests import factories as user_factories


def assert_login_failed_redirect(test_case, response, expected_message):
    """Assert the response redirects to the Homeport login-failed page with the message."""
    test_case.assertEqual(response.status_code, status.HTTP_302_FOUND)
    parsed_url = urlparse(response.url)
    test_case.assertEqual(parsed_url.path, "/login_failed/")
    query_params = parse_qs(parsed_url.query)
    test_case.assertEqual(query_params["message"], [expected_message])


class OAuthViewInitTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.provider = models.IdentityProvider.objects.create(
            provider=ProviderChoices.KEYCLOAK,
            client_id="test_client_id",
            client_secret="test_client_secret",
            discovery_url="http://keycloak.test/.well-known/openid-configuration",
            userinfo_url="http://keycloak.test/userinfo",
            token_url="http://keycloak.test/token",
            auth_url="http://keycloak.test/auth",
            **PROVIDER_DEFAULTS[ProviderChoices.KEYCLOAK],
        )
        self.url = reverse(f"auth_{self.provider.provider}_init")

    def test_successful_redirect(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

        # Check session
        self.assertIn(OIDC_STATE_KEY, self.client.session)

        # Check redirect URL
        parsed_url = urlparse(response.url)
        self.assertEqual(parsed_url.scheme, "http")
        self.assertEqual(parsed_url.netloc, "keycloak.test")
        self.assertEqual(parsed_url.path, "/auth")

        # Check query params
        query_params = parse_qs(parsed_url.query)
        self.assertEqual(query_params["response_type"], ["code"])
        self.assertEqual(query_params["client_id"], [self.provider.client_id])
        self.assertIn("redirect_uri", query_params)
        self.assertEqual(query_params["scope"], ["openid"])
        self.assertEqual(query_params["state"], [self.client.session[OIDC_STATE_KEY]])

    def test_pkce_is_enabled(self):
        self.provider.enable_pkce = True
        self.provider.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

        # Check session
        self.assertIn(OIDC_STATE_KEY, self.client.session)
        self.assertIn(OIDC_CODE_VERIFIER_KEY, self.client.session)

        # Check redirect URL params for PKCE
        parsed_url = urlparse(response.url)
        query_params = parse_qs(parsed_url.query)
        self.assertIn("code_challenge", query_params)
        self.assertIn("code_challenge_method", query_params)
        self.assertEqual(query_params["code_challenge_method"], ["S256"])

    def test_custom_scope(self):
        self.provider.extra_scope = "profile email"
        self.provider.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

        parsed_url = urlparse(response.url)
        query_params = parse_qs(parsed_url.query)
        self.assertEqual(query_params["scope"], [f"openid {self.provider.extra_scope}"])

    def test_authenticated_user_is_rejected(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("This view is for anonymous users only", str(response.content))

    def test_inactive_provider(self):
        self.provider.is_active = False
        self.provider.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Identity provider is disabled", str(response.content))

    def test_provider_not_defined(self):
        self.provider.delete()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Identity provider is not defined", str(response.content))


class OAuthViewDefaultInitTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.provider = models.IdentityProvider.objects.create(
            provider=ProviderChoices.KEYCLOAK,
            client_id="test_client_id",
            client_secret="test_client_secret",
            discovery_url="http://keycloak.test/.well-known/openid-configuration",
            userinfo_url="http://keycloak.test/userinfo",
            token_url="http://keycloak.test/token",
            auth_url="http://keycloak.test/auth",
            **PROVIDER_DEFAULTS[ProviderChoices.KEYCLOAK],
        )
        self.url = reverse("auth_default_init")
        self.fallback_url = "https://portal.example.com/login/?disableAutoLogin"

    @override_config(DEFAULT_IDP=ProviderChoices.KEYCLOAK)
    def test_redirects_to_default_provider(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn(OIDC_STATE_KEY, self.client.session)

        parsed_url = urlparse(response.url)
        self.assertEqual(parsed_url.netloc, "keycloak.test")
        self.assertEqual(parsed_url.path, "/auth")
        query_params = parse_qs(parsed_url.query)
        self.assertEqual(query_params["client_id"], [self.provider.client_id])
        self.assertEqual(query_params["state"], [self.client.session[OIDC_STATE_KEY]])
        self.assertTrue(
            query_params["redirect_uri"][0].endswith("/api-auth/keycloak/complete/")
        )

    @override_config(DEFAULT_IDP=ProviderChoices.KEYCLOAK)
    def test_return_url_and_locale_are_forwarded(self):
        response = self.client.get(
            self.url,
            {"return_url": "https://portal.example.com", "ui_locales": "et"},
        )
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(
            self.client.session[OIDC_RETURN_URL_KEY], "https://portal.example.com"
        )
        query_params = parse_qs(urlparse(response.url).query)
        self.assertEqual(query_params["ui_locales"], ["et"])

    @override_config(DEFAULT_IDP=ProviderChoices.KEYCLOAK)
    def test_pkce_of_default_provider_is_honoured(self):
        self.provider.enable_pkce = True
        self.provider.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn(OIDC_CODE_VERIFIER_KEY, self.client.session)
        query_params = parse_qs(urlparse(response.url).query)
        self.assertEqual(query_params["code_challenge_method"], ["S256"])

    @override_config(DEFAULT_IDP="", HOMEPORT_URL="https://portal.example.com/")
    def test_unset_default_falls_back_to_login_page(self):
        response = self.client.get(self.url, {"return_url": "https://evil.example"})
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response.url, self.fallback_url)
        self.assertNotIn(OIDC_STATE_KEY, self.client.session)

    @override_config(
        DEFAULT_IDP=ProviderChoices.KEYCLOAK,
        HOMEPORT_URL="https://portal.example.com",
    )
    def test_inactive_default_provider_falls_back_to_login_page(self):
        self.provider.is_active = False
        self.provider.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response.url, self.fallback_url)

    @override_config(
        DEFAULT_IDP=ProviderChoices.TARA, HOMEPORT_URL="https://portal.example.com"
    )
    def test_missing_default_provider_falls_back_to_login_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response.url, self.fallback_url)

    @override_config(DEFAULT_IDP=ProviderChoices.KEYCLOAK)
    def test_authenticated_user_is_rejected(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OAuthViewCompleteTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.provider = models.IdentityProvider.objects.create(
            provider=ProviderChoices.KEYCLOAK,
            client_id="test_client_id",
            client_secret="test_client_secret",
            discovery_url="http://keycloak.test/.well-known/openid-configuration",
            userinfo_url="http://keycloak.test/userinfo",
            token_url="http://keycloak.test/token",
            auth_url="http://keycloak.test/auth",
            **PROVIDER_DEFAULTS[ProviderChoices.KEYCLOAK],
        )
        self.url = reverse(f"auth_{self.provider.provider}_complete")
        self.state = "test_state"
        self.code = "test_code"

        # Setup session
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session.save()

        # Mock external requests
        responses.start()
        self.addCleanup(responses.stop)

    def _mock_token_request(
        self, access_token="test_access_token", refresh_token="test_refresh_token"
    ):
        return responses.add(
            method="POST",
            url=self.provider.token_url,
            json={"access_token": access_token, "refresh_token": refresh_token},
            status=status.HTTP_200_OK,
        )

    def _mock_userinfo_request(self, user_info):
        responses.add(
            method="GET",
            url=self.provider.userinfo_url,
            json=user_info,
            status=status.HTTP_200_OK,
        )

    def test_successful_login_new_user(self):
        user_info = {
            "sub": "test_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "test@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(
            response.url.startswith("https://example.com/oauth_login_completed/")
        )

        user = User.objects.get(username=user_info["sub"])
        self.assertEqual(user.first_name, user_info["given_name"])
        self.assertEqual(user.last_name, user_info["family_name"])
        self.assertEqual(user.email, user_info["email"])
        self.assertTrue(
            models.OAuthToken.objects.filter(
                user=user, provider=self.provider.provider
            ).exists()
        )
        self.assertIsNotNone(user.last_login)

        # Check code in redirect URL
        parsed_url = urlparse(response.url)
        exchange_code = parse_qs(parsed_url.query)["code"][0]

        # Exchange code for token
        exchange_url = reverse("auth-token-exchange")
        exchange_response = self.client.post(exchange_url, {"code": exchange_code})
        self.assertEqual(exchange_response.status_code, status.HTTP_200_OK)

        token_key = exchange_response.data["token"]
        self.assertTrue(Token.objects.filter(user=user, key=token_key).exists())

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["birth_date"])
    def test_new_user_oidc_iso_birthdate_string_succeeds_and_sets_birth_date(self):
        """OIDC birthdate is an ISO string; it must become a date so reversion can serialize."""
        user_info = {
            "sub": "user_birthdate_ok_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "birthdate_ok@example.com",
            "birthdate": "1983-01-21",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertEqual(user.birth_date.isoformat(), "1983-01-21")

    def test_successful_login_existing_user(self):
        user_info = {
            "sub": "existing_user",
            "given_name": "UpdatedFirstName",
            "family_name": "UpdatedLastName",
            "email": "updated@example.com",
        }
        existing_user = structure_factories.UserFactory(
            username=user_info["sub"],
            first_name="OldFirstName",
            last_name="OldLastName",
            email="old@example.com",
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        existing_user.refresh_from_db()
        self.assertEqual(existing_user.first_name, user_info["given_name"])
        self.assertEqual(existing_user.last_name, user_info["family_name"])
        self.assertEqual(existing_user.email, user_info["email"])
        self.assertTrue(
            models.OAuthToken.objects.filter(
                user=existing_user, provider=self.provider.provider
            ).exists()
        )

    def test_login_fails_for_deactivated_user(self):
        user_info = {
            "sub": "existing_user",
            "given_name": "Deactivated",
            "family_name": "User",
            "email": "deactivated@example.com",
        }
        # Create a user that is already in the system but is not active
        structure_factories.UserFactory(
            username=user_info["sub"],
            is_active=False,
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # Assert that the login fails with a specific error message
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("User is deactivated", str(response.content))

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_deactivated_user_with_pending_invitation_can_login(self):
        """
        When DEACTIVATE_USER_IF_NO_ROLES is enabled and a user was auto-deactivated
        due to losing all roles, they should still be able to log in via OIDC
        if they have a pending invitation — otherwise they can never regain access.
        """
        user_info = {
            "sub": "deactivated_invited_user",
            "given_name": "Invited",
            "family_name": "User",
            "email": "invited@example.com",
        }
        user = structure_factories.UserFactory(
            username=user_info["sub"],
            email=user_info["email"],
            is_active=False,
            deactivation_reason="No active roles and no course accounts",
        )
        # Create a pending invitation for this user
        user_factories.ProjectInvitationFactory(
            email=user_info["email"],
            state=InvitationState.PENDING,
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # User should be allowed to log in and be reactivated
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_deactivated_user_with_matching_group_invitation_can_login(self):
        """
        When DEACTIVATE_USER_IF_NO_ROLES is enabled and a user was auto-deactivated,
        they should still be able to log in if their email matches an active
        group invitation pattern.
        """
        user_info = {
            "sub": "deactivated_group_user",
            "given_name": "Group",
            "family_name": "User",
            "email": "groupuser@example.com",
        }
        user = structure_factories.UserFactory(
            username=user_info["sub"],
            email=user_info["email"],
            is_active=False,
            deactivation_reason="No active roles and no course accounts",
        )
        # Create an active group invitation matching this email
        user_factories.CustomerGroupInvitationFactory(
            user_email_patterns=[".*@example.com"],
            is_active=True,
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # User should be allowed to log in and be reactivated
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_deactivated_user_without_invitation_still_blocked(self):
        """
        When DEACTIVATE_USER_IF_NO_ROLES is enabled but the deactivated user has
        no pending invitation or matching group invitation, login should still fail.
        """
        user_info = {
            "sub": "deactivated_no_invite",
            "given_name": "No",
            "family_name": "Invite",
            "email": "noinvite@example.com",
        }
        structure_factories.UserFactory(
            username=user_info["sub"],
            email=user_info["email"],
            is_active=False,
            deactivation_reason="No active roles and no course accounts",
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("User is deactivated", str(response.content))

    def test_invalid_state(self):
        response = self.client.get(
            self.url, {"state": "invalid_state", "code": self.code}
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("Invalid auth state", str(response.content))

    def test_missing_state_in_session(self):
        session = self.client.session
        del session[OIDC_STATE_KEY]
        session.save()
        response = self.client.get(self.url, {"state": self.state, "code": self.code})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("Invalid auth state", str(response.content))

    def test_token_request_fails(self):
        responses.add(
            method="POST",
            url=self.provider.token_url,
            status=status.HTTP_400_BAD_REQUEST,
            json={
                "error": "invalid_grant",
                "error_description": "Invalid authorization code",
            },
        )
        response = self.client.get(self.url, {"state": self.state, "code": self.code})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("invalid_grant", str(response.content))

    def test_userinfo_request_fails(self):
        self._mock_token_request()
        responses.add(
            method="GET",
            url=self.provider.userinfo_url,
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        response = self.client.get(self.url, {"state": self.state, "code": self.code})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_user_is_rejected(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {"state": self.state, "code": self.code})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("This view is for anonymous users only", str(response.content))

    def test_inactive_provider(self):
        self.provider.is_active = False
        self.provider.save()
        response = self.client.get(self.url, {"state": self.state, "code": self.code})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Identity provider is disabled", str(response.content))

    @mock.patch("waldur_auth_social.views.event_logger")
    def test_login_event_is_logged(self, mock_event_logger):
        user_info = {
            "sub": "test_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "test@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        self.client.get(self.url, {"state": self.state, "code": self.code})

        mock_event_logger.emit.assert_called_once()
        user = User.objects.get(username=user_info["sub"])
        _args, kwargs = mock_event_logger.emit.call_args
        self.assertEqual(kwargs["event_type"], "auth_logged_in_with_oauth")
        self.assertEqual(kwargs["event_context"]["user"], user)
        self.assertEqual(kwargs["event_context"]["provider"], self.provider.provider)

    def test_pkce_flow(self):
        self.provider.enable_pkce = True
        self.provider.save()

        code_verifier = "test_code_verifier"
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_CODE_VERIFIER_KEY] = code_verifier
        session.save()

        user_info = {
            "sub": "pkce_user",
            "email": "pkce@example.com",
        }
        token_mock = self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

        # Check that code_verifier was sent in token request
        self.assertIn(
            f"code_verifier={code_verifier}", token_mock.calls[0].request.body
        )

    def test_pkce_flow_fails_if_verifier_is_missing(self):
        self.provider.enable_pkce = True
        self.provider.save()

        # code_verifier is not in session
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session.save()

        response = self.client.get(self.url, {"state": self.state, "code": self.code})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("PKCE verification failed", str(response.content))

    def test_tara_login_does_not_update_username_for_existing_user(self):
        # Configure provider for TARA
        tara_defaults = PROVIDER_DEFAULTS[ProviderChoices.TARA]
        self.provider.provider = ProviderChoices.TARA
        self.provider.user_field = tara_defaults["user_field"]
        self.provider.user_claim = tara_defaults["user_claim"]
        self.provider.attribute_mapping = tara_defaults["attribute_mapping"]
        self.provider.save()

        # Re-generate URL for TARA provider
        self.url = reverse(f"auth_{self.provider.provider}_complete")

        # Create existing user
        civil_number = "EE12345678901"
        original_username = "tara_user"
        existing_user = structure_factories.UserFactory(
            civil_number=civil_number,
            username=original_username,
            first_name="OldFirstName",
            last_name="OldLastName",
        )

        # Mock OIDC responses
        user_info = {
            "sub": civil_number,
            "given_name": "NewFirstName",
            "family_name": "NewLastName",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Perform login
        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # Assertions
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        existing_user.refresh_from_db()

        self.assertEqual(existing_user.username, original_username)
        self.assertEqual(existing_user.first_name, user_info["given_name"])
        self.assertEqual(existing_user.last_name, user_info["family_name"])
        self.assertEqual(existing_user.civil_number, civil_number)

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS_RESPONSE_MESSAGE="It is blocked"
    )
    def test_new_user_creation_is_blocked_if_uninvited_and_toggle_is_on(self):
        # Arrange: A new user with no invitation
        user_info = {
            "sub": "uninvited_user",
            "given_name": "Uninvited",
            "family_name": "User",
            "email": "uninvited@example.com",
        }
        self.assertEqual(User.objects.count(), 0)
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Act
        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # Assert
        assert_login_failed_redirect(self, response, "It is blocked")
        self.assertEqual(User.objects.count(), 0)

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_new_user_creation_is_allowed_if_invited_and_toggle_is_on(self):
        # Arrange: A new user with a valid invitation
        user_info = {
            "sub": "invited_user",
            "given_name": "Invited",
            "family_name": "User",
            "email": "invited@example.com",
        }
        project = structure_factories.ProjectFactory()
        user_factories.ProjectInvitationFactory(
            email=user_info["email"],
            scope=project,
            state=InvitationState.PENDING,
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Act
        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(User.objects.filter(username=user_info["sub"]).exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_new_user_creation_is_blocked_for_inactive_invitation(self):
        # Arrange: A new user with an expired invitation
        user_info = {
            "sub": "expired_invite_user",
            "given_name": "Expired",
            "family_name": "Invite",
            "email": "expired@example.com",
        }
        project = structure_factories.ProjectFactory()
        user_factories.ProjectInvitationFactory(
            email=user_info["email"],
            scope=project,
            state=InvitationState.EXPIRED,
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Act
        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # Assert
        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )
        self.assertFalse(User.objects.filter(username=user_info["sub"]).exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_new_user_creation_is_blocked_if_email_is_missing(self):
        # Arrange: User info from provider lacks an email address
        user_info = {
            "sub": "no_email_user",
            "given_name": "No",
            "family_name": "Email",
            # "email" is missing
        }
        self.assertEqual(User.objects.count(), 0)
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Act
        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # Assert
        assert_login_failed_redirect(
            self, response, "User email is not provided. Account creation is blocked."
        )
        self.assertEqual(User.objects.count(), 0)

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_new_user_creation_is_allowed_if_matching_group_invitation_exists(self):
        # Arrange: A new user whose email matches an active group invitation pattern
        user_info = {
            "sub": "group_invited_user",
            "given_name": "Group",
            "family_name": "Invited",
            "email": "groupuser@example.com",
        }
        user_factories.CustomerGroupInvitationFactory(
            user_email_patterns=[".*@example.com"],
            is_active=True,
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Act
        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # Assert: user should be created
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(User.objects.filter(username=user_info["sub"]).exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_new_user_creation_is_blocked_if_group_invitation_is_inactive(self):
        # Arrange: A new user whose email matches an inactive group invitation pattern
        user_info = {
            "sub": "inactive_group_user",
            "given_name": "Inactive",
            "family_name": "Group",
            "email": "inactivegroup@example.com",
        }
        user_factories.CustomerGroupInvitationFactory(
            user_email_patterns=[".*@example.com"],
            is_active=False,
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Act
        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # Assert: user should NOT be created
        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )
        self.assertFalse(User.objects.filter(username=user_info["sub"]).exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_new_user_creation_is_blocked_if_email_does_not_match_group_invitation_pattern(
        self,
    ):
        # Arrange: A new user whose email does NOT match the group invitation pattern
        user_info = {
            "sub": "nonmatching_user",
            "given_name": "NonMatching",
            "family_name": "User",
            "email": "user@otherdomain.com",
        }
        user_factories.CustomerGroupInvitationFactory(
            user_email_patterns=[".*@example.com"],
            is_active=True,
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Act
        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        # Assert: user should NOT be created
        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )
        self.assertFalse(User.objects.filter(username=user_info["sub"]).exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_new_user_creation_is_blocked_if_email_only_partially_matches_group_invitation_pattern(
        self,
    ):
        """The pattern must match the whole email, not just its beginning.

        Matching by prefix would let an invitation for a domain admit any
        lookalike domain that merely starts with it.
        """
        user_info = {
            "sub": "lookalike_group_user",
            "given_name": "Look",
            "family_name": "Alike",
            "email": "attacker@example.com.attacker.net",
        }
        user_factories.CustomerGroupInvitationFactory(
            user_email_patterns=[r".*@example\.com"],
            is_active=True,
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )
        self.assertFalse(User.objects.filter(username=user_info["sub"]).exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_group_invitation_pattern_match_is_case_insensitive(self):
        """Emails are compared case-insensitively elsewhere (``email__iexact``)."""
        user_info = {
            "sub": "mixed_case_group_user",
            "given_name": "Mixed",
            "family_name": "Case",
            "email": "Someone@EXAMPLE.CoM",
        }
        user_factories.CustomerGroupInvitationFactory(
            user_email_patterns=[r".*@example\.com"],
            is_active=True,
        )
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(User.objects.filter(username=user_info["sub"]).exists())

    @override_config(WALDUR_AUTH_SOCIAL_ROLE_CLAIM="roles")
    def test_user_assigned_roles_from_claims(self):
        user_info = {
            "sub": "test_role_user",
            "given_name": "Role",
            "family_name": "User",
            "email": "role@example.com",
            "roles": ["staff", "support"],
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_support)

    @override_config(WALDUR_AUTH_SOCIAL_ROLE_CLAIM="roles")
    def test_existing_user_assigned_roles_from_claims(self):
        user = structure_factories.UserFactory(is_staff=False, is_support=False)
        user_info = {
            "sub": user.username,
            "given_name": user.first_name,
            "family_name": user.last_name,
            "email": user.email,
            "roles": ["staff", "support"],
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_support)

    @override_config(WALDUR_AUTH_SOCIAL_ROLE_CLAIM="roles")
    def test_existing_user_roles_revoked(self):
        user = structure_factories.UserFactory(is_staff=True, is_support=True)
        user_info = {
            "sub": user.username,
            "given_name": user.first_name,
            "family_name": user.last_name,
            "email": user.email,
            "roles": [],
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_support)

    @override_config(WALDUR_AUTH_SOCIAL_ROLE_CLAIM="custom_roles")
    def test_custom_role_claim_name(self):
        user_info = {
            "sub": "test_custom_role",
            "given_name": "Custom",
            "family_name": "Role",
            "email": "custom@example.com",
            "custom_roles": ["staff", "support"],
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_support)

    @override_config(WALDUR_AUTH_SOCIAL_ROLE_CLAIM="roles")
    def test_invalid_role_claim_format(self):
        user_info = {
            "sub": "test_invalid_role",
            "given_name": "Invalid",
            "family_name": "Role",
            "email": "invalid@example.com",
            "roles": {"invalid": "format"},
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        with self.assertLogs("waldur_auth_social.utils", level="WARNING") as cm:
            response = self.client.get(
                self.url, {"state": self.state, "code": self.code}
            )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(
            any(
                "Roles claim roles is not a list or string" in output
                for output in cm.output
            )
        )

    @override_config(WALDUR_AUTH_SOCIAL_ROLE_CLAIM="")
    def test_empty_role_claim_name(self):
        # User is not staff/support initially
        user = structure_factories.UserFactory(is_staff=False, is_support=False)
        user_info = {
            "sub": user.username,
            "given_name": user.first_name,
            "family_name": user.last_name,
            "email": user.email,
            "roles": ["staff", "support"],  # These should be IGNORED
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user.refresh_from_db()
        # Should remain False because processing was skipped
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_support)

    @override_config(WALDUR_AUTH_SOCIAL_ROLE_CLAIM="roles")
    def test_configured_role_claim_missing_from_response(self):
        # User is staff initially
        user = structure_factories.UserFactory(is_staff=True, is_support=True)
        user_info = {
            "sub": user.username,
            "given_name": user.first_name,
            "family_name": user.last_name,
            "email": user.email,
            # "roles" claim is MISSING
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user.refresh_from_db()
        # Should remain True because processing was skipped (roles is None)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_support)


class MultiHomeportRedirectTest(test.APITransactionTestCase):
    """Tests for multi-homeport redirect functionality"""

    def setUp(self):
        super().setUp()
        self.provider = models.IdentityProvider.objects.create(
            provider=ProviderChoices.KEYCLOAK,
            client_id="test_client_id",
            client_secret="test_client_secret",
            discovery_url="http://keycloak.test/.well-known/openid-configuration",
            userinfo_url="http://keycloak.test/userinfo",
            token_url="http://keycloak.test/token",
            auth_url="http://keycloak.test/auth",
            allowed_redirects=[
                "https://homeport1.example.com",
                "https://homeport2.example.com",
            ],
            **PROVIDER_DEFAULTS[ProviderChoices.KEYCLOAK],
        )
        self.init_url = reverse(f"auth_{self.provider.provider}_init")
        self.complete_url = reverse(f"auth_{self.provider.provider}_complete")
        self.state = "test_state"
        self.code = "test_code"

        # Mock external requests
        responses.start()
        self.addCleanup(responses.stop)

    def _mock_token_request(
        self, access_token="test_access_token", refresh_token="test_refresh_token"
    ):
        return responses.add(
            method="POST",
            url=self.provider.token_url,
            json={"access_token": access_token, "refresh_token": refresh_token},
            status=status.HTTP_200_OK,
        )

    def _mock_userinfo_request(self, user_info):
        responses.add(
            method="GET",
            url=self.provider.userinfo_url,
            json=user_info,
            status=status.HTTP_200_OK,
        )

    def test_init_stores_referrer_in_session(self):
        """Test that the init endpoint stores the referrer URL in session"""
        response = self.client.get(
            self.init_url, HTTP_REFERER="https://homeport1.example.com/login"
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn(OIDC_REFERRER_KEY, self.client.session)
        self.assertEqual(
            self.client.session[OIDC_REFERRER_KEY],
            "https://homeport1.example.com/login",
        )

    def test_init_without_referrer(self):
        """Test that init endpoint works without a referrer header"""
        response = self.client.get(self.init_url)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertNotIn(OIDC_REFERRER_KEY, self.client.session)

    def test_redirect_to_valid_homeport_from_referrer(self):
        """Test successful redirect to a homeport that matches the stored referrer"""
        user_info = {
            "sub": "test_user",
            "given_name": "Test",
            "family_name": "User",
            "email": "test@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with state and referrer
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_REFERRER_KEY] = "https://homeport1.example.com/login"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(
            response.url.startswith(
                "https://homeport1.example.com/oauth_login_completed/"
            )
        )

    def test_redirect_to_second_allowed_homeport(self):
        """Test successful redirect to the second allowed homeport"""
        user_info = {
            "sub": "test_user2",
            "given_name": "Test",
            "family_name": "User",
            "email": "test2@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with state and referrer from second homeport
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_REFERRER_KEY] = "https://homeport2.example.com/auth"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(
            response.url.startswith(
                "https://homeport2.example.com/oauth_login_completed/"
            )
        )

    def test_redirect_blocked_for_unauthorized_homeport(self):
        """Test that redirect is blocked if referrer is not in allowed list"""
        user_info = {
            "sub": "test_user3",
            "given_name": "Test",
            "family_name": "User",
            "email": "test3@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with state and unauthorized referrer
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_REFERRER_KEY] = "https://unauthorized.example.com/login"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("not in the allowed redirects list", str(response.content))

    def test_redirect_without_referrer_uses_first_allowed_homeport(self):
        """Test that without referrer, the first allowed homeport is used"""
        user_info = {
            "sub": "test_user4",
            "given_name": "Test",
            "family_name": "User",
            "email": "test4@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with state but no referrer
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(
            response.url.startswith(
                "https://homeport1.example.com/oauth_login_completed/"
            )
        )

    @override_config(HOMEPORT_URL="https://fallback.example.com/")
    def test_fallback_to_homeport_url_when_no_allowed_redirects(self):
        """Test fallback to HOMEPORT_URL constance setting when allowed_redirects is empty"""
        # Create provider without allowed_redirects
        provider_no_allowed = models.IdentityProvider.objects.create(
            provider=ProviderChoices.TARA,
            client_id="tara_client_id",
            client_secret="tara_client_secret",
            discovery_url="https://tara.ria.ee/.well-known/openid-configuration",
            userinfo_url="https://tara.ria.ee/userinfo",
            token_url="https://tara.ria.ee/token",
            auth_url="https://tara.ria.ee/auth",
            allowed_redirects=[],  # Empty list
            **PROVIDER_DEFAULTS[ProviderChoices.TARA],
        )

        complete_url = reverse(f"auth_{provider_no_allowed.provider}_complete")
        user_info = {
            "sub": "EE12345678901",
            "given_name": "Test",
            "family_name": "User",
        }

        responses.add(
            method="POST",
            url=provider_no_allowed.token_url,
            json={"access_token": "token", "refresh_token": "refresh"},
            status=status.HTTP_200_OK,
        )
        responses.add(
            method="GET",
            url=provider_no_allowed.userinfo_url,
            json=user_info,
            status=status.HTTP_200_OK,
        )

        # Setup session
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session.save()

        response = self.client.get(
            complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(
            response.url.startswith(
                "https://fallback.example.com/oauth_login_completed/"
            )
        )

    def test_referrer_with_trailing_slash_matches_exactly(self):
        """Test that referrer URLs are matched exactly after normalization"""
        user_info = {
            "sub": "test_user5",
            "given_name": "Test",
            "family_name": "User",
            "email": "test5@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with referrer that has trailing slash - should be normalized
        # and match against allowed_redirects (which are stored without trailing slashes)
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_REFERRER_KEY] = "https://homeport1.example.com/"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(
            response.url.startswith(
                "https://homeport1.example.com/oauth_login_completed/"
            )
        )

    def test_referrer_with_path_is_validated_by_base_url(self):
        """Test that referrer with path components is validated by base URL only"""
        user_info = {
            "sub": "test_user6",
            "given_name": "Test",
            "family_name": "User",
            "email": "test6@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with referrer that has a path
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_REFERRER_KEY] = "https://homeport2.example.com/some/deep/path"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        # Should redirect to homeport2 base URL, not the deep path
        self.assertTrue(
            response.url.startswith(
                "https://homeport2.example.com/oauth_login_completed/"
            )
        )

    def test_return_url_parameter_in_init(self):
        """Test that return_url query parameter is stored in session"""
        response = self.client.get(
            self.init_url, {"return_url": "https://homeport1.example.com"}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn(OIDC_RETURN_URL_KEY, self.client.session)
        self.assertEqual(
            self.client.session[OIDC_RETURN_URL_KEY],
            "https://homeport1.example.com",
        )

    def test_return_url_takes_priority_over_referrer(self):
        """Test that return_url parameter takes priority over HTTP Referer header"""
        response = self.client.get(
            self.init_url,
            {"return_url": "https://homeport1.example.com"},
            HTTP_REFERER="https://homeport2.example.com/some/page",
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertIn(OIDC_RETURN_URL_KEY, self.client.session)
        self.assertEqual(
            self.client.session[OIDC_RETURN_URL_KEY],
            "https://homeport1.example.com",
        )
        # Referrer should NOT be stored when return_url is provided
        self.assertNotIn(OIDC_REFERRER_KEY, self.client.session)

    def test_redirect_using_return_url(self):
        """Test successful redirect using return_url parameter"""
        user_info = {
            "sub": "test_return_url_user",
            "given_name": "Test",
            "family_name": "User",
            "email": "test_return@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with return_url
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_RETURN_URL_KEY] = "https://homeport2.example.com"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(
            response.url.startswith(
                "https://homeport2.example.com/oauth_login_completed/"
            )
        )

    def test_return_url_blocked_if_not_in_allowed_list(self):
        """Test that return_url is validated against allowed_redirects"""
        user_info = {
            "sub": "test_invalid_return_url",
            "given_name": "Test",
            "family_name": "User",
            "email": "test_invalid@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with unauthorized return_url
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_RETURN_URL_KEY] = "https://malicious.com"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("not in the allowed redirects list", str(response.content))

    def test_return_url_with_path_validated_by_base(self):
        """Test that return_url with path is validated by base URL only"""
        user_info = {
            "sub": "test_return_url_path",
            "given_name": "Test",
            "family_name": "User",
            "email": "test_path@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with return_url that has a path
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_RETURN_URL_KEY] = "https://homeport1.example.com/deep/path"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        # Should redirect to homeport1 base URL
        self.assertTrue(
            response.url.startswith(
                "https://homeport1.example.com/oauth_login_completed/"
            )
        )

    def test_reject_javascript_scheme(self):
        """Test that javascript: scheme is rejected"""
        user_info = {
            "sub": "test_javascript",
            "given_name": "Test",
            "family_name": "User",
            "email": "test_js@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with javascript: scheme
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_RETURN_URL_KEY] = "javascript:alert('xss')"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("Invalid URL scheme", str(response.content))

    def test_reject_url_without_domain(self):
        """Test that URLs without domain are rejected"""
        user_info = {
            "sub": "test_nodomain",
            "given_name": "Test",
            "family_name": "User",
            "email": "test_nodomain@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with URL missing domain
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_RETURN_URL_KEY] = "https://"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("Missing domain", str(response.content))

    def test_case_insensitive_matching(self):
        """Test that URL matching is case-insensitive"""
        user_info = {
            "sub": "test_case",
            "given_name": "Test",
            "family_name": "User",
            "email": "test_case@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        # Setup session with mixed case URL
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session[OIDC_RETURN_URL_KEY] = "HTTPS://HOMEPORT1.EXAMPLE.COM"
        session.save()

        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )

        # Should succeed because matching is case-insensitive
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        # URL should be normalized to lowercase
        self.assertTrue(
            response.url.startswith(
                "https://homeport1.example.com/oauth_login_completed/"
            )
        )


class IdentityProviderValidationTest(test.APITestCase):
    """Tests for IdentityProvider serializer validation rules"""

    def setUp(self):
        super().setUp()
        self.serializer_class = IdentityProviderSerializer

    def test_reject_redirect_with_path(self):
        """Test that redirect URLs with paths are rejected"""
        serializer = self.serializer_class()
        with self.assertRaises(ValidationError) as context:
            serializer.validate_allowed_redirects(["https://example.com/login"])
        self.assertIn("must not contain a path", str(context.exception))

    def test_reject_redirect_with_query_params(self):
        """Test that redirect URLs with query parameters are rejected"""
        serializer = self.serializer_class()
        with self.assertRaises(ValidationError) as context:
            serializer.validate_allowed_redirects(["https://example.com?foo=bar"])
        self.assertIn("must not contain query parameters", str(context.exception))

    def test_reject_redirect_with_fragment(self):
        """Test that redirect URLs with fragments are rejected"""
        serializer = self.serializer_class()
        with self.assertRaises(ValidationError) as context:
            serializer.validate_allowed_redirects(["https://example.com#section"])
        self.assertIn("must not contain fragments", str(context.exception))

    def test_reject_http_non_localhost(self):
        """Test that HTTP URLs for non-localhost are rejected"""
        serializer = self.serializer_class()
        with self.assertRaises(ValidationError) as context:
            serializer.validate_allowed_redirects(["http://example.com"])
        self.assertIn("HTTPS required", str(context.exception))
        self.assertIn("HTTP is only allowed for localhost", str(context.exception))

    def test_allow_http_localhost(self):
        """Test that HTTP is allowed for localhost"""
        serializer = self.serializer_class()

        # Test various localhost formats
        localhost_urls = [
            "http://localhost",
            "http://localhost:8080",
            "http://127.0.0.1",
            "http://127.0.0.1:3000",
        ]

        result = serializer.validate_allowed_redirects(localhost_urls)
        self.assertEqual(len(result), 4)
        self.assertIn("http://localhost", result)
        self.assertIn("http://localhost:8080", result)
        self.assertIn("http://127.0.0.1", result)
        self.assertIn("http://127.0.0.1:3000", result)

    def test_redirect_url_normalization(self):
        """Test that redirect URLs with trailing slashes are normalized"""
        serializer = self.serializer_class()

        # URLs with trailing slashes should be normalized to without
        urls_with_trailing_slash = [
            "https://example.com/",
            "https://app.example.com/",
            "https://example.com:8443/",
        ]

        result = serializer.validate_allowed_redirects(urls_with_trailing_slash)
        self.assertEqual(len(result), 3)
        self.assertIn("https://example.com", result)
        self.assertIn("https://app.example.com", result)
        self.assertIn("https://example.com:8443", result)

        # Verify no trailing slashes in normalized URLs
        for url in result:
            self.assertFalse(url.endswith("/"))

    def test_allow_valid_https_urls(self):
        """Test that valid HTTPS URLs are accepted"""
        serializer = self.serializer_class()

        valid_urls = [
            "https://example.com",
            "https://app.example.com",
            "https://example.com:8443",
            "https://sub.domain.example.com",
        ]

        result = serializer.validate_allowed_redirects(valid_urls)
        self.assertEqual(len(result), 4)
        for url in valid_urls:
            self.assertIn(url, result)

    def test_reject_invalid_url_format(self):
        """Test that malformed URLs are rejected"""
        serializer = self.serializer_class()

        with self.assertRaises(ValidationError) as context:
            serializer.validate_allowed_redirects(["not-a-url"])
        self.assertIn("Enter a valid URL", str(context.exception))

    def test_reject_non_http_schemes(self):
        """Test that non-HTTP/HTTPS schemes are rejected"""
        serializer = self.serializer_class()

        with self.assertRaises(ValidationError) as context:
            serializer.validate_allowed_redirects(["ftp://example.com"])
        self.assertIn("Only http and https are allowed", str(context.exception))

    def test_reject_non_list_input(self):
        """Test that non-list input is rejected"""
        serializer = self.serializer_class()

        with self.assertRaises(ValidationError) as context:
            serializer.validate_allowed_redirects("https://example.com")
        self.assertIn("must be a list", str(context.exception))

    def test_reject_non_string_urls(self):
        """Test that non-string URLs in list are rejected"""
        serializer = self.serializer_class()

        with self.assertRaises(ValidationError) as context:
            serializer.validate_allowed_redirects([123, "https://example.com"])
        self.assertIn("must be a string", str(context.exception))

    def test_url_normalization_lowercase(self):
        """Test that URLs are normalized to lowercase for case-insensitive matching"""
        serializer = self.serializer_class()

        result = serializer.validate_allowed_redirects(
            ["https://Example.COM", "HTTPS://TEST.Example.COM:8443"]
        )
        # Should be lowercased
        self.assertEqual(result[0], "https://example.com")
        self.assertEqual(result[1], "https://test.example.com:8443")


class SchacPersonalUniqueIDParsingTest(test.APITestCase):
    """Test parsing of schacPersonalUniqueID to civil_number format."""

    def test_parse_estonian_schac_id(self):
        """Test parsing Estonian schacPersonalUniqueID."""
        # Estonian format: urn:schac:personalUniqueID:EE:EST:<id>
        value = "urn:schac:personalUniqueID:EE:EST:60001019906"
        result = parse_schac_personal_unique_id(value)
        self.assertEqual(result, "EE60001019906")

    def test_parse_finnish_schac_id(self):
        """Test parsing Finnish schacPersonalUniqueID."""
        value = "urn:schac:personalUniqueID:FI:FIN:010170-1234"
        result = parse_schac_personal_unique_id(value)
        self.assertEqual(result, "FI010170-1234")

    def test_passthrough_tara_format(self):
        """Test that TARA format (already normalized) passes through unchanged."""
        # TARA returns sub claim as country code + ID
        value = "EE60001019906"
        result = parse_schac_personal_unique_id(value)
        self.assertEqual(result, "EE60001019906")

    def test_passthrough_plain_id(self):
        """Test that plain ID without URN prefix passes through unchanged."""
        value = "60001019906"
        result = parse_schac_personal_unique_id(value)
        self.assertEqual(result, "60001019906")

    def test_parse_international_schac_id(self):
        """Test parsing international (int) schacPersonalUniqueID."""
        value = "urn:schac:personalUniqueID:int:orcid:0000-0001-2345-6789"
        result = parse_schac_personal_unique_id(value)
        self.assertEqual(result, "INT0000-0001-2345-6789")

    def test_lowercase_country_code_is_uppercased(self):
        """Test that lowercase country code is normalized to uppercase."""
        value = "urn:schac:personalUniqueID:lt:LTU:37510040173"
        result = parse_schac_personal_unique_id(value)
        self.assertEqual(result, "LT37510040173")


class EnabledUserProfileAttributesSyncTest(test.APITransactionTestCase):
    """Test that IdP sync respects ENABLED_USER_PROFILE_ATTRIBUTES setting."""

    def setUp(self):
        super().setUp()
        self.provider = models.IdentityProvider.objects.create(
            provider=ProviderChoices.KEYCLOAK,
            client_id="test_client_id",
            client_secret="test_client_secret",
            discovery_url="http://keycloak.test/.well-known/openid-configuration",
            userinfo_url="http://keycloak.test/userinfo",
            token_url="http://keycloak.test/token",
            auth_url="http://keycloak.test/auth",
            **PROVIDER_DEFAULTS[ProviderChoices.KEYCLOAK],
        )
        self.url = reverse(f"auth_{self.provider.provider}_complete")
        self.state = "test_state"
        self.code = "test_code"

        # Setup session
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session.save()

        # Mock external requests
        responses.start()
        self.addCleanup(responses.stop)

    def _mock_token_request(
        self, access_token="test_access_token", refresh_token="test_refresh_token"
    ):
        return responses.add(
            method="POST",
            url=self.provider.token_url,
            json={"access_token": access_token, "refresh_token": refresh_token},
            status=status.HTTP_200_OK,
        )

    def _mock_userinfo_request(self, user_info):
        responses.add(
            method="GET",
            url=self.provider.userinfo_url,
            json=user_info,
            status=status.HTTP_200_OK,
        )

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["phone_number", "organization"])
    def test_enabled_attributes_are_synced(self):
        """Test that enabled attributes are synced from IdP."""
        user_info = {
            "sub": "test_enabled",
            "given_name": "Test",
            "family_name": "User",
            "email": "enabled@example.com",
            "phone_number": "+1234567890",
            "schac_home_organization": "Test University",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertEqual(user.phone_number, "+1234567890")
        self.assertEqual(user.organization, "Test University")

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["organization"])
    def test_disabled_attributes_are_not_synced(self):
        """Test that disabled attributes are NOT synced from IdP."""
        user_info = {
            "sub": "test_disabled",
            "given_name": "Test",
            "family_name": "User",
            "email": "disabled@example.com",
            "phone_number": "+1234567890",  # phone_number is NOT enabled
            "schac_home_organization": "Test University",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        # phone_number should NOT be synced because it's not enabled
        self.assertEqual(user.phone_number, "")
        # organization should be synced because it's enabled
        self.assertEqual(user.organization, "Test University")

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=[])
    def test_core_attributes_synced_even_when_list_is_empty(self):
        """Test that core attributes (first_name, last_name, email) are always synced."""
        user_info = {
            "sub": "test_core",
            "given_name": "Core",
            "family_name": "User",
            "email": "core@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertEqual(user.first_name, "Core")
        self.assertEqual(user.last_name, "User")
        self.assertEqual(user.email, "core@example.com")

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["civil_number"])
    def test_civil_number_sync_when_enabled(self):
        """Test that civil_number is synced when enabled."""
        user_info = {
            "sub": "test_civil",
            "given_name": "Civil",
            "family_name": "User",
            "email": "civil@example.com",
            "schacPersonalUniqueID": "urn:schac:personalUniqueID:EE:EST:60001019906",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertEqual(user.civil_number, "EE60001019906")

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=[])
    def test_civil_number_not_synced_when_disabled(self):
        """Test that civil_number is NOT synced when disabled."""
        user_info = {
            "sub": "test_civil_disabled",
            "given_name": "Civil",
            "family_name": "User",
            "email": "civil_disabled@example.com",
            "schacPersonalUniqueID": "urn:schac:personalUniqueID:EE:EST:60001019906",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        # civil_number should NOT be synced (None or empty for new users)
        self.assertFalse(user.civil_number)

    @override_config(
        ENABLED_USER_PROFILE_ATTRIBUTES=["organization", "identity_source"]
    )
    def test_multiple_attributes_synced_selectively(self):
        """Test that multiple attributes are synced according to configuration."""
        user_info = {
            "sub": "test_multi",
            "given_name": "Multi",
            "family_name": "User",
            "email": "multi@example.com",
            "schac_home_organization": "Example University",  # maps to organization
            "phone_number": "+9876543210",  # NOT enabled
            "identity_source": "external_idp",  # enabled
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        # Enabled attributes
        self.assertEqual(user.organization, "Example University")
        self.assertEqual(user.identity_source, "external_idp")
        # Disabled attributes
        self.assertEqual(user.phone_number, "")

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["organization"])
    def test_existing_user_attribute_not_updated_when_disabled(self):
        """Test that existing user attributes are not updated when the attribute is disabled."""
        # Create user with existing phone number
        existing_user = structure_factories.UserFactory(
            username="test_existing",
            phone_number="+1111111111",
        )
        user_info = {
            "sub": "test_existing",
            "given_name": "Existing",
            "family_name": "User",
            "email": "existing@example.com",
            "phone_number": "+2222222222",  # NOT enabled, should not update
            "schac_home_organization": "New University",  # enabled, should update
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        existing_user.refresh_from_db()
        # phone_number should remain unchanged
        self.assertEqual(existing_user.phone_number, "+1111111111")
        # organization should be updated
        self.assertEqual(existing_user.organization, "New University")

    @override_config(
        ENABLED_USER_PROFILE_ATTRIBUTES=[
            "country_of_residence",
            "nationality",
            "organization_country",
        ]
    )
    def test_single_item_list_claims_are_unwrapped_for_scalar_fields(self):
        user_info = {
            "sub": "test_list_scalar",
            "given_name": "List",
            "family_name": "Scalar",
            "email": "list_scalar@example.com",
            "schacCountryOfResidence": ["EE"],
            "schacCountryOfCitizenship": ["EE"],
            "org_reg_country": ["EE"],
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertEqual(user.country_of_residence, "EE")
        self.assertEqual(user.nationality, "EE")
        self.assertEqual(user.organization_country, "EE")

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["organization_country"])
    def test_legacy_org_country_claim_is_used_as_fallback(self):
        user_info = {
            "sub": "test_legacy_org_country",
            "given_name": "Legacy",
            "family_name": "Claim",
            "email": "legacy_claim@example.com",
            "org_country": "EE",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertEqual(user.organization_country, "EE")

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["country_of_residence"])
    def test_multi_item_list_claims_are_skipped_for_scalar_fields(self):
        existing_user = structure_factories.UserFactory(
            username="test_multi_scalar",
            country_of_residence="LV",
        )
        user_info = {
            "sub": existing_user.username,
            "given_name": existing_user.first_name,
            "family_name": existing_user.last_name,
            "email": existing_user.email,
            "schacCountryOfResidence": ["EE", "FI"],
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        existing_user.refresh_from_db()
        self.assertEqual(existing_user.country_of_residence, "LV")

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["nationality"])
    def test_overlong_scalar_claims_are_skipped(self):
        user_info = {
            "sub": "test_overlong_scalar",
            "given_name": "Overlong",
            "family_name": "User",
            "email": "overlong@example.com",
            # Alpha-3 value should not be written to alpha-2 field.
            "schacCountryOfCitizenship": ["EST"],
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertEqual(user.nationality, "")

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["gender"])
    def test_uppercase_gender_claim_is_stored_as_lowercase(self):
        """IdPs may return gender as 'MALE'/'FEMALE' — must be lowercased before save."""
        user_info = {
            "sub": "test_gender_case",
            "given_name": "Test",
            "family_name": "User",
            "email": "gender_case@example.com",
            "gender": "MALE",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertEqual(user.gender, "male")

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["gender"])
    def test_invalid_gender_claim_is_skipped(self):
        """Unrecognised gender values must not be written to the user."""
        user_info = {
            "sub": "test_gender_invalid",
            "given_name": "Test",
            "family_name": "User",
            "email": "gender_invalid@example.com",
            "gender": "enigmatic",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertIsNone(user.gender)


class OIDCEmailMatchmakingTest(test.APITransactionTestCase):
    """Tests for OIDC email-based failover user matching."""

    def setUp(self):
        super().setUp()
        self.provider = models.IdentityProvider.objects.create(
            provider=ProviderChoices.KEYCLOAK,
            client_id="test_client_id",
            client_secret="test_client_secret",
            discovery_url="http://keycloak.test/.well-known/openid-configuration",
            userinfo_url="http://keycloak.test/userinfo",
            token_url="http://keycloak.test/token",
            auth_url="http://keycloak.test/auth",
            **PROVIDER_DEFAULTS[ProviderChoices.KEYCLOAK],
        )
        self.url = reverse(f"auth_{self.provider.provider}_complete")
        self.state = "test_state"
        self.code = "test_code"

        # Setup session
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session.save()

        # Mock external requests
        responses.start()
        self.addCleanup(responses.stop)

    def _mock_token_request(self):
        return responses.add(
            method="POST",
            url=self.provider.token_url,
            json={
                "access_token": "test_access_token",
                "refresh_token": "test_refresh_token",
            },
            status=status.HTTP_200_OK,
        )

    def _mock_userinfo_request(self, user_info):
        responses.add(
            method="GET",
            url=self.provider.userinfo_url,
            json=user_info,
            status=status.HTTP_200_OK,
        )

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_email_matchmaking_matches_user_by_email(self):
        """Pre-provisioned user is matched by email and username is updated."""
        existing_user = structure_factories.UserFactory(
            username="old_username",
            email="user@example.com",
        )
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "user@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        existing_user.refresh_from_db()
        self.assertEqual(existing_user.username, "new_oidc_sub")
        self.assertEqual(User.objects.count(), 1)

    def test_email_matchmaking_disabled_by_default(self):
        """Setting off -> new user created, no email match."""
        structure_factories.UserFactory(
            username="old_username",
            email="user@example.com",
        )
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "user@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(User.objects.count(), 2)
        self.assertTrue(User.objects.filter(username="new_oidc_sub").exists())

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_email_matchmaking_duplicate_emails_raises_error(self):
        """Multiple users with same email -> OAuthException."""
        structure_factories.UserFactory(
            username="user1",
            email="duplicate@example.com",
        )
        structure_factories.UserFactory(
            username="user2",
            email="duplicate@example.com",
        )
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "duplicate@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("Multiple users found with the same email", str(response.content))

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_email_matchmaking_deactivated_user_raises_error(self):
        """Deactivated user matched by email -> OAuthException."""
        structure_factories.UserFactory(
            username="old_username",
            email="user@example.com",
            is_active=False,
        )
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "user@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("User is deactivated", str(response.content))

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_email_matchmaking_no_email_in_payload_skips(self):
        """No email claim -> falls through to creation."""
        structure_factories.UserFactory(
            username="old_username",
            email="user@example.com",
        )
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "Test",
            "family_name": "User",
            # no email claim
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(User.objects.count(), 2)

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_email_matchmaking_skipped_when_user_field_is_email(self):
        """user_field='email' -> skip failover, use direct lookup."""
        self.provider.user_field = "email"
        self.provider.user_claim = "email"
        self.provider.save()

        structure_factories.UserFactory(
            username="old_username",
            email="user@example.com",
        )
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "user@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        # Should have matched via primary lookup (email field)
        self.assertEqual(User.objects.count(), 1)

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_email_matchmaking_case_insensitive(self):
        """Email case mismatch still matches."""
        existing_user = structure_factories.UserFactory(
            username="old_username",
            email="User@Example.COM",
        )
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "user@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        existing_user.refresh_from_db()
        self.assertEqual(existing_user.username, "new_oidc_sub")
        self.assertEqual(User.objects.count(), 1)

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_email_matchmaking_no_match_creates_user(self):
        """Email doesn't match any user -> normal creation."""
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "nomatch@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(User.objects.filter(username="new_oidc_sub").exists())

    @override_config(
        OIDC_MATCHMAKING_BY_EMAIL=True,
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
    )
    def test_email_matchmaking_with_uninvited_blocking(self):
        """Both settings on, email matches -> user matched (not blocked)."""
        existing_user = structure_factories.UserFactory(
            username="old_username",
            email="user@example.com",
        )
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "user@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        existing_user.refresh_from_db()
        self.assertEqual(existing_user.username, "new_oidc_sub")
        self.assertEqual(User.objects.count(), 1)

    @override_config(
        OIDC_MATCHMAKING_BY_EMAIL=True,
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
    )
    def test_email_matchmaking_no_match_still_blocks_uninvited(self):
        """Both settings on, no match -> creation blocked."""
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "Test",
            "family_name": "User",
            "email": "uninvited@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )

    @override_config(OIDC_MATCHMAKING_BY_EMAIL=True)
    def test_email_matchmaking_updates_attributes(self):
        """Matched user gets attribute updates from OIDC payload."""
        existing_user = structure_factories.UserFactory(
            username="old_username",
            email="user@example.com",
            first_name="OldFirst",
            last_name="OldLast",
        )
        user_info = {
            "sub": "new_oidc_sub",
            "given_name": "NewFirst",
            "family_name": "NewLast",
            "email": "user@example.com",
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        existing_user.refresh_from_db()
        self.assertEqual(existing_user.username, "new_oidc_sub")
        self.assertEqual(existing_user.first_name, "NewFirst")
        self.assertEqual(existing_user.last_name, "NewLast")


class OIDCAllowedEmailPatternsTest(test.APITransactionTestCase):
    """Tests for the OIDC_ALLOWED_USER_EMAIL_PATTERNS allowlist.

    The allowlist widens signup beyond invitations and, once configured, also
    gates every login of an already existing account.
    """

    def setUp(self):
        super().setUp()
        self.provider = models.IdentityProvider.objects.create(
            provider=ProviderChoices.KEYCLOAK,
            client_id="test_client_id",
            client_secret="test_client_secret",
            discovery_url="http://keycloak.test/.well-known/openid-configuration",
            userinfo_url="http://keycloak.test/userinfo",
            token_url="http://keycloak.test/token",
            auth_url="http://keycloak.test/auth",
            **PROVIDER_DEFAULTS[ProviderChoices.KEYCLOAK],
        )
        self.url = reverse(f"auth_{self.provider.provider}_complete")
        self.state = "test_state"
        self.code = "test_code"

        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session.save()

        responses.start()
        self.addCleanup(responses.stop)

    def _mock_token_request(self):
        return responses.add(
            method="POST",
            url=self.provider.token_url,
            json={
                "access_token": "test_access_token",
                "refresh_token": "test_refresh_token",
            },
            status=status.HTTP_200_OK,
        )

    def _mock_userinfo_request(self, user_info):
        responses.add(
            method="GET",
            url=self.provider.userinfo_url,
            json=user_info,
            status=status.HTTP_200_OK,
        )

    def _login(self, user_info):
        self._mock_token_request()
        self._mock_userinfo_request(user_info)
        return self.client.get(self.url, {"state": self.state, "code": self.code})

    # --- Signup path ---

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_creation_is_allowed_if_email_matches_allowlist(self):
        user_info = {
            "sub": "allowed_user",
            "given_name": "Allowed",
            "family_name": "User",
            "email": "someone@example.com",
        }

        response = self._login(user_info)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(User.objects.filter(username="allowed_user").exists())

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_creation_is_blocked_if_email_only_partially_matches_allowlist(self):
        """The pattern must match the whole email, not just its beginning."""
        user_info = {
            "sub": "lookalike_user",
            "given_name": "Look",
            "family_name": "Alike",
            "email": "attacker@example.com.attacker.net",
        }

        response = self._login(user_info)

        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )
        self.assertFalse(User.objects.filter(username="lookalike_user").exists())

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_allowlist_match_is_case_insensitive(self):
        user_info = {
            "sub": "mixed_case_user",
            "given_name": "Mixed",
            "family_name": "Case",
            "email": "Someone@EXAMPLE.CoM",
        }

        response = self._login(user_info)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(User.objects.filter(username="mixed_case_user").exists())

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=["*broken"],
    )
    def test_invalid_allowlist_pattern_never_allows_creation(self):
        """A pattern that does not compile denies rather than admits."""
        user_info = {
            "sub": "broken_pattern_user",
            "given_name": "Broken",
            "family_name": "Pattern",
            "email": "aaa@example.com",
        }

        response = self._login(user_info)

        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )
        self.assertFalse(User.objects.filter(username="broken_pattern_user").exists())

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r"(a+)+@example\.com"],
    )
    def test_redos_prone_allowlist_pattern_never_allows_creation(self):
        """A pattern rejected as ReDoS-prone denies rather than admits."""
        user_info = {
            "sub": "dangerous_pattern_user",
            "given_name": "Dangerous",
            "family_name": "Pattern",
            "email": "aaa@example.com",
        }

        response = self._login(user_info)

        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )
        self.assertFalse(
            User.objects.filter(username="dangerous_pattern_user").exists()
        )

    @override_config(OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"])
    def test_allowlist_is_inert_while_blocking_is_disabled(self):
        """With the master toggle off, signup stays open to everybody."""
        user_info = {
            "sub": "open_signup_user",
            "given_name": "Open",
            "family_name": "Signup",
            "email": "someone@otherdomain.com",
        }

        response = self._login(user_info)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(User.objects.filter(username="open_signup_user").exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_creation_is_allowed_if_autoprovisioning_rule_matches(self):
        autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".*@example\.com"],
            plan=None,
        )
        user_info = {
            "sub": "autoprovisioned_user",
            "given_name": "Auto",
            "family_name": "Provisioned",
            "email": "someone@example.com",
        }

        response = self._login(user_info)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(User.objects.filter(username="autoprovisioned_user").exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_creation_is_blocked_if_autoprovisioning_rule_does_not_match(self):
        autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".*@example\.com"],
            plan=None,
        )
        user_info = {
            "sub": "unmatched_user",
            "given_name": "Unmatched",
            "family_name": "User",
            "email": "someone@otherdomain.com",
        }

        response = self._login(user_info)

        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )
        self.assertFalse(User.objects.filter(username="unmatched_user").exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_creation_is_blocked_if_autoprovisioning_rule_has_no_filters(self):
        """An unconfigured rule must not admit everybody."""
        autoprovisioning_factories.RuleFactory(plan=None)
        user_info = {
            "sub": "unfiltered_rule_user",
            "given_name": "Unfiltered",
            "family_name": "Rule",
            "email": "someone@otherdomain.com",
        }

        response = self._login(user_info)

        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )
        self.assertFalse(User.objects.filter(username="unfiltered_rule_user").exists())

    # --- Login path ---

    def _user_info_for(self, user):
        return {
            "sub": user.username,
            "given_name": user.first_name,
            "family_name": user.last_name,
            "email": user.email,
        }

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_existing_user_login_is_blocked_if_email_does_not_match(self):
        user = structure_factories.UserFactory(email="someone@otherdomain.com")

        response = self._login(self._user_info_for(user))

        assert_login_failed_redirect(
            self, response, "Access to this deployment is restricted."
        )

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
        OIDC_BLOCKED_LOGIN_RESPONSE_MESSAGE="Ask your administrator for access.",
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS_RESPONSE_MESSAGE="Signup is closed.",
    )
    def test_blocked_login_uses_the_login_specific_message(self):
        """An existing user must not be told their account cannot be created."""
        user = structure_factories.UserFactory(email="someone@otherdomain.com")

        response = self._login(self._user_info_for(user))

        assert_login_failed_redirect(
            self, response, "Ask your administrator for access."
        )

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_blocked_login_leaves_the_account_intact(self):
        user = structure_factories.UserFactory(
            email="someone@otherdomain.com",
            first_name="Original",
        )

        self._login({**self._user_info_for(user), "given_name": "Updated"})

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(user.first_name, "Original")

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_existing_user_login_is_allowed_if_email_matches(self):
        user = structure_factories.UserFactory(email="someone@example.com")

        response = self._login(self._user_info_for(user))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_staff_and_support_are_exempt_from_the_login_gate(self):
        for kwargs in ({"is_staff": True}, {"is_support": True}):
            with self.subTest(**kwargs):
                user = structure_factories.UserFactory(
                    email="someone@otherdomain.com", **kwargs
                )

                response = self._login(self._user_info_for(user))

                self.assertEqual(response.status_code, status.HTTP_302_FOUND)

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_user_holding_a_role_is_exempt_from_the_login_gate(self):
        user = structure_factories.UserFactory(email="someone@otherdomain.com")
        project = structure_factories.ProjectFactory()
        project.add_user(user, ProjectRole.ADMIN)

        response = self._login(self._user_info_for(user))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_user_matching_an_autoprovisioning_rule_is_exempt_from_the_login_gate(self):
        user = structure_factories.UserFactory(email="someone@otherdomain.com")
        autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".*@otherdomain\.com"],
            plan=None,
        )

        response = self._login(self._user_info_for(user))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_group_invitation_exempts_from_the_login_gate(self):
        user_factories.CustomerGroupInvitationFactory(
            user_email_patterns=[r".*@otherdomain\.com"],
            is_active=True,
        )
        user = structure_factories.UserFactory(email="someone@otherdomain.com")

        response = self._login(self._user_info_for(user))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_group_invitation_does_not_exempt_a_lookalike_domain(self):
        """The invitation exemption is anchored the same way the allowlist is."""
        user_factories.CustomerGroupInvitationFactory(
            user_email_patterns=[r".*@otherdomain\.com"],
            is_active=True,
        )
        user = structure_factories.UserFactory(
            email="attacker@otherdomain.com.attacker.net"
        )

        response = self._login(self._user_info_for(user))

        assert_login_failed_redirect(
            self, response, "Access to this deployment is restricted."
        )

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_pending_invitation_exempts_from_the_login_gate(self):
        """An invited user must be able to log in *in order to* accept the invitation.

        They hold no role until they accept, so without this exemption they are
        admitted at signup and locked out on every subsequent login.
        """
        user = structure_factories.UserFactory(email="invited@otherdomain.com")
        user_factories.ProjectInvitationFactory(
            email=user.email,
            scope=structure_factories.ProjectFactory(),
            state=InvitationState.PENDING,
        )

        response = self._login(self._user_info_for(user))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_login_gate_uses_the_incoming_email(self):
        """A user the provider moved into the allowlist must not stay locked out.

        The stored address is only refreshed further down the login flow, so
        judging on it alone would be self-perpetuating.
        """
        user = structure_factories.UserFactory(email="mover@otherdomain.com")

        response = self._login(
            {**self._user_info_for(user), "email": "mover@example.com"}
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user.refresh_from_db()
        self.assertEqual(user.email, "mover@example.com")

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
        WALDUR_AUTH_SOCIAL_ROLE_CLAIM="roles",
    )
    def test_incoming_staff_role_claim_exempts_from_the_login_gate(self):
        """The is_staff flag is only written after the gate runs."""
        user = structure_factories.UserFactory(email="operator@otherdomain.com")
        self.assertFalse(user.is_staff)

        response = self._login({**self._user_info_for(user), "roles": ["staff"]})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user.refresh_from_db()
        self.assertTrue(user.is_staff)

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_expired_role_does_not_exempt_from_the_login_gate(self):
        """Roles awaiting the expiration sweeper must not grant a grace window."""
        user = structure_factories.UserFactory(email="former@otherdomain.com")
        project = structure_factories.ProjectFactory()
        permission = project.add_user(user, ProjectRole.ADMIN)
        UserRole.objects.filter(pk=permission.pk).update(
            expiration_time=timezone.now() - timedelta(days=1)
        )

        response = self._login(self._user_info_for(user))

        assert_login_failed_redirect(
            self, response, "Access to this deployment is restricted."
        )

    @override_config(
        OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True,
        OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"],
    )
    def test_background_sync_is_not_gated(self):
        """The login policy must not break non-interactive identity sync."""
        user = structure_factories.UserFactory(
            username="synced_user",
            email="synced@otherdomain.com",
            first_name="Old",
        )

        synced, created = create_or_update_oauth_user(
            self.provider,
            {
                "sub": "synced_user",
                "given_name": "New",
                "family_name": user.last_name,
                "email": user.email,
            },
            is_interactive_login=False,
        )

        self.assertFalse(created)
        self.assertEqual(synced.pk, user.pk)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "New")

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_autoprovisioning_rule_email_pattern_must_match_whole_email(self):
        """The rule allow path is anchored just like the allowlist itself."""
        autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".*@example\.com"],
            plan=None,
        )
        user_info = {
            "sub": "rule_lookalike_user",
            "given_name": "Rule",
            "family_name": "Lookalike",
            "email": "attacker@example.com.attacker.net",
        }

        response = self._login(user_info)

        assert_login_failed_redirect(
            self, response, "Account creation is blocked for uninvited users."
        )
        self.assertFalse(User.objects.filter(username="rule_lookalike_user").exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_autoprovisioning_rule_matches_on_another_filter(self):
        """A non-strict email match must not veto a rule that matches otherwise."""
        autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".*@example\.com"],
            user_affiliations=["faculty"],
            plan=None,
        )
        self.provider.attribute_mapping = {
            **self.provider.attribute_mapping,
            "affiliations": "voperson_external_affiliation",
        }
        self.provider.save()
        user_info = {
            "sub": "affiliated_user",
            "given_name": "Affiliated",
            "family_name": "User",
            "email": "attacker@example.com.attacker.net",
            "voperson_external_affiliation": ["faculty"],
        }

        response = self._login(user_info)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertTrue(User.objects.filter(username="affiliated_user").exists())

    @override_config(OIDC_BLOCK_CREATION_OF_UNINVITED_USERS=True)
    def test_login_gate_is_inert_while_the_allowlist_is_empty(self):
        """Deployments using only the signup toggle keep their previous behaviour."""
        user = structure_factories.UserFactory(email="someone@otherdomain.com")

        response = self._login(self._user_info_for(user))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

    @override_config(OIDC_ALLOWED_USER_EMAIL_PATTERNS=[r".*@example\.com"])
    def test_login_gate_is_inert_while_blocking_is_disabled(self):
        user = structure_factories.UserFactory(email="someone@otherdomain.com")

        response = self._login(self._user_info_for(user))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
