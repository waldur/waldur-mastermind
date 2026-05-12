from unittest import mock
from urllib.parse import parse_qs, urlparse

import responses
from constance.test.unittest import override_config
from rest_framework import status, test
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from rest_framework.reverse import reverse

from waldur_auth_social import models
from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
from waldur_auth_social.serializers import IdentityProviderSerializer
from waldur_auth_social.utils import parse_schac_personal_unique_id
from waldur_auth_social.views import (
    OIDC_CODE_VERIFIER_KEY,
    OIDC_REFERRER_KEY,
    OIDC_RETURN_URL_KEY,
    OIDC_STATE_KEY,
)
from waldur_core.core.models import User
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.enums import InvitationState
from waldur_core.users.tests import factories as user_factories


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

        # Check token in redirect URL
        parsed_url = urlparse(response.url)
        token_key = parse_qs(parsed_url.query)["token"][0]
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
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("It is blocked", str(response.content))
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
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn(
            "Account creation is blocked for uninvited users.", str(response.content)
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
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn(
            "User email is not provided. Account creation is blocked.",
            str(response.content),
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
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
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
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(User.objects.filter(username=user_info["sub"]).exists())

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
            "org_country": ["EE"],
        }
        self._mock_token_request()
        self._mock_userinfo_request(user_info)

        response = self.client.get(self.url, {"state": self.state, "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        user = User.objects.get(username=user_info["sub"])
        self.assertEqual(user.country_of_residence, "EE")
        self.assertEqual(user.nationality, "EE")
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

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn(
            "Account creation is blocked for uninvited users.", str(response.content)
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
