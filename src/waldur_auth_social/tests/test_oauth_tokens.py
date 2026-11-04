import time
from unittest import mock

import jwt
import responses
from django.core.cache import cache
from django.db import connection
from django.test import TestCase

from waldur_auth_social import oauth_tokens, revocation, token_cache
from waldur_auth_social.models import IdentityProvider, OAuthToken
from waldur_core.configuration.testing import override_config
from waldur_core.core import encryption
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories

FIRECREST = "https://firecrest.example.org"
TOKEN_URL = "https://idp.example.org/token"
DISCOVERY_URL = "https://idp.example.org/.well-known/openid-configuration"


def make_token(expires_in=3600):
    return jwt.encode({"exp": int(time.time()) + expires_in}, "k" * 32, "HS256")


def raw_value(model, column, pk):
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {column} FROM {model._meta.db_table} WHERE id = %s",  # noqa: S608
            [pk],
        )
        return cursor.fetchone()[0]


class TokenTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = structure_factories.UserFactory()
        self.provider = IdentityProvider.objects.create(
            provider="keycloak",
            client_id="waldur",
            client_secret="client-secret",
            discovery_url=DISCOVERY_URL,
            token_url=TOKEN_URL,
            userinfo_url="https://idp.example.org/userinfo",
            auth_url="https://idp.example.org/auth",
            label="Keycloak",
        )

    def store(self, refresh_token="refresh-1", access_token=None):
        oauth_tokens.store_tokens(
            self.user, "keycloak", access_token or make_token(), refresh_token
        )


@override_config(FIRECREST_ALLOWED_API_URLS=FIRECREST)
class EncryptionAtRestTest(TokenTestBase):
    def test_refresh_token_and_client_secret_are_ciphertext_in_the_database(self):
        self.store("refresh-secret")
        token = OAuthToken.objects.get(user=self.user)

        stored = raw_value(OAuthToken, "refresh_token", token.pk)
        self.assertNotIn("refresh-secret", stored)
        self.assertTrue(encryption.is_encrypted(stored))
        self.assertEqual(token.refresh_token, "refresh-secret")

        secret = raw_value(IdentityProvider, "client_secret", self.provider.pk)
        self.assertTrue(encryption.is_encrypted(secret))
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.client_secret, "client-secret")

    def test_access_token_is_cached_encrypted_and_not_stored(self):
        access_token = make_token()
        self.store(access_token=access_token)

        self.assertFalse(
            any(f.name == "access_token" for f in OAuthToken._meta.get_fields())
        )
        cached = cache.get(token_cache._cache_key(self.user.pk))
        self.assertNotEqual(cached, access_token)
        self.assertEqual(oauth_tokens.get_access_token(self.user), access_token)


class StoragePolicyTest(TokenTestBase):
    @override_config(
        FIRECREST_ALLOWED_API_URLS="",
        SLURM_MONITOR_ALLOWED_API_URLS="https://monitor.example.org/api/v2",
    )
    def test_a_cluster_monitor_alone_needs_the_token(self):
        # A read-only workspace reads slurm-monitor with it.
        self.store("refresh-1")
        self.assertEqual(
            OAuthToken.objects.get(user=self.user).refresh_token, "refresh-1"
        )

    @override_config(FIRECREST_ALLOWED_API_URLS="")
    def test_nothing_is_kept_while_the_hpc_workspace_is_off(self):
        OAuthToken.objects.create(
            user=self.user, provider="keycloak", refresh_token="old"
        )
        self.store()
        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())
        self.assertIsNone(oauth_tokens.cached_access_token(self.user))

    @override_config(FIRECREST_ALLOWED_API_URLS=FIRECREST)
    def test_signing_in_through_another_provider_replaces_the_token(self):
        self.store("refresh-1")
        oauth_tokens.store_tokens(self.user, "eduteams", make_token(), "refresh-2")
        token = OAuthToken.objects.get(user=self.user)
        self.assertEqual(token.provider, "eduteams")
        self.assertEqual(token.refresh_token, "refresh-2")


