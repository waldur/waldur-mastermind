import base64
import json
from urllib.parse import parse_qs

import responses
from django.core.cache import cache
from django.test import TestCase

from waldur_auth_social import discovery
from waldur_auth_social.const import ProviderChoices, TokenEndpointAuthMethod
from waldur_auth_social.models import IdentityProvider

DISCOVERY_URL = "https://idp.example.org/.well-known/openid-configuration"
TOKEN_URL = "https://idp.example.org/token"

POST = TokenEndpointAuthMethod.CLIENT_SECRET_POST
BASIC = TokenEndpointAuthMethod.CLIENT_SECRET_BASIC


def basic_header(client_id, client_secret):
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


class DiscoveryTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.idp = IdentityProvider.objects.create(
            provider=ProviderChoices.KEYCLOAK,
            client_id="waldur",
            client_secret="secret",
            discovery_url=DISCOVERY_URL,
            token_url=TOKEN_URL,
        )

    def tearDown(self):
        cache.clear()

    def advertise(self, methods):
        document = {"issuer": "https://idp.example.org"}
        if methods is not None:
            document["token_endpoint_auth_methods_supported"] = methods
        responses.get(DISCOVERY_URL, json=document)

    def discovery_calls(self):
        return [c for c in responses.calls if c.request.url == DISCOVERY_URL]

    def token_calls(self):
        return [c for c in responses.calls if c.request.url == TOKEN_URL]


class TokenEndpointAuthMethodTest(DiscoveryTestBase):
    @responses.activate
    def test_method_follows_what_the_provider_advertises(self):
        cases = [
            # TARA
            ([BASIC], BASIC),
            # Unity IdM / EUDAT B2ACCESS: omitted, so the OIDC default applies
            (None, BASIC),
            # Keycloak, eduTEAMS
            ([POST, BASIC, "client_secret_jwt", "private_key_jwt"], POST),
            ([POST], POST),
            # Neither is supported: keep sending them in the body, as before.
            (["private_key_jwt"], POST),
            # Not a list: treated as omitted.
            ("client_secret_basic", BASIC),
        ]
        for methods, expected in cases:
            with self.subTest(methods=methods):
                cache.clear()
                responses.reset()
                self.advertise(methods)
                self.assertEqual(
                    discovery.token_endpoint_auth_method(self.idp), expected
                )

    @responses.activate
    def test_without_any_document_the_previous_choice_applies(self):
        responses.get(DISCOVERY_URL, status=502)
        tara = IdentityProvider(
            provider=ProviderChoices.TARA, discovery_url=DISCOVERY_URL
        )

        self.assertEqual(discovery.token_endpoint_auth_method(self.idp), POST)
        self.assertEqual(discovery.token_endpoint_auth_method(tara), BASIC)

    @responses.activate
    def test_unreadable_document_is_not_asked_for_again_for_a_while(self):
        responses.get(DISCOVERY_URL, status=502)

        self.assertIsNone(discovery.get_discovery_document(self.idp))
        self.assertIsNone(discovery.get_discovery_document(self.idp))
        self.assertEqual(len(self.discovery_calls()), 1)

    @responses.activate
    def test_last_known_document_is_used_while_discovery_is_unreachable(self):
        self.advertise([BASIC])
        self.assertEqual(discovery.token_endpoint_auth_method(self.idp), BASIC)

        # The hour is up and the discovery endpoint is down.
        cache.delete(discovery._cache_key("discovery", self.idp))
        responses.reset()
        responses.get(DISCOVERY_URL, status=502)

        self.assertEqual(discovery.token_endpoint_auth_method(self.idp), BASIC)
        self.assertEqual(len(self.discovery_calls()), 1)

    @responses.activate
    def test_document_that_is_not_an_object_is_unreadable(self):
        responses.get(DISCOVERY_URL, json=["client_secret_basic"])

        self.assertIsNone(discovery.get_discovery_document(self.idp))

    @responses.activate
    def test_document_is_cached_until_forgotten(self):
        self.advertise([BASIC])

        discovery.token_endpoint_auth_method(self.idp)
        discovery.token_endpoint_auth_method(self.idp)
        self.assertEqual(len(self.discovery_calls()), 1)

        discovery.forget_discovery_document(self.idp)
        discovery.token_endpoint_auth_method(self.idp)
        self.assertEqual(len(self.discovery_calls()), 2)


