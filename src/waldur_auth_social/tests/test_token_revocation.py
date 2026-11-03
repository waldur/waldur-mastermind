import time
from datetime import timedelta
from urllib.parse import parse_qs

import jwt
import responses
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_auth_social import oauth_tokens, tasks
from waldur_auth_social.models import IdentityProvider, OAuthToken
from waldur_core.configuration.testing import override_config
from waldur_core.core.models import User
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event

BASE_URL = "https://keycloak.example.com/realms/hpc/protocol/openid-connect"
DISCOVERY_URL = (
    "https://keycloak.example.com/realms/hpc/.well-known/openid-configuration"
)
REVOCATION_URL = f"{BASE_URL}/revoke"
FIRECREST = "https://firecrest.example.org"


def session_token(session_id):
    """A Keycloak-style refresh token naming its session."""
    return jwt.encode({"typ": "Offline", "sid": session_id}, "k" * 32, "HS256")


@override_config(FIRECREST_ALLOWED_API_URLS=FIRECREST)
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
            user=self.user, provider="keycloak", refresh_token="offline-1"
        )

    def tearDown(self):
        cache.clear()

    def discovery(self, **document):
        responses.get(DISCOVERY_URL, json={"issuer": "x", **document})

    def logout(self, user=None):
        self.client.force_authenticate(user or self.user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("auth-logout"))

    def revoked_reasons(self):
        return [
            event.context["reason"]
            for event in Event.objects.filter(
                event_type=EventType.OIDC_TOKEN_REVOKED
            ).order_by("id")
        ]

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
        access_token = jwt.encode(
            {"exp": int(time.time()) + 3600}, "k" * 32, algorithm="HS256"
        )
        oauth_tokens.cache_access_token(self.user, access_token)

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
        # Access tokens minted from it are unusable at once, not at expiry.
        self.assertIsNone(oauth_tokens.cached_access_token(self.user))
        self.assertEqual(self.revoked_reasons(), ["logout"])

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
        OAuthToken.objects.create(user=other, provider="keycloak", refresh_token="r")

        self.logout(other)

        discovery_calls = [c for c in responses.calls if c.request.url == DISCOVERY_URL]
        self.assertEqual(len(discovery_calls), 1)

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
        self.assertEqual(self.revoked_reasons(), ["deactivated"])

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

    def store_refresh_token(self, refresh_token):
        token = OAuthToken.objects.get(user=self.user)
        token.refresh_token = refresh_token
        token.save()

    def sign_in(self, refresh_token, provider="keycloak"):
        with self.captureOnCommitCallbacks(execute=True):
            oauth_tokens.store_tokens(self.user, provider, "access", refresh_token)

    @responses.activate
    def test_sign_in_in_another_session_revokes_the_replaced_token(self):
        self.discovery(revocation_endpoint=REVOCATION_URL)
        responses.post(REVOCATION_URL, status=200)
        old = session_token("session-1")
        self.store_refresh_token(old)

        new = session_token("session-2")
        self.sign_in(new)

        self.assertEqual(OAuthToken.objects.get(user=self.user).refresh_token, new)
        self.assertEqual([r["token"] for r in self.revocation_requests()], [[old]])
        self.assertEqual(self.revoked_reasons(), ["replaced"])

    @responses.activate
    def test_sign_in_in_the_same_session_does_not_revoke(self):
        # Keycloak would end the session, and the new token with it.
        self.store_refresh_token(session_token("session-1"))

        self.sign_in(session_token("session-1"))

        self.assertEqual(len(responses.calls), 0)
        self.assertEqual(self.revoked_reasons(), [])

    @responses.activate
    def test_opaque_token_of_the_same_provider_is_not_revoked(self):
        # No session id to tell whether it is the same session.
        self.sign_in("offline-2")

        self.assertEqual(len(responses.calls), 0)
        self.assertEqual(
            OAuthToken.objects.get(user=self.user).refresh_token, "offline-2"
        )

    @responses.activate
    def test_sign_in_through_another_provider_revokes_the_old_token(self):
        self.discovery(revocation_endpoint=REVOCATION_URL)
        responses.post(REVOCATION_URL, status=200)

        self.sign_in("eduteams-1", provider="eduteams")

        self.assertEqual(
            [r["token"] for r in self.revocation_requests()], [["offline-1"]]
        )
        self.assertEqual(OAuthToken.objects.get(user=self.user).provider, "eduteams")

    @responses.activate
    def test_sign_in_while_the_hpc_workspace_is_off_removes_the_token(self):
        self.discovery(revocation_endpoint=REVOCATION_URL)
        responses.post(REVOCATION_URL, status=200)
        old = session_token("session-1")
        self.store_refresh_token(old)

        with override_config(FIRECREST_ALLOWED_API_URLS=""):
            self.sign_in(session_token("session-2"))

        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())
        self.assertEqual([r["token"] for r in self.revocation_requests()], [[old]])

    @responses.activate
    def test_idle_tokens_are_revoked(self):
        self.discovery(revocation_endpoint=REVOCATION_URL)
        responses.post(REVOCATION_URL, status=200)
        self.user.last_login = timezone.now() - timedelta(days=31)
        self.user.save(update_fields=["last_login"])
        active = User.objects.create_user("bob", "bob@example.com", "pw")
        active.last_login = timezone.now()
        active.save(update_fields=["last_login"])
        OAuthToken.objects.create(user=active, provider="keycloak", refresh_token="r")

        with self.captureOnCommitCallbacks(execute=True):
            tasks.cleanup_stale_oauth_tokens()

        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())
        self.assertTrue(OAuthToken.objects.filter(user=active).exists())
        self.assertEqual(
            [r["token"] for r in self.revocation_requests()], [["offline-1"]]
        )
        self.assertEqual(self.revoked_reasons(), ["idle"])

    @responses.activate
    def test_all_tokens_are_revoked_once_the_hpc_workspace_is_off(self):
        self.discovery(revocation_endpoint=REVOCATION_URL)
        responses.post(REVOCATION_URL, status=200)
        self.user.last_login = timezone.now()
        self.user.save(update_fields=["last_login"])

        with override_config(FIRECREST_ALLOWED_API_URLS=""):
            with self.captureOnCommitCallbacks(execute=True):
                tasks.cleanup_stale_oauth_tokens()

        self.assertFalse(OAuthToken.objects.exists())
        self.assertEqual(len(self.revocation_requests()), 1)
        self.assertEqual(self.revoked_reasons(), ["disabled"])
