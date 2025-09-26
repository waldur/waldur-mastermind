from unittest import mock
from urllib.parse import parse_qs, urlparse

import responses
from constance.test.unittest import override_config
from rest_framework import status, test
from rest_framework.authtoken.models import Token
from rest_framework.reverse import reverse

from waldur_auth_social import models
from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
from waldur_auth_social.views import OIDC_CODE_VERIFIER_KEY, OIDC_STATE_KEY
from waldur_core.core.models import User
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.enums import InvitationState
from waldur_core.users.tests import factories as user_factories


class OAuthViewInitTest(test.APITransactionTestCase):
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
        self.assertIn(
            "Account creation is blocked for uninvited users.", str(response.content)
        )
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