@override_config(FIRECREST_ALLOWED_API_URLS=FIRECREST)
class RefreshTest(TokenTestBase):
    @responses.activate
    def test_cached_token_is_used_without_asking_the_identity_provider(self):
        access_token = make_token()
        self.store(access_token=access_token)
        self.assertEqual(oauth_tokens.get_access_token(self.user), access_token)
        self.assertEqual(len(responses.calls), 0)

    @responses.activate
    def test_expiring_token_is_refreshed_and_rotated_token_saved_encrypted(self):
        self.store("refresh-1", access_token=make_token(expires_in=10))
        fresh = make_token()
        responses.post(
            TOKEN_URL, json={"access_token": fresh, "refresh_token": "refresh-2"}
        )

        self.assertEqual(oauth_tokens.get_access_token(self.user), fresh)
        token = OAuthToken.objects.get(user=self.user)
        self.assertEqual(token.refresh_token, "refresh-2")
        self.assertTrue(
            encryption.is_encrypted(raw_value(OAuthToken, "refresh_token", token.pk))
        )
        # The new access token is cached: no second refresh.
        self.assertEqual(oauth_tokens.get_access_token(self.user), fresh)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_token_refreshed_by_another_worker_meanwhile_is_reused(self):
        self.store()
        cache.clear()
        fresh = make_token()
        # The first look misses; by the time this worker holds the row lock,
        # another one has cached a fresh token.
        with mock.patch.object(
            oauth_tokens, "cached_access_token", side_effect=[None, fresh]
        ):
            self.assertEqual(oauth_tokens.get_access_token(self.user), fresh)
        self.assertEqual(len(responses.calls), 0)

    @responses.activate
    def test_refused_refresh_token_is_dropped_and_logged(self):
        self.store()
        cache.clear()
        responses.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})

        with self.assertRaisesMessage(
            oauth_tokens.TokenUnavailable, "Sign in again"
        ) as raised:
            oauth_tokens.get_access_token(self.user)
        self.assertEqual(raised.exception.sign_in_provider, "keycloak")
        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.OIDC_TOKEN_REFRESH_FAILED
            ).exists()
        )
        event = Event.objects.get(event_type=EventType.OIDC_TOKEN_REVOKED)
        self.assertEqual(event.context["reason"], "rejected")
        # The provider refused the token already: nothing to revoke there.
        self.assertEqual([c.request.url for c in responses.calls], [TOKEN_URL])

    @responses.activate
    def test_refresh_refused_for_another_reason_keeps_the_token(self):
        # A client secret rotated at the provider but not yet in Waldur.
        self.store()
        cache.clear()
        responses.post(TOKEN_URL, status=401, json={"error": "invalid_client"})

        with self.assertRaisesMessage(
            oauth_tokens.TokenUnavailable, "did not refresh"
        ) as raised:
            oauth_tokens.get_access_token(self.user)
        self.assertIsNone(raised.exception.sign_in_provider)
        self.assertTrue(OAuthToken.objects.filter(user=self.user).exists())
        self.assertFalse(
            Event.objects.filter(event_type=EventType.OIDC_TOKEN_REVOKED).exists()
        )

    @responses.activate
    def test_identity_provider_outage_keeps_the_token(self):
        self.store()
        cache.clear()
        responses.post(TOKEN_URL, status=503)

        with self.assertRaises(oauth_tokens.TokenUnavailable):
            oauth_tokens.get_access_token(self.user)
        self.assertTrue(OAuthToken.objects.filter(user=self.user).exists())


@override_config(FIRECREST_ALLOWED_API_URLS=FIRECREST)
class TokenExchangeTest(TokenTestBase):
    @responses.activate
    def test_token_is_exchanged_for_the_audience_and_cached_per_audience(self):
        sign_in = make_token()
        self.store(access_token=sign_in)
        exchanged = make_token()
        responses.post(TOKEN_URL, json={"access_token": exchanged})

        token = oauth_tokens.get_access_token(self.user, audience="firecrest")

        self.assertEqual(token, exchanged)
        body = responses.calls[0].request.body
        self.assertIn(
            "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange",
            body,
        )
        self.assertIn(f"subject_token={sign_in}", body)
        self.assertIn("audience=firecrest", body)
        # The client scope defaults to the audience.
        self.assertIn("scope=firecrest", body)
        self.assertIn("client_id=waldur", body)
        # Cached per audience; the sign-in token is unchanged.
        self.assertEqual(
            oauth_tokens.get_access_token(self.user, audience="firecrest"), exchanged
        )
        self.assertEqual(oauth_tokens.get_access_token(self.user), sign_in)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_explicit_scope_is_requested(self):
        self.store()
        responses.post(TOKEN_URL, json={"access_token": make_token()})
        oauth_tokens.get_access_token(
            self.user, audience="firecrest", scope="firecrest-audience"
        )
        self.assertIn("scope=firecrest-audience", responses.calls[0].request.body)

    @responses.activate
    def test_refused_exchange_explains_without_dropping_the_token(self):
        self.store()
        responses.post(
            TOKEN_URL,
            status=400,
            json={
                "error": "invalid_request",
                "error_description": "Requested audience not available: firecrest",
            },
        )
        with self.assertRaisesMessage(
            oauth_tokens.TokenUnavailable, "token exchange setup"
        ):
            oauth_tokens.get_access_token(self.user, audience="firecrest")
        self.assertTrue(OAuthToken.objects.filter(user=self.user).exists())

    @responses.activate
    def test_exchanged_tokens_are_cached_per_scope(self):
        self.store()
        responses.post(TOKEN_URL, json={"access_token": make_token()})
        oauth_tokens.get_access_token(self.user, audience="firecrest", scope="a")
        oauth_tokens.get_access_token(self.user, audience="firecrest", scope="b")
        oauth_tokens.get_access_token(self.user, audience="firecrest", scope="a")
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_refused_sign_in_token_is_refreshed_and_exchanged_again(self):
        # The cached sign-in token outlived its session at the provider.
        self.store("refresh-1")
        fresh, exchanged = make_token(), make_token()
        responses.post(TOKEN_URL, status=400, json={"error": "invalid_token"})
        responses.post(
            TOKEN_URL, json={"access_token": fresh, "refresh_token": "refresh-2"}
        )
        responses.post(TOKEN_URL, json={"access_token": exchanged})

        token = oauth_tokens.get_access_token(self.user, audience="firecrest")

        self.assertEqual(token, exchanged)
        self.assertIn(f"subject_token={fresh}", responses.calls[2].request.body)

    @responses.activate
    def test_ended_session_asks_to_sign_in_again(self):
        self.store()
        responses.post(TOKEN_URL, status=400, json={"error": "invalid_token"})
        responses.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})

        with self.assertRaisesMessage(
            oauth_tokens.TokenUnavailable, "Sign in again"
        ) as raised:
            oauth_tokens.get_access_token(self.user, audience="firecrest")
        self.assertEqual(raised.exception.sign_in_provider, "keycloak")
        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())

    @responses.activate
    def test_revocation_forgets_exchanged_tokens(self):
        self.store()
        responses.post(TOKEN_URL, json={"access_token": make_token()})
        oauth_tokens.get_access_token(self.user, audience="firecrest")
        revocation.forget_oauth_tokens(self.user, revocation.RevocationReason.LOGOUT)
        self.assertIsNone(
            oauth_tokens.cached_access_token(self.user, "firecrest|firecrest")
        )
