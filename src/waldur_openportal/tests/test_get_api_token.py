import httpx
import respx
from django.core.cache import cache
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_auth_social.models import IdentityProvider
from waldur_core.core.models import User

DISCOVERY_URL = "http://oidc.example.com/.well-known/openid-configuration"
INTROSPECTION_URL = "http://oidc.example.com/introspect"


class GetApiTokenTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        IdentityProvider.objects.create(
            provider="keycloak",
            client_id="client",
            client_secret="secret",
            discovery_url=DISCOVERY_URL,
        )
        self.user = User.objects.create_user(
            username="alice@example.com", email="alice@example.com"
        )
        self.url = "/api/openportal/get_api_token/"

    @respx.mock
    def test_logged_out_user_gets_a_fresh_token(self):
        """Logout deletes the DRF token; the Bearer exchange must mint a new one
        instead of rejecting the user for not having an active session."""
        Token.objects.filter(user=self.user).delete()

        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(
                200, json={"introspection_endpoint": INTROSPECTION_URL}
            )
        )
        respx.post(INTROSPECTION_URL).mock(
            return_value=httpx.Response(
                200, json={"active": True, "email": "alice@example.com"}
            )
        )

        response = self.client.get(self.url, HTTP_AUTHORIZATION="Bearer some-idp-token")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        token = Token.objects.get(user=self.user)
        self.assertEqual(response.json()["token"], token.key)
