import time

import jwt
import responses
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.cache import cache
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_auth_social import models
from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
from waldur_auth_social.id_token import jwks_cache_key
from waldur_auth_social.tests.oidc_helpers import (
    SIGNING_KEY_ID,
    issuer_of,
    jwks_url_of,
    make_id_token,
    mock_signing_keys,
    public_jwk,
)
from waldur_auth_social.views import OIDC_STATE_KEY
from waldur_core.core.models import User

OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class IdTokenValidationTest(test.APITestCase):
    provider_type = ProviderChoices.KEYCLOAK
    base_url = "http://keycloak.test"

    def setUp(self):
        super().setUp()
        cache.clear()
        self.provider = models.IdentityProvider.objects.create(
            provider=self.provider_type,
            client_id="test_client_id",
            client_secret="test_client_secret",
            discovery_url=f"{self.base_url}/.well-known/openid-configuration",
            userinfo_url=f"{self.base_url}/userinfo",
            token_url=f"{self.base_url}/token",
            auth_url=f"{self.base_url}/auth",
            **PROVIDER_DEFAULTS[self.provider_type],
        )
        self.url = reverse(f"auth_{self.provider.provider}_complete")
        self.state = "test_state"
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session.save()

        responses.start()
        self.addCleanup(responses.reset)
        self.addCleanup(responses.stop)

        self.sub = "subject-1"
        self.user_info = {
            "sub": self.sub,
            "given_name": "Alice",
            "family_name": "Doe",
            "email": "alice@example.com",
        }

    def _mock_token(self, id_token):
        body = {"access_token": "access", "refresh_token": "refresh"}
        if id_token is not None:
            body["id_token"] = id_token
        responses.add(
            method="POST",
            url=self.provider.token_url,
            json=body,
            status=status.HTTP_200_OK,
        )

    def _mock_userinfo(self):
        responses.add(
            method="GET",
            url=self.provider.userinfo_url,
            json=self.user_info,
            status=status.HTTP_200_OK,
        )

    def _login(self, id_token, keys=None):
        self.users_before = User.objects.count()
        jwks_mock = mock_signing_keys(self.provider, keys=keys)
        self._mock_token(id_token)
        self._mock_userinfo()
        response = self.client.get(self.url, {"state": self.state, "code": "code"})
        return response, jwks_mock

    def _assert_rejected(self, response):
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(User.objects.count(), self.users_before)

    def test_valid_id_token_logs_user_in(self):
        response, _ = self._login(make_id_token(self.provider, self.sub))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(User.objects.count(), self.users_before + 1)

    def test_missing_id_token_is_rejected(self):
        response, _ = self._login(None)
        self._assert_rejected(response)

    def test_signature_by_another_key_is_rejected(self):
        token = make_id_token(self.provider, self.sub, private_key=OTHER_KEY)
        response, _ = self._login(token)
        self._assert_rejected(response)

    def test_unknown_key_id_is_rejected(self):
        token = make_id_token(self.provider, self.sub, kid="unknown")
        response, _ = self._login(token)
        self._assert_rejected(response)

    def test_wrong_issuer_is_rejected(self):
        token = make_id_token(self.provider, self.sub, iss="https://evil.example.com")
        response, _ = self._login(token)
        self._assert_rejected(response)

    def test_wrong_audience_is_rejected(self):
        token = make_id_token(self.provider, self.sub, aud="another_client")
        response, _ = self._login(token)
        self._assert_rejected(response)

    def test_expired_token_is_rejected(self):
        now = int(time.time())
        token = make_id_token(self.provider, self.sub, iat=now - 600, exp=now - 300)
        response, _ = self._login(token)
        self._assert_rejected(response)

    def test_token_without_expiry_is_rejected(self):
        token = make_id_token(self.provider, self.sub, exp=None)
        response, _ = self._login(token)
        self._assert_rejected(response)

    def test_symmetric_algorithm_is_rejected(self):
        payload = jwt.decode(
            make_id_token(self.provider, self.sub),
            options={"verify_signature": False},
        )
        token = jwt.encode(
            payload,
            "test_client_secret",
            algorithm="HS256",
            headers={"kid": SIGNING_KEY_ID},
        )
        response, _ = self._login(token)
        self._assert_rejected(response)

    def test_user_info_subject_must_match_id_token(self):
        token = make_id_token(self.provider, "another-subject")
        response, _ = self._login(token)
        self._assert_rejected(response)

    def test_discovery_metadata_is_filled_on_first_login(self):
        self._login(make_id_token(self.provider, self.sub))

        self.provider.refresh_from_db()
        self.assertEqual(self.provider.issuer, issuer_of(self.provider))
        self.assertEqual(self.provider.jwks_url, jwks_url_of(self.provider))

    def test_signing_keys_are_cached(self):
        _, jwks_mock = self._login(make_id_token(self.provider, self.sub))
        self.client.logout()
        session = self.client.session
        session[OIDC_STATE_KEY] = self.state
        session.save()

        response, _ = self._login(make_id_token(self.provider, self.sub))

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(jwks_mock.call_count, 1)

    def test_rotated_key_is_picked_up_without_admin_action(self):
        # The cached key set only knows the old key.
        self.provider.issuer = issuer_of(self.provider)
        self.provider.jwks_url = jwks_url_of(self.provider)
        self.provider.save()
        cache.set(jwks_cache_key(self.provider), {"keys": [public_jwk()]})
        new_keys = [public_jwk(), public_jwk(OTHER_KEY, kid="rotated")]
        token = make_id_token(
            self.provider, self.sub, private_key=OTHER_KEY, kid="rotated"
        )

        response, jwks_mock = self._login(token, keys=new_keys)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(jwks_mock.call_count, 1)

    def test_signing_key_is_fetched_with_ssl_setting_of_provider(self):
        self.provider.verify_ssl = False
        self.provider.save()

        _, jwks_mock = self._login(make_id_token(self.provider, self.sub))

        self.assertEqual(jwks_mock.call_count, 1)
        self.assertFalse(jwks_mock.calls[0].request.req_kwargs["verify"])


class TaraIdTokenValidationTest(IdTokenValidationTest):
    provider_type = ProviderChoices.TARA
    base_url = "https://tara-test.ria.ee"

    def setUp(self):
        super().setUp()
        self.sub = "EE60001019906"
        self.user_info = {
            "sub": self.sub,
            "given_name": "Mari",
            "family_name": "Maasikas",
        }

    def test_low_level_of_assurance_is_rejected(self):
        token = make_id_token(self.provider, self.sub, acr="low")
        response, _ = self._login(token)
        self._assert_rejected(response)

    def test_missing_level_of_assurance_is_rejected(self):
        token = make_id_token(self.provider, self.sub, acr=None)
        response, _ = self._login(token)
        self._assert_rejected(response)

    def test_substantial_level_of_assurance_is_accepted(self):
        token = make_id_token(self.provider, self.sub, acr="substantial")
        response, _ = self._login(token)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
