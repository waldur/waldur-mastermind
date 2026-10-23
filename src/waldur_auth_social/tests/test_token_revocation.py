from urllib.parse import parse_qs

import responses
from django.core.cache import cache
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_auth_social.models import IdentityProvider, OAuthToken
from waldur_core.core.models import User

BASE_URL = "https://keycloak.example.com/realms/hpc/protocol/openid-connect"
DISCOVERY_URL = (
    "https://keycloak.example.com/realms/hpc/.well-known/openid-configuration"
)
REVOCATION_URL = f"{BASE_URL}/revoke"


class TokenRevocationTest(test.APITestCase):
    """Stored identity provider tokens go when the user logs out or leaves.

    Revocation waits for the commit, so each action runs inside
    captureOnCommitCallbacks(execute=True).
    """

    def setUp(self):
        cache.clear()
        IdentityProvider.objects.create(
            provider="keycloak",
            client_id="waldur",
            client_secret="secret",
            discovery_url=DISCOVERY_URL,
            userinfo_url=f"{BASE_URL}/userinfo",
            token_url=f"{BASE_URL}/token",
            auth_url=f"{BASE_URL}/auth",
            logout_url=f"{BASE_URL}/logout",
        )
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        OAuthToken.objects.create(
            user=self.user,
            provider="keycloak",
            access_token="access-1",
            refresh_token="offline-1",
        )

    def tearDown(self):
        cache.clear()

    def discovery(self, **document):
        responses.get(DISCOVERY_URL, json={"issuer": "x", **document})

    def logout(self, user=None):
        self.client.force_authenticate(user or self.user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("auth-logout"))

    def revocation_requests(self):
        return [
            parse_qs(call.request.body)
            for call in responses.calls
            if call.request.url == REVOCATION_URL
        ]

    @responses.activate
    def test_logout_revokes_and_removes_the_refresh_token(self):
        self.discovery(revocation_endpoint=REVOCATION_URL)
        responses.post(REVOCATION_URL, status=200)

        response = self.logout()

        self.assertIn(
            response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT)
        )
        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())
        self.assertEqual(
            self.revocation_requests(),
            [
                {
                    "token": ["offline-1"],
                    "token_type_hint": ["refresh_token"],
                    "client_id": ["waldur"],
                    "client_secret": ["secret"],
                }
            ],
        )

    @responses.activate
    def test_logout_succeeds_when_the_provider_refuses_revocation(self):
        self.discovery(revocation_endpoint=REVOCATION_URL)
        responses.post(REVOCATION_URL, status=503)

        response = self.logout()

        self.assertIn(
            response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT)
        )
        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())

    @responses.activate
    def test_logout_succeeds_when_the_discovery_document_is_unreachable(self):
        responses.get(DISCOVERY_URL, status=502)

        response = self.logout()

        self.assertIn(
            response.status_code, (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT)
        )
        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())
        self.assertEqual(self.revocation_requests(), [])

    @responses.activate
    def test_provider_without_revocation_endpoint_only_removes_the_token(self):
        self.discovery()

        self.logout()

        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())
        self.assertEqual(self.revocation_requests(), [])

    @responses.activate
    def test_missing_revocation_endpoint_is_cached(self):
        self.discovery()
        self.logout()
        other = User.objects.create_user("bob", "bob@example.com", "pw")
        OAuthToken.objects.create(
            user=other, provider="keycloak", access_token="a", refresh_token="r"
        )

        self.logout(other)

        discovery_calls = [c for c in responses.calls if c.request.url == DISCOVERY_URL]
        self.assertEqual(len(discovery_calls), 1)

    @responses.activate
    def test_access_token_is_revoked_when_there_is_no_refresh_token(self):
        OAuthToken.objects.filter(user=self.user).update(refresh_token="")
        self.discovery(revocation_endpoint=REVOCATION_URL)
        responses.post(REVOCATION_URL, status=200)

        self.logout()

        self.assertEqual(
            [r["token_type_hint"] for r in self.revocation_requests()],
            [["access_token"]],
        )

    @responses.activate
    def test_local_user_without_stored_token_logs_out(self):
        local = User.objects.create_user("carol", "carol@example.com", "pw")

        response = self.logout(local)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(len(responses.calls), 0)
        # Only the user who logged out loses their stored token.
        self.assertTrue(OAuthToken.objects.filter(user=self.user).exists())

    @responses.activate
    def test_deactivation_revokes_and_removes_the_token(self):
        self.discovery(revocation_endpoint=REVOCATION_URL)
        responses.post(REVOCATION_URL, status=200)

        self.user.is_active = False
        with self.captureOnCommitCallbacks(execute=True):
            self.user.save()

        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())
        self.assertEqual(len(self.revocation_requests()), 1)

    @responses.activate
    def test_other_updates_keep_the_token(self):
        self.user.full_name = "Alice Researcher"
        with self.captureOnCommitCallbacks(execute=True):
            self.user.save()

        self.assertTrue(OAuthToken.objects.filter(user=self.user).exists())
        self.assertEqual(len(responses.calls), 0)

    @responses.activate
    def test_deletion_revokes_the_token(self):
        self.discovery(revocation_endpoint=REVOCATION_URL)
        responses.post(REVOCATION_URL, status=200)

        with self.captureOnCommitCallbacks(execute=True):
            self.user.delete()

        self.assertFalse(OAuthToken.objects.exists())
        self.assertEqual(len(self.revocation_requests()), 1)
