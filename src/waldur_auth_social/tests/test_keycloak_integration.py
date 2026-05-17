import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import responses
from django.utils import timezone
from rest_framework import status, test
from rest_framework.authtoken.models import Token
from rest_framework.reverse import reverse

from waldur_auth_social import models
from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
from waldur_core.core.models import TokenExchangeCode, User


class KeycloakTokenExchangeTest(test.APITransactionTestCase):
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
        self.complete_url = reverse(f"auth_{self.provider.provider}_complete")
        self.exchange_url = reverse("auth-token-exchange")
        self.state = "test_state"
        self.code = "test_code"

        # Setup session
        session = self.client.session
        session["oidc_state"] = self.state
        session.save()

        # Mock external requests
        responses.start()
        self.addCleanup(responses.stop)

    def _mock_oidc_responses(self, user_info):
        responses.add(
            method="POST",
            url=self.provider.token_url,
            json={
                "access_token": "test_access_token",
                "refresh_token": "test_refresh_token",
            },
            status=status.HTTP_200_OK,
        )
        responses.add(
            method="GET",
            url=self.provider.userinfo_url,
            json=user_info,
            status=status.HTTP_200_OK,
        )

    def test_full_keycloak_login_flow(self):
        user_info = {
            "sub": "keycloak_user_1",
            "given_name": "Key",
            "family_name": "Cloak",
            "email": "keycloak@example.com",
        }
        self._mock_oidc_responses(user_info)

        # 1. Complete OAuth login
        response = self.client.get(
            self.complete_url, {"state": self.state, "code": self.code}
        )
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

        # 2. Extract exchange code from redirect URL
        parsed_url = urlparse(response.url)
        exchange_code_uuid = parse_qs(parsed_url.query)["code"][0]

        # Verify it's in the DB
        self.assertTrue(
            TokenExchangeCode.objects.filter(uuid=exchange_code_uuid).exists()
        )

        # 3. Exchange code for token
        exchange_response = self.client.post(
            self.exchange_url, {"code": exchange_code_uuid}
        )
        self.assertEqual(exchange_response.status_code, status.HTTP_200_OK)

        token_key = exchange_response.data["token"]
        user = User.objects.get(username=user_info["sub"])
        self.assertTrue(Token.objects.filter(user=user, key=token_key).exists())

        # 4. Verify code is deleted after exchange
        self.assertFalse(
            TokenExchangeCode.objects.filter(uuid=exchange_code_uuid).exists()
        )

    def test_exchange_code_expires(self):
        user = User.objects.create(username="expired_user")
        token, _ = Token.objects.get_or_create(user=user)
        exchange_code = TokenExchangeCode.objects.create(user=user, token=token)

        TokenExchangeCode.objects.filter(pk=exchange_code.pk).update(
            created=timezone.now() - timedelta(seconds=60)
        )

        response = self.client.post(self.exchange_url, {"code": exchange_code.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Generic error to avoid confirming code existence
        self.assertEqual(response.data["detail"], "Invalid or expired exchange code.")
        # Expired code is consumed regardless of staleness
        self.assertFalse(TokenExchangeCode.objects.filter(pk=exchange_code.pk).exists())

    def test_invalid_exchange_code(self):
        response = self.client.post(self.exchange_url, {"code": uuid.uuid4().hex})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "Invalid or expired exchange code.")

    def test_exchange_code_is_single_use(self):
        user = User.objects.create(username="replay_user")
        token, _ = Token.objects.get_or_create(user=user)
        exchange_code = TokenExchangeCode.objects.create(user=user, token=token)

        first = self.client.post(self.exchange_url, {"code": exchange_code.uuid.hex})
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["token"], token.key)

        second = self.client.post(self.exchange_url, {"code": exchange_code.uuid.hex})
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    def test_external_token_path(self):
        user = User.objects.create(username="oidc_user")
        exchange_code = TokenExchangeCode.objects.create(
            user=user, external_token="external-oidc-access-token"
        )

        response = self.client.post(self.exchange_url, {"code": exchange_code.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["token"], "external-oidc-access-token")
        self.assertFalse(TokenExchangeCode.objects.filter(pk=exchange_code.pk).exists())

    def test_cleanup_task_removes_stale_codes(self):
        from waldur_core.core.tasks import cleanup_stale_token_exchange_codes

        user = User.objects.create(username="stale_user")
        token, _ = Token.objects.get_or_create(user=user)
        fresh = TokenExchangeCode.objects.create(user=user, token=token)
        stale = TokenExchangeCode.objects.create(user=user, token=token)
        TokenExchangeCode.objects.filter(pk=stale.pk).update(
            created=timezone.now() - timedelta(minutes=10)
        )

        cleanup_stale_token_exchange_codes()

        self.assertTrue(TokenExchangeCode.objects.filter(pk=fresh.pk).exists())
        self.assertFalse(TokenExchangeCode.objects.filter(pk=stale.pk).exists())