class ClientAuthTest(DiscoveryTestBase):
    def test_post_puts_credentials_in_the_body(self):
        self.assertEqual(
            discovery.client_auth(self.idp, {"code": "c"}, POST),
            ({"code": "c", "client_id": "waldur", "client_secret": "secret"}, None),
        )

    def test_basic_leaves_the_body_alone(self):
        self.assertEqual(
            discovery.client_auth(self.idp, {"code": "c"}, BASIC),
            ({"code": "c"}, ("waldur", "secret")),
        )

    @responses.activate
    def test_basic_credentials_are_form_urlencoded(self):
        # RFC 6749 2.3.1; also keeps characters outside Latin-1 from failing
        # in requests' own Basic encoding.
        self.idp.client_id = "waldur:client"
        self.idp.client_secret = "p+s%w:ö rd"
        self.advertise([BASIC])
        responses.post(TOKEN_URL, json={"access_token": "a"})

        discovery.post_with_client_auth(self.idp, TOKEN_URL, {"code": "c"}, 5)

        [call] = self.token_calls()
        self.assertEqual(
            call.request.headers["Authorization"],
            basic_header("waldur%3Aclient", "p%2Bs%25w%3A%C3%B6+rd"),
        )


class RetryWithOtherMethodTest(DiscoveryTestBase):
    def accept_only(self, method, rejection=(401, {"error": "invalid_client"})):
        """A token endpoint that accepts the client only with *method*."""

        def callback(request):
            sent_basic = "Authorization" in request.headers
            body = parse_qs(request.body)
            accepted = (
                sent_basic
                if method == BASIC
                else not sent_basic and body.get("client_secret") == ["secret"]
            )
            if accepted:
                return 200, {}, json.dumps({"access_token": "a"})
            status, payload = rejection
            return status, {}, json.dumps(payload)

        responses.add_callback(responses.POST, TOKEN_URL, callback=callback)

    def post(self):
        return discovery.post_with_client_auth(
            self.idp, TOKEN_URL, {"code": "c"}, timeout=5
        )

    @responses.activate
    def test_rejected_client_is_retried_with_the_other_method_and_remembered(self):
        # Advertises the body, accepts only Basic.
        self.advertise([POST, BASIC])
        self.accept_only(BASIC)

        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(len(self.token_calls()), 2)

        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(len(self.token_calls()), 3)
        self.assertEqual(discovery.token_endpoint_auth_method(self.idp), BASIC)

    @responses.activate
    def test_invalid_client_in_a_400_is_retried(self):
        self.advertise(None)
        self.accept_only(POST, rejection=(400, {"error": "invalid_client"}))

        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(discovery.token_endpoint_auth_method(self.idp), POST)

    @responses.activate
    def test_other_errors_are_not_retried(self):
        self.advertise([BASIC])
        responses.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})

        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(len(self.token_calls()), 1)

    @responses.activate
    def test_client_rejected_both_ways_returns_the_first_answer(self):
        # A wrong client secret, say.
        self.advertise([BASIC])
        responses.post(TOKEN_URL, status=401, json={"error": "first"})
        responses.post(TOKEN_URL, status=401, json={"error": "second"})

        response = self.post()

        self.assertEqual(response.json(), {"error": "first"})
        self.assertEqual(len(self.token_calls()), 2)
        self.assertEqual(discovery.token_endpoint_auth_method(self.idp), BASIC)

    def use_special_secret(self):
        self.idp.client_secret = "p+s%w:ö rd"
        self.raw_header = basic_header("waldur", "p+s%w:ö rd")

    @responses.activate
    def test_unencoded_basic_is_tried_when_the_encoded_form_is_rejected(self):
        # A server that does not decode RFC 6749 form-urlencoding (werkzeug).
        self.use_special_secret()
        self.advertise([BASIC])

        def callback(request):
            if request.headers.get("Authorization") == self.raw_header:
                return 200, {}, json.dumps({"access_token": "a"})
            return 401, {}, json.dumps({"error": "invalid_client"})

        responses.add_callback(responses.POST, TOKEN_URL, callback=callback)

        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(len(self.token_calls()), 2)
        self.assertEqual(
            discovery.token_endpoint_auth_method(self.idp),
            discovery.CLIENT_SECRET_BASIC_UNENCODED,
        )

        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(len(self.token_calls()), 3)

    @responses.activate
    def test_special_secret_rejected_every_way_returns_the_first_answer(self):
        self.use_special_secret()
        self.advertise([BASIC])
        responses.post(TOKEN_URL, status=401, json={"error": "first"})
        responses.post(TOKEN_URL, status=401, json={"error": "second"})
        responses.post(TOKEN_URL, status=400, json={"error": "invalid_client"})

        response = self.post()

        self.assertEqual(response.json(), {"error": "first"})
        headers = [c.request.headers.get("Authorization") for c in self.token_calls()]
        self.assertEqual(
            headers,
            [
                basic_header("waldur", "p%2Bs%25w%3A%C3%B6+rd"),
                self.raw_header,
                None,
            ],
        )

    @responses.activate
    def test_forgetting_the_provider_drops_the_remembered_method(self):
        self.advertise([POST, BASIC])
        self.accept_only(BASIC)
        self.post()

        discovery.forget_discovery_document(self.idp)

        self.assertEqual(discovery.token_endpoint_auth_method(self.idp), POST)
