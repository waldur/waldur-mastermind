import time
from unittest import mock

import jwt
import responses
from django.core.cache import cache
from django.db import connection
from django.test import TestCase

from waldur_auth_social import oauth_tokens, token_cache
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
