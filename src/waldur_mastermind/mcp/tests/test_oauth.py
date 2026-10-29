import base64
import hashlib
from datetime import timedelta
from unittest import mock
from urllib.parse import urlencode

from django.core.cache import cache
from django.test import RequestFactory, modify_settings, override_settings
from django.utils import timezone
from oauth2_provider.models import (
    get_access_token_model,
    get_application_model,
    get_grant_model,
    get_refresh_token_model,
)
from oauth2_provider.settings import oauth2_settings
from rest_framework import test
from rest_framework.throttling import SimpleRateThrottle

from waldur_core.configuration.testing import override_config
from waldur_core.structure.tests import factories
from waldur_mastermind.mcp import tasks
from waldur_mastermind.mcp.apps import configure_django_cors_headers

Application = get_application_model()
AccessToken = get_access_token_model()
Grant = get_grant_model()
RefreshToken = get_refresh_token_model()

MCP_URL = "https://api.example.com/mcp"
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
CIMD_CLIENT_ID = "https://client.example.com/oauth/metadata.json"
VERIFIER = "a" * 64
FORM = "application/x-www-form-urlencoded"


def s256(verifier):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def token_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class OAuthTestMixin:
    def post_form(self, url, data, **extra):
        return self.client.post(
            url, urlencode(data, doseq=True), content_type=FORM, **extra
        )

    def create_public_client(self, **kwargs):
        return Application.objects.create(
            name="Claude",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=REDIRECT_URI,
            **kwargs,
        )

    def create_grant(self, application, user, resource=None, scope="mcp:read"):
        # Stands in for the consent bridge, which creates grants in #806.
        return Grant.objects.create(
            application=application,
            user=user,
            code="code-" + application.client_id,
            expires=timezone.now() + timedelta(minutes=1),
            redirect_uri=REDIRECT_URI,
            scope=scope,
            code_challenge=s256(VERIFIER),
            code_challenge_method="S256",
            resource=resource or [],
        )

    def exchange_code(self, application, grant, **extra):
        return self.post_form(
            "/oauth/token/",
            {
                "grant_type": "authorization_code",
                "client_id": application.client_id,
                "code": grant.code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": VERIFIER,
                **extra,
            },
        )

    def refresh(self, application, refresh_token, **extra):
        return self.post_form(
            "/oauth/token/",
            {
                "grant_type": "refresh_token",
                "client_id": application.client_id,
                "refresh_token": refresh_token,
                **extra,
            },
        )


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class MetadataTest(test.APITestCase):
    def test_metadata_advertises_code_pkce_dcr_and_cimd(self):
        response = self.client.get("/.well-known/oauth-authorization-server")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["issuer"], "http://testserver")
        self.assertEqual(data["response_types_supported"], ["code"])
        self.assertEqual(
            data["grant_types_supported"], ["authorization_code", "refresh_token"]
        )
        self.assertEqual(data["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(data["scopes_supported"], ["mcp:read"])
        self.assertIn("none", data["token_endpoint_auth_methods_supported"])
        self.assertEqual(
            data["authorization_endpoint"], "http://testserver/oauth/authorize/"
        )
        self.assertEqual(data["token_endpoint"], "http://testserver/oauth/token/")
        self.assertEqual(
            data["registration_endpoint"], "http://testserver/oauth/register/"
        )
        self.assertTrue(data["client_id_metadata_document_supported"])

    @override_settings(ALLOWED_HOSTS=["api.example.com"])
    def test_issuer_follows_the_api_host(self):
        response = self.client.get(
            "/.well-known/oauth-authorization-server", HTTP_HOST="api.example.com"
        )

        self.assertEqual(response.json()["issuer"], "http://api.example.com")

    @override_settings(ALLOWED_HOSTS=["api.example.com"])
    def test_authorization_response_iss_matches_the_metadata_issuer(self):
        # RFC 9207: clients reject a code whose iss differs from the metadata
        # issuer. The consent bridge (#806) issues codes from an API path.
        metadata = self.client.get(
            "/.well-known/oauth-authorization-server", HTTP_HOST="api.example.com"
        ).json()
        request = RequestFactory().post(
            "/api/oauth/consent/", HTTP_HOST="api.example.com"
        )

        self.assertEqual(
            oauth2_settings.oauth2_authorization_server_issuer(request),
            metadata["issuer"],
        )


class GatingTest(OAuthTestMixin, test.APITestCase):
    URLS = [
        ("get", "/.well-known/oauth-authorization-server"),
        ("get", "/oauth/authorize/"),
        ("post", "/oauth/token/"),
        ("post", "/oauth/revoke_token/"),
        ("post", "/oauth/register/"),
        ("get", "/oauth/register/some-client/"),
    ]

    @override_config(MCP_ENABLED=False, MCP_RESOURCE_URLS=MCP_URL)
    def test_endpoints_are_not_served_while_mcp_is_disabled(self):
        for method, url in self.URLS:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 404)

    @override_config(MCP_ENABLED=False, MCP_RESOURCE_URLS=MCP_URL)
    def test_disabling_mcp_stops_token_issuance(self):
        user = factories.UserFactory()
        application = self.create_public_client()
        grant = self.create_grant(application, user)

        response = self.exchange_code(application, grant)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(AccessToken.objects.filter(user=user).exists())

    @override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
    def test_authorize_is_reserved_for_the_consent_bridge(self):
        response = self.client.get("/oauth/authorize/")

        self.assertEqual(response.status_code, 503)

    @override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
    def test_endpoints_stay_csrf_exempt(self):
        # The gating wrapper must keep DOT's csrf_exempt, or these POSTs get 403.
        client = test.APIClient(enforce_csrf_checks=True)
        for url in ["/oauth/token/", "/oauth/revoke_token/", "/oauth/register/"]:
            with self.subTest(url=url):
                response = client.post(url, {}, format="json")

                self.assertNotEqual(response.status_code, 403)


class CorsTest(test.APITestCase):
    ORIGIN = "http://localhost:6274"

    @override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
    def test_any_origin_can_read_the_answers(self):
        for method, url in GatingTest.URLS:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url, HTTP_ORIGIN=self.ORIGIN)

                self.assertEqual(response["Access-Control-Allow-Origin"], "*")
                self.assertNotIn("Access-Control-Allow-Credentials", response)

    @override_config(MCP_ENABLED=False, MCP_RESOURCE_URLS=MCP_URL)
    def test_not_found_answer_is_readable_too(self):
        response = self.client.get(
            "/.well-known/oauth-authorization-server", HTTP_ORIGIN=self.ORIGIN
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")

    def test_preflight_allows_the_headers_mcp_clients_send(self):
        for url in ["/.well-known/oauth-authorization-server", "/oauth/register/"]:
            with self.subTest(url=url):
                response = self.client.options(
                    url,
                    HTTP_ORIGIN=self.ORIGIN,
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                    HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type, mcp-protocol-version",
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Access-Control-Allow-Origin"], "*")
                allowed = response["Access-Control-Allow-Headers"]
                self.assertIn("MCP-Protocol-Version", allowed)
                self.assertIn("Authorization", allowed)

    @modify_settings(MIDDLEWARE={"prepend": "corsheaders.middleware.CorsMiddleware"})
    @override_settings(
        # As helm's config-override.yaml sets them: homeport only, no MCP header.
        CORS_ALLOWED_ORIGINS=["https://homeport.example.com"],
        CORS_ALLOW_HEADERS=("accept", "authorization", "content-type"),
    )
    def test_django_cors_headers_preflight_allows_mcp_clients(self):
        configure_django_cors_headers()

        def preflight(url):
            return self.client.options(
                url,
                HTTP_ORIGIN=self.ORIGIN,
                HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type, mcp-protocol-version",
            )

        for url in ["/.well-known/oauth-authorization-server", "/oauth/token/"]:
            with self.subTest(url=url):
                response = preflight(url)

                self.assertIn(
                    response["Access-Control-Allow-Origin"], ["*", self.ORIGIN]
                )
                self.assertIn(
                    "mcp-protocol-version", response["Access-Control-Allow-Headers"]
                )
        # The rest of the API still answers only homeport.
        self.assertNotIn("Access-Control-Allow-Origin", preflight("/api/users/"))


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class DynamicClientRegistrationTest(OAuthTestMixin, test.APITestCase):
    def register(self, **metadata):
        body = {
            "client_name": "Claude",
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            **metadata,
        }
        return self.client.post("/oauth/register/", body, format="json")

    def test_anonymous_client_registers_without_admin_action(self):
        response = self.register()

        self.assertEqual(response.status_code, 201)
        application = Application.objects.get(client_id=response.json()["client_id"])
        self.assertEqual(
            application.registration_source, Application.RegistrationSource.DCR
        )
        self.assertEqual(application.client_type, Application.CLIENT_PUBLIC)
        self.assertIsNone(application.user)

    def test_registration_alone_gives_no_data_access(self):
        response = self.register()
        application = Application.objects.get(client_id=response.json()["client_id"])

        # The only token is the RFC 7592 registration token: no user, no MCP scope.
        token = AccessToken.objects.get(application=application)
        self.assertIsNone(token.user)
        self.assertFalse(token.allow_scopes(["mcp:read"]))
        self.assertFalse(Grant.objects.filter(application=application).exists())

    def test_confidential_client_gets_no_token_without_a_user(self):
        response = self.register(token_endpoint_auth_method="client_secret_post")
        data = response.json()

        token_response = self.post_form(
            "/oauth/token/",
            {
                "grant_type": "client_credentials",
                "client_id": data["client_id"],
                "client_secret": data["client_secret"],
            },
        )

        self.assertEqual(token_response.status_code, 400)
        self.assertEqual(token_response.json()["error"], "unauthorized_client")

    def test_malformed_grant_types_get_a_client_error(self):
        for grant_types in [[{}], [["authorization_code"]]]:
            with self.subTest(grant_types=grant_types):
                response = self.register(grant_types=grant_types)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_client_metadata")

    def test_grants_without_user_consent_cannot_be_registered(self):
        for grant_type in ["client_credentials", "password", "implicit"]:
            with self.subTest(grant_type=grant_type):
                response = self.register(grant_types=[grant_type])

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_client_metadata")
        self.assertFalse(Application.objects.exists())

    def test_client_credentials_is_refused_even_for_a_manual_client(self):
        application = Application(
            name="Backend",
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            client_secret="secret",
        )
        application.save()

        response = self.post_form(
            "/oauth/token/",
            {
                "grant_type": "client_credentials",
                "client_id": application.client_id,
                "client_secret": "secret",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AccessToken.objects.exists())

    def test_registration_update_cannot_switch_to_a_consentless_grant(self):
        data = self.register().json()

        response = self.client.put(
            data["registration_client_uri"],
            {"redirect_uris": [REDIRECT_URI], "grant_types": ["client_credentials"]},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {data['registration_access_token']}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_client_metadata")
        application = Application.objects.get(client_id=data["client_id"])
        self.assertEqual(
            application.authorization_grant_type,
            Application.GRANT_AUTHORIZATION_CODE,
        )

    def test_registration_update_without_the_token_is_unauthorized(self):
        data = self.register().json()

        response = self.client.put(
            data["registration_client_uri"],
            {"grant_types": ["client_credentials"]},
            format="json",
        )

        self.assertEqual(response.status_code, 401)

    def test_registration_update_keeps_working_for_allowed_grants(self):
        data = self.register().json()

        response = self.client.put(
            data["registration_client_uri"],
            {
                "client_name": "Renamed",
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "none",
            },
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {data['registration_access_token']}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Application.objects.get(client_id=data["client_id"]).name, "Renamed"
        )

    def test_registration_token_expires_and_is_cleared(self):
        data = self.register().json()
        token = AccessToken.objects.get(application__client_id=data["client_id"])

        self.assertAlmostEqual(
            token.expires,
            timezone.now() + timedelta(days=30),
            delta=timedelta(minutes=1),
        )
        token.expires = timezone.now() - timedelta(seconds=1)
        token.save()
        tasks.clear_expired_oauth_tokens()
        self.assertFalse(AccessToken.objects.exists())


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class ClientIdMetadataDocumentTest(OAuthTestMixin, test.APITestCase):
    DOCUMENT = {
        "client_id": CIMD_CLIENT_ID,
        "client_name": "Example agent",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_method": "none",
    }

    def test_unknown_client_registers_on_first_sight(self):
        with mock.patch(
            "oauth2_provider.cimd.SafeMetadataFetcher.fetch",
            return_value=(self.DOCUMENT, 300),
        ) as fetch:
            response = self.post_form(
                "/oauth/token/",
                {
                    "grant_type": "authorization_code",
                    "client_id": CIMD_CLIENT_ID,
                    "code": "unknown",
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": VERIFIER,
                },
            )

        fetch.assert_called_once_with(CIMD_CLIENT_ID)
        application = Application.objects.get(client_id=CIMD_CLIENT_ID)
        self.assertEqual(
            application.registration_source, Application.RegistrationSource.CIMD
        )
        self.assertEqual(application.name, "Example agent")
        # Registered, but the request still fails: there is no consented grant.
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertFalse(AccessToken.objects.filter(application=application).exists())


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class RegistrationThrottleTest(OAuthTestMixin, test.APITestCase):
    def setUp(self):
        cache.clear()
        rates = mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, {"mcp_client_registration": "2/hour"}
        )
        rates.start()
        self.addCleanup(rates.stop)

    def register(self, **extra):
        return self.client.post(
            "/oauth/register/",
            {
                "client_name": "Agent",
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "none",
            },
            format="json",
            **extra,
        )

    def request_token_as(self, client_id, **extra):
        return self.post_form(
            "/oauth/token/",
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": "unknown",
                "redirect_uri": REDIRECT_URI,
                "code_verifier": VERIFIER,
            },
            **extra,
        )

    def via_proxy(self, client_says, proxy_saw):
        # The proxy appends the address it saw to whatever the client sent.
        return {"HTTP_X_FORWARDED_FOR": f"{client_says}, {proxy_saw}"}

    def fetch_document(self, client_id):
        return (
            {**ClientIdMetadataDocumentTest.DOCUMENT, "client_id": client_id},
            300,
        )

    def test_dynamic_registration_is_limited_per_address(self):
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.register().status_code, 201)

        response = self.register()

        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response["Retry-After"]), 0)
        self.assertEqual(Application.objects.count(), 2)

    def test_unknown_metadata_documents_are_limited_per_address(self):
        urls = [f"https://agent{i}.example.com/client.json" for i in range(3)]
        with mock.patch(
            "oauth2_provider.cimd.SafeMetadataFetcher.fetch",
            side_effect=self.fetch_document,
        ) as fetch:
            for url in urls:
                self.request_token_as(url)

        self.assertEqual(fetch.call_count, 2)
        self.assertFalse(Application.objects.filter(client_id=urls[2]).exists())

    def test_known_clients_do_not_count(self):
        with mock.patch(
            "oauth2_provider.cimd.SafeMetadataFetcher.fetch",
            side_effect=self.fetch_document,
        ) as fetch:
            for _ in range(3):
                self.request_token_as(CIMD_CLIENT_ID)

        fetch.assert_called_once()
        # Only the first sight counted, so one more registration is allowed.
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.register().status_code, 429)

    def test_forwarded_for_set_by_the_client_does_not_reset_the_count(self):
        statuses = [
            self.register(
                **self.via_proxy(f"198.51.100.{i}", "203.0.113.7")
            ).status_code
            for i in range(3)
        ]

        self.assertEqual(statuses, [201, 201, 429])

    def test_forwarded_for_set_by_the_client_does_not_reset_the_cimd_count(self):
        urls = [f"https://agent{i}.example.com/client.json" for i in range(3)]
        with mock.patch(
            "oauth2_provider.cimd.SafeMetadataFetcher.fetch",
            side_effect=self.fetch_document,
        ) as fetch:
            for i, url in enumerate(urls):
                self.request_token_as(
                    url, **self.via_proxy(f"198.51.100.{i}", "203.0.113.7")
                )

        self.assertEqual(fetch.call_count, 2)

    def test_clients_behind_the_proxy_are_counted_apart(self):
        statuses = [
            self.register(
                **self.via_proxy("198.51.100.1", f"203.0.113.{i}")
            ).status_code
            for i in range(3)
        ]

        self.assertEqual(statuses, [201, 201, 201])

    @override_settings(MCP_OAUTH_TRUSTED_PROXY_COUNT=0)
    def test_without_a_proxy_forwarded_for_is_ignored(self):
        statuses = [
            self.register(HTTP_X_FORWARDED_FOR=f"203.0.113.{i}").status_code
            for i in range(3)
        ]

        self.assertEqual(statuses, [201, 201, 429])


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class TokenTest(OAuthTestMixin, test.APITestCase):
    def setUp(self):
        self.user = factories.UserFactory()
        self.application = self.create_public_client()

    def get_access_token(self, response):
        return AccessToken.objects.get(
            token_checksum=token_hash(response.json()["access_token"])
        )

    def test_token_is_bound_to_the_requested_resource(self):
        grant = self.create_grant(self.application, self.user, resource=[MCP_URL])

        response = self.exchange_code(self.application, grant, resource=MCP_URL)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["scope"], "mcp:read")
        self.assertEqual(data["expires_in"], 3600)
        token = self.get_access_token(response)
        self.assertEqual(token.user, self.user)
        self.assertEqual(token.resource, [MCP_URL])

    def test_equivalent_spelling_of_the_resource_is_accepted(self):
        for resource in [
            "https://API.example.com:443/mcp/",
            "https://api.example.com/./mcp",
        ]:
            with self.subTest(resource=resource):
                application = self.create_public_client()
                grant = self.create_grant(application, self.user)

                response = self.exchange_code(application, grant, resource=resource)

                self.assertEqual(response.status_code, 200)

    def test_token_without_resource_is_bound_to_the_mcp_endpoint(self):
        grant = self.create_grant(self.application, self.user)

        response = self.exchange_code(self.application, grant)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.get_access_token(response).resource, [MCP_URL])

    def test_unknown_resource_is_refused(self):
        grant = self.create_grant(self.application, self.user)

        response = self.exchange_code(
            self.application, grant, resource="https://evil.example.com/mcp"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["error"], "invalid_target")
        self.assertFalse(AccessToken.objects.filter(user=self.user).exists())

    def test_unknown_resource_in_the_grant_is_refused(self):
        grant = self.create_grant(
            self.application, self.user, resource=["https://evil.example.com/mcp"]
        )

        response = self.exchange_code(self.application, grant)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_target")

    @override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS="")
    def test_no_token_is_issued_while_no_resource_is_configured(self):
        grant = self.create_grant(self.application, self.user)

        response = self.exchange_code(self.application, grant)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_target")

    @override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS="api.example.com/mcp")
    def test_malformed_resource_setting_refuses_issuance(self):
        grant = self.create_grant(self.application, self.user)

        response = self.exchange_code(self.application, grant)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_target")
        self.assertFalse(AccessToken.objects.filter(user=self.user).exists())

    def test_device_code_grant_is_refused_without_an_error(self):
        response = self.post_form(
            "/oauth/token/",
            {"grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "unsupported_grant_type")

    def test_wrong_pkce_verifier_is_refused(self):
        grant = self.create_grant(self.application, self.user)

        response = self.exchange_code(self.application, grant, code_verifier="b" * 64)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_inactive_user_cannot_exchange_a_code(self):
        grant = self.create_grant(self.application, self.user)
        self.user.is_active = False
        self.user.save()

        response = self.exchange_code(self.application, grant)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_tokens_are_not_stored_in_plaintext(self):
        grant = self.create_grant(self.application, self.user)

        response = self.exchange_code(self.application, grant)

        self.assertEqual(self.get_access_token(response).token, "")
        refresh = RefreshToken.objects.get(
            token_checksum=token_hash(response.json()["refresh_token"])
        )
        self.assertEqual(refresh.token, "")


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class RefreshTokenTest(OAuthTestMixin, test.APITestCase):
    def setUp(self):
        self.user = factories.UserFactory()
        self.application = self.create_public_client()
        grant = self.create_grant(self.application, self.user)
        self.tokens = self.exchange_code(self.application, grant).json()

    def test_refresh_rotates_the_refresh_token(self):
        response = self.refresh(self.application, self.tokens["refresh_token"])

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(
            response.json()["refresh_token"], self.tokens["refresh_token"]
        )

    def test_reused_refresh_token_revokes_the_family(self):
        rotated = self.refresh(self.application, self.tokens["refresh_token"]).json()

        replay = self.refresh(self.application, self.tokens["refresh_token"])
        after_replay = self.refresh(self.application, rotated["refresh_token"])

        self.assertEqual(replay.status_code, 400)
        self.assertEqual(after_replay.status_code, 400)

    def test_revoked_refresh_token_cannot_be_used(self):
        revoke = self.post_form(
            "/oauth/revoke_token/",
            {
                "token": self.tokens["refresh_token"],
                "token_type_hint": "refresh_token",
                "client_id": self.application.client_id,
            },
        )

        response = self.refresh(self.application, self.tokens["refresh_token"])

        self.assertEqual(revoke.status_code, 200)
        self.assertEqual(response.status_code, 400)

    def test_inactive_user_cannot_refresh(self):
        self.user.is_active = False
        self.user.save()

        response = self.refresh(self.application, self.tokens["refresh_token"])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_reserved_write_scope_cannot_be_granted(self):
        response = self.refresh(
            self.application, self.tokens["refresh_token"], scope="mcp:read mcp:write"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_scope")

    def test_refresh_keeps_the_resource_binding(self):
        response = self.refresh(self.application, self.tokens["refresh_token"])

        token = AccessToken.objects.get(
            token_checksum=token_hash(response.json()["access_token"])
        )
        self.assertEqual(token.resource, [MCP_URL])

    def test_refresh_fails_once_the_resource_is_no_longer_served(self):
        with override_config(MCP_RESOURCE_URLS="https://new.example.com/mcp"):
            response = self.refresh(self.application, self.tokens["refresh_token"])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_target")

    @override_config(
        MCP_ENABLED=True, MCP_RESOURCE_URLS=f"{MCP_URL} https://old.example.com/mcp"
    )
    def test_default_binding_survives_dropping_another_url(self):
        application = self.create_public_client()
        grant = self.create_grant(application, self.user)
        tokens = self.exchange_code(application, grant).json()
        token = AccessToken.objects.get(
            token_checksum=token_hash(tokens["access_token"])
        )

        with override_config(MCP_RESOURCE_URLS=MCP_URL):
            response = self.refresh(application, tokens["refresh_token"])

        self.assertEqual(token.resource, [MCP_URL])
        self.assertEqual(response.status_code, 200)


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class RestApiTest(OAuthTestMixin, test.APITestCase):
    def test_oauth_access_token_is_rejected_by_the_rest_api(self):
        user = factories.UserFactory()
        application = self.create_public_client()
        grant = self.create_grant(application, user)
        access_token = self.exchange_code(application, grant).json()["access_token"]

        response = self.client.get(
            "/api/users/me/", HTTP_AUTHORIZATION=f"Bearer {access_token}"
        )

        self.assertEqual(response.status_code, 401)


class CleanupTest(OAuthTestMixin, test.APITestCase):
    def test_expired_tokens_are_cleared(self):
        application = self.create_public_client()
        AccessToken.objects.create(
            application=application,
            user=factories.UserFactory(),
            token_checksum=token_hash("expired"),
            expires=timezone.now() - timedelta(days=1),
            scope="mcp:read",
        )

        tasks.clear_expired_oauth_tokens()

        self.assertFalse(AccessToken.objects.exists())

    def test_expired_cimd_applications_are_cleared(self):
        Application.objects.create(
            client_id=CIMD_CLIENT_ID,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=REDIRECT_URI,
            registration_source=Application.RegistrationSource.CIMD,
            cimd_expires_at=timezone.now() - timedelta(days=1),
        )

        tasks.clear_expired_cimd_applications()

        self.assertFalse(Application.objects.exists())


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class DeactivationTest(OAuthTestMixin, test.APITestCase):
    def setUp(self):
        self.user = factories.UserFactory()
        self.other_user = factories.UserFactory()
        self.application = self.create_public_client()
        self.tokens = self.exchange_code(
            self.application, self.create_grant(self.application, self.user)
        ).json()
        self.pending_grant = Grant.objects.create(
            application=self.application,
            user=self.user,
            code="pending",
            expires=timezone.now() + timedelta(minutes=1),
            redirect_uri=REDIRECT_URI,
            scope="mcp:read",
            code_challenge=s256(VERIFIER),
            code_challenge_method="S256",
        )
        other_application = self.create_public_client()
        self.exchange_code(
            other_application, self.create_grant(other_application, self.other_user)
        )

    def test_deactivation_deletes_the_users_oauth_sessions(self):
        self.user.is_active = False
        self.user.save()

        self.assertFalse(AccessToken.objects.filter(user=self.user).exists())
        self.assertFalse(RefreshToken.objects.filter(user=self.user).exists())
        self.assertFalse(Grant.objects.filter(user=self.user).exists())
        self.assertTrue(AccessToken.objects.filter(user=self.other_user).exists())

    def test_reactivation_does_not_revive_old_sessions(self):
        self.user.is_active = False
        self.user.save()
        self.user.is_active = True
        self.user.save()

        response = self.refresh(self.application, self.tokens["refresh_token"])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_other_saves_keep_the_sessions(self):
        self.user.first_name = "Renamed"
        self.user.save()

        self.assertTrue(AccessToken.objects.filter(user=self.user).exists())
        self.assertTrue(RefreshToken.objects.filter(user=self.user).exists())
