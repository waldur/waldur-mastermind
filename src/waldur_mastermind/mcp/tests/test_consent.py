from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

from oauth2_provider.oauth2_backends import OAuthLibCore
from oauthlib.oauth2.rfc6749.errors import InvalidClientIdError
from rest_framework import test
from rest_framework.authtoken.models import Token

from waldur_core.configuration.testing import override_config
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories
from waldur_mastermind.mcp.tests.test_oauth import (
    CIMD_CLIENT_ID,
    MCP_URL,
    REDIRECT_URI,
    VERIFIER,
    AccessToken,
    Application,
    Grant,
    OAuthTestMixin,
    s256,
)
from waldur_mastermind.mcp.tests.test_server import create_pat

AUTHORIZE_URL = "/oauth/authorize/"
CONSENT_URL = "/api/oauth/consent/"
HOMEPORT_URL = "https://portal.example.com/"
OUT_OF_RANGE_PORT_URI = REDIRECT_URI.replace("claude.ai", "claude.ai:99999")
CIMD_DOCUMENT = {
    "client_id": CIMD_CLIENT_ID,
    "client_name": "ChatGPT",
    "redirect_uris": [REDIRECT_URI],
    "grant_types": ["authorization_code"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}
# Requests from a known client to a registered redirect URI that are still
# refused, with the error each one gets.
INVALID_REQUESTS = [
    ("invalid_request", {"code_challenge": None}),
    ("invalid_request", {"code_challenge_method": None}),
    ("invalid_request", {"code_challenge_method": "plain"}),
    ("invalid_request", {"code_challenge": ""}),
    ("invalid_request", {"code_challenge": "a" * 129}),
    ("unauthorized_client", {"response_type": "token"}),
    ("invalid_scope", {"scope": "mcp:write"}),
    ("invalid_target", {"resource": ["https://evil.example/mcp"]}),
    ("invalid_target", {"resource": ["not-a-uri"]}),
    ("invalid_target", {"resource": ["http://[foo"]}),
]


def query_of(url):
    return parse_qs(urlparse(url).query)


def without_query(url):
    return url.split("?")[0]


class ConsentTestMixin(OAuthTestMixin):
    def setUp(self):
        self.application = self.create_public_client(
            registration_source=Application.RegistrationSource.DCR
        )
        self.user = factories.UserFactory()

    def params(self, **overrides):
        params = {
            "client_id": self.application.client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "mcp:read",
            "state": "client-state",
            "code_challenge": s256(VERIFIER),
            "code_challenge_method": "S256",
            "resource": [MCP_URL],
            **overrides,
        }
        return {key: value for key, value in params.items() if value is not None}

    def login(self, user=None):
        token, _ = Token.objects.get_or_create(user=user or self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def get_consent(self, **overrides):
        query = urlencode(self.params(**overrides), doseq=True)
        return self.client.get(f"{CONSENT_URL}?{query}")

    def decide(self, allow=True, **overrides):
        return self.client.post(
            CONSENT_URL, {**self.params(**overrides), "allow": allow}, format="json"
        )

    def assert_refused(self, response):
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(set(response.json()), {"error", "error_description"})
        self.assertFalse(Grant.objects.exists())


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL, HOMEPORT_URL=HOMEPORT_URL)
class AuthorizeTest(ConsentTestMixin, test.APITestCase):
    def authorize(self, **overrides):
        query = urlencode(self.params(**overrides), doseq=True)
        return self.client.get(f"{AUTHORIZE_URL}?{query}")

    def register_by_admin(self):
        Application.objects.filter(pk=self.application.pk).update(
            registration_source=Application.RegistrationSource.MANUAL
        )

    def assert_error_page(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Location", response.headers)
        self.assertIn("text/html", response.headers["Content-Type"])
        self.assertNotIn(b"<p></p>", response.content)

    def assert_error_redirect(self, response, error):
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(without_query(response.url), REDIRECT_URI)
        query = query_of(response.url)
        self.assertEqual(query["error"], [error])
        self.assertEqual(query["state"], ["client-state"])
        self.assertEqual(query["iss"], ["http://testserver"])

    def test_valid_request_goes_to_the_consent_page_without_a_user(self):
        response = self.authorize()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            without_query(response.url), "https://portal.example.com/oauth/consent"
        )
        self.assertEqual(
            query_of(response.url),
            {
                key: value if isinstance(value, list) else [value]
                for key, value in self.params().items()
            },
        )
        self.assertFalse(Grant.objects.exists())

    def test_repeated_resources_reach_the_consent_page(self):
        second = "https://api.example.com/mcp/support"
        with override_config(MCP_RESOURCE_URLS=f"{MCP_URL} {second}"):
            response = self.authorize(resource=[MCP_URL, second])

        self.assertEqual(query_of(response.url)["resource"], [MCP_URL, second])

    def test_unknown_client_gets_an_error_page(self):
        self.assert_error_page(self.authorize(client_id="nope"))

    def test_missing_client_gets_an_error_page(self):
        self.assert_error_page(self.authorize(client_id=None))

    def test_unregistered_redirect_uri_is_never_redirected_to(self):
        self.assert_error_page(self.authorize(redirect_uri="https://evil.example/cb"))

    def test_out_of_range_port_gets_an_error_page(self):
        self.assert_error_page(self.authorize(redirect_uri=OUT_OF_RANGE_PORT_URI))

    def test_repeated_client_id_gets_an_error_page(self):
        response = self.authorize(client_id=[self.application.client_id, "other"])

        self.assert_error_page(response)

    def test_null_byte_in_the_client_id_gets_an_error_page(self):
        self.assert_error_page(self.authorize(client_id="a\x00b"))

    def test_error_page_escapes_what_it_shows(self):
        error = InvalidClientIdError(description="<script>alert(1)</script>")
        with mock.patch(
            "waldur_mastermind.mcp.views.validate_authorization_request",
            side_effect=error,
        ):
            response = self.authorize()

        self.assert_error_page(response)
        self.assertNotIn(b"<script>", response.content)

    def test_script_redirect_uri_is_refused(self):
        # DCR and CIMD refuse these, but the admin can save one.
        application = Application.objects.create(
            name="Bad",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="javascript:alert(1)",
        )
        for overrides in ({}, {"code_challenge": None}):
            with self.subTest(overrides=overrides):
                response = self.authorize(
                    client_id=application.client_id,
                    redirect_uri="javascript:alert(1)",
                    **overrides,
                )

                self.assertEqual(response.status_code, 400)
                self.assertNotIn("Location", response.headers)

    def test_invalid_requests_from_self_registered_clients_get_an_error_page(self):
        for _, overrides in INVALID_REQUESTS:
            with self.subTest(overrides=overrides):
                self.assert_error_page(self.authorize(**overrides))

    def test_invalid_requests_from_cimd_clients_get_an_error_page(self):
        with mock.patch(
            "oauth2_provider.cimd.SafeMetadataFetcher.fetch",
            return_value=(CIMD_DOCUMENT, 300),
        ):
            for _, overrides in INVALID_REQUESTS:
                with self.subTest(overrides=overrides):
                    response = self.authorize(client_id=CIMD_CLIENT_ID, **overrides)

                    self.assert_error_page(response)

    def test_error_page_names_an_error_that_has_no_description(self):
        response = self.authorize(scope="mcp:write")

        self.assert_error_page(response)
        self.assertIn(b"<p>invalid_scope</p>", response.content)

    def test_registering_a_client_does_not_make_authorize_a_redirector(self):
        evil = "https://evil.example/cb"
        Application.objects.filter(pk=self.application.pk).update(redirect_uris=evil)

        response = self.authorize(redirect_uri=evil, scope="nope")

        self.assert_error_page(response)

    def test_invalid_requests_from_admin_registered_clients_return_with_the_error(
        self,
    ):
        self.register_by_admin()
        for error, overrides in INVALID_REQUESTS:
            with self.subTest(overrides=overrides):
                self.assert_error_redirect(self.authorize(**overrides), error)

    def test_state_the_consent_api_would_refuse_returns_to_the_client(self):
        self.register_by_admin()

        response = self.authorize(state="a\x00b")

        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(without_query(response.url), REDIRECT_URI)
        self.assertEqual(query_of(response.url)["error"], ["invalid_request"])

    def test_a_request_is_refused_while_no_resource_is_configured(self):
        self.register_by_admin()

        with override_config(MCP_RESOURCE_URLS=""):
            response = self.authorize(resource=None)

        self.assert_error_redirect(response, "invalid_target")

    def test_a_request_is_refused_while_the_resource_setting_is_malformed(self):
        self.register_by_admin()
        for setting in ("not-a-uri", "http://[foo"):
            with (
                self.subTest(setting=setting),
                override_config(MCP_RESOURCE_URLS=setting),
            ):
                response = self.authorize(resource=None)

                self.assert_error_redirect(response, "invalid_target")

    def test_trusted_clients_still_go_through_consent(self):
        Application.objects.filter(pk=self.application.pk).update(
            skip_authorization=True
        )
        self.login()

        response = self.authorize(approval_prompt="auto")

        self.assertTrue(response.url.startswith("https://portal.example.com/"))
        self.assertFalse(Grant.objects.exists())

    def test_cimd_client_is_registered_on_first_sight(self):
        with mock.patch(
            "oauth2_provider.cimd.SafeMetadataFetcher.fetch",
            return_value=(CIMD_DOCUMENT, 300),
        ):
            response = self.authorize(client_id=CIMD_CLIENT_ID)

        self.assertEqual(response.status_code, 302, response.content)
        self.assertTrue(response.url.startswith("https://portal.example.com/"))
        self.assertTrue(Application.objects.filter(client_id=CIMD_CLIENT_ID).exists())


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class ConsentDetailsTest(ConsentTestMixin, test.APITestCase):
    def test_details_describe_the_client_and_the_scopes(self):
        self.login()

        response = self.get_consent()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json(),
            {
                "client_name": "Claude",
                "client_host": None,
                "redirect_host": "claude.ai",
                "registration_source": "dcr",
                "scopes": [
                    {"name": "mcp:read", "description": "Read your Waldur data"}
                ],
                "elevated_access": None,
            },
        )

    def test_nameless_client_is_shown_by_its_redirect_host(self):
        self.login()
        for name in ("", "   ", "‮\n​"):
            with self.subTest(name=name):
                Application.objects.filter(pk=self.application.pk).update(name=name)

                self.assertEqual(self.get_consent().data["client_name"], "claude.ai")

    def test_client_name_is_shown_without_control_or_bidi_characters(self):
        self.login()
        Application.objects.filter(pk=self.application.pk).update(
            name="Claude‮\nofficial​ app"
        )

        response = self.get_consent()

        self.assertEqual(response.data["client_name"], "Claude official app")

    def test_cimd_client_shows_the_host_of_its_client_id(self):
        Application.objects.create(
            name="ChatGPT",
            client_id=CIMD_CLIENT_ID,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=REDIRECT_URI,
            registration_source=Application.RegistrationSource.CIMD,
        )
        self.login()

        with mock.patch(
            "oauth2_provider.cimd.SafeMetadataFetcher.fetch",
            return_value=(CIMD_DOCUMENT, 300),
        ):
            response = self.get_consent(client_id=CIMD_CLIENT_ID)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["client_host"], "client.example.com")
        self.assertEqual(response.data["registration_source"], "cimd")

    def test_elevated_access_names_the_role(self):
        cases = [
            ({"is_staff": True}, "staff"),
            ({"is_support": True}, "support"),
            ({"is_staff": True, "is_support": True}, "staff"),
        ]
        for roles, expected in cases:
            with self.subTest(roles=roles):
                self.login(factories.UserFactory(**roles))

                self.assertEqual(self.get_consent().data["elevated_access"], expected)

    def test_scope_defaults_when_the_client_names_none(self):
        self.login()

        response = self.get_consent(scope=None)

        self.assertEqual(response.data["scopes"][0]["name"], "mcp:read")

    def test_anonymous_request_is_refused(self):
        self.assertEqual(self.get_consent().status_code, 401)

    def test_invalid_request_is_refused(self):
        self.login()

        response = self.get_consent(client_id="nope")

        self.assert_refused(response)
        self.assertEqual(response.json()["error"], "invalid_request")

    def test_unparseable_resource_is_refused(self):
        self.login()

        response = self.get_consent(resource=["http://[foo"])

        self.assert_refused(response)
        self.assertEqual(response.json()["error"], "invalid_target")

    def test_refused_resource_is_not_repeated_in_the_error(self):
        self.login()
        resource = "https://x/ Your account is locked, call +1-555-0100"

        response = self.get_consent(resource=[resource])

        self.assert_refused(response)
        self.assertNotIn("locked", response.json()["error_description"])

    def test_malformed_params_are_named_in_the_error(self):
        self.login()

        response = self.get_consent(code_challenge="a" * 129, resource=[""])

        self.assert_refused(response)
        self.assertEqual(response.json()["error"], "invalid_request")
        self.assertIn("code_challenge", response.json()["error_description"])
        self.assertIn("resource", response.json()["error_description"])

    def test_missing_response_type_is_refused(self):
        self.login()

        self.assert_refused(self.get_consent(response_type=None))


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class ConsentDecisionTest(ConsentTestMixin, test.APITestCase):
    TAMPERED = [
        {"client_id": "nope"},
        {"redirect_uri": "https://evil.example/cb"},
        {"redirect_uri": OUT_OF_RANGE_PORT_URI},
        {"response_type": "token"},
        {"code_challenge_method": "plain"},
        {"code_challenge_method": None},
        {"code_challenge": None},
        {"code_challenge": "a" * 129},
        {"scope": "mcp:write"},
        {"resource": ["https://evil.example/mcp"]},
    ]

    def setUp(self):
        super().setUp()
        self.login()

    def exchange(self, code, **extra):
        self.client.credentials()
        return self.post_form(
            "/oauth/token/",
            {
                "grant_type": "authorization_code",
                "client_id": self.application.client_id,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": VERIFIER,
                **extra,
            },
        )

    def test_full_flow_ends_in_a_token_for_the_requested_resource(self):
        authorize = self.client.get(
            f"{AUTHORIZE_URL}?{urlencode(self.params(), doseq=True)}"
        )
        self.assertEqual(authorize.status_code, 302)

        # Homeport passes on what the consent page's URL carried.
        params = {
            key: values if key == "resource" else values[0]
            for key, values in query_of(authorize.url).items()
        }
        details = self.client.get(f"{CONSENT_URL}?{urlencode(params, doseq=True)}")
        self.assertEqual(details.status_code, 200, details.content)

        decision = self.client.post(
            CONSENT_URL, {**params, "allow": True}, format="json"
        )
        self.assertEqual(decision.status_code, 200, decision.content)
        redirect_url = decision.json()["redirect_url"]
        self.assertEqual(without_query(redirect_url), REDIRECT_URI)
        query = query_of(redirect_url)
        self.assertEqual(query["state"], ["client-state"])

        response = self.exchange(query["code"][0])

        self.assertEqual(response.status_code, 200, response.content)
        token = AccessToken.objects.get(user=self.user)
        self.assertEqual(token.resource, [MCP_URL])
        self.assertEqual(token.application, self.application)

    def test_token_exchange_needs_the_pkce_verifier(self):
        code = query_of(self.decide().json()["redirect_url"])["code"][0]

        response = self.exchange(code, code_verifier="b" * 64)

        self.assertEqual(response.status_code, 400)

    def test_client_naming_no_resource_or_redirect_uri_gets_a_token(self):
        response = self.decide(resource=None, redirect_uri=None)

        self.assertEqual(response.status_code, 200, response.content)
        redirect_url = response.json()["redirect_url"]
        self.assertEqual(without_query(redirect_url), REDIRECT_URI)

        response = self.exchange(query_of(redirect_url)["code"][0])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(AccessToken.objects.get().resource, [MCP_URL])

    def test_token_is_bound_to_the_resource_the_client_named(self):
        second = "https://api.example.com/mcp/support"
        with override_config(MCP_RESOURCE_URLS=f"{MCP_URL} {second}"):
            redirect_url = self.decide(resource=[second]).json()["redirect_url"]
            response = self.exchange(query_of(redirect_url)["code"][0])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(AccessToken.objects.get().resource, [second])

    def test_grant_belongs_to_the_approving_user(self):
        self.decide()

        self.assertEqual(Grant.objects.get().user, self.user)

    def test_staff_and_support_can_approve(self):
        for roles in ({"is_staff": True}, {"is_support": True}):
            with self.subTest(roles=roles):
                user = factories.UserFactory(**roles)
                self.login(user)

                self.assertEqual(self.decide().status_code, 200)
                self.assertTrue(Grant.objects.filter(user=user).exists())

    def test_response_is_not_cached(self):
        self.assertEqual(self.decide().headers["Cache-Control"], "no-store")

    def test_state_reaches_the_client_unchanged(self):
        response = self.decide(state=" spaced state ")

        self.assertEqual(
            query_of(response.json()["redirect_url"])["state"], [" spaced state "]
        )

    def test_params_outside_the_flow_are_not_honoured(self):
        response = self.decide(response_mode="fragment")

        self.assertEqual(urlparse(response.json()["redirect_url"]).fragment, "")

    def test_query_string_on_the_decision_is_refused(self):
        for query in (
            "response_mode=fragment",
            "resource=https://evil.example/mcp",
        ):
            for allow in (True, False):
                with self.subTest(query=query, allow=allow):
                    response = self.client.post(
                        f"{CONSENT_URL}?{query}",
                        {**self.params(resource=None), "allow": allow},
                        format="json",
                    )

                    self.assert_refused(response)
                    self.assertEqual(response.json()["error"], "invalid_request")

    def test_deny_returns_access_denied_to_the_client(self):
        response = self.decide(allow=False)

        self.assertEqual(response.status_code, 200, response.content)
        redirect_url = response.json()["redirect_url"]
        self.assertEqual(without_query(redirect_url), REDIRECT_URI)
        query = query_of(redirect_url)
        self.assertEqual(query["error"], ["access_denied"])
        self.assertEqual(query["state"], ["client-state"])
        self.assertNotIn("code", query)
        self.assertFalse(Grant.objects.exists())

    def test_iss_matches_the_metadata_issuer_on_allow_and_deny(self):
        issuer = self.client.get("/.well-known/oauth-authorization-server").json()[
            "issuer"
        ]
        for allow in (True, False):
            with self.subTest(allow=allow):
                response = self.decide(allow=allow)

                self.assertEqual(
                    query_of(response.json()["redirect_url"])["iss"], [issuer]
                )

    def test_changed_params_are_refused_on_allow_and_deny(self):
        for allow in (True, False):
            for overrides in self.TAMPERED:
                with self.subTest(allow=allow, overrides=overrides):
                    self.assert_refused(self.decide(allow=allow, **overrides))

        self.assertFalse(Event.objects.filter(event_type__startswith="oauth").exists())

    def test_a_decision_is_required(self):
        response = self.client.post(CONSENT_URL, self.params(), format="json")

        self.assert_refused(response)

    def test_forms_are_refused_as_a_form_without_allow_reads_as_a_denial(self):
        form = urlencode(self.params(), doseq=True)
        cases = [
            lambda: self.client.post(CONSENT_URL, self.params(), format="multipart"),
            lambda: self.client.post(
                CONSENT_URL, form, content_type="application/x-www-form-urlencoded"
            ),
        ]
        for post in cases:
            self.assertEqual(post().status_code, 415)

        self.assertFalse(Event.objects.filter(event_type__startswith="oauth").exists())

    def test_consent_is_audited(self):
        cases = [(True, "oauth_consent_granted"), (False, "oauth_consent_denied")]
        for allow, event_type in cases:
            with self.subTest(allow=allow):
                self.decide(allow=allow)

                event = Event.objects.get(event_type=event_type)
                self.assertIn("Claude", event.message)
                self.assertIn(self.user.username, event.message)
                self.assertEqual(event.context["client_name"], "Claude")
                self.assertEqual(event.context["client_id"], self.application.client_id)
                self.assertEqual(event.context["redirect_host"], "claude.ai")
                self.assertEqual(event.context["scope"], "mcp:read")
                self.assertEqual(event.context["resource"], MCP_URL)

    def test_audited_resource_is_the_one_the_grant_is_bound_to(self):
        second = "https://api.example.com/mcp/support"
        cases = [([second], second), (None, MCP_URL)]
        with override_config(MCP_RESOURCE_URLS=f"{MCP_URL} {second}"):
            for requested, expected in cases:
                with self.subTest(requested=requested):
                    Event.objects.all().delete()

                    self.decide(resource=requested)

                    event = Event.objects.get(event_type="oauth_consent_granted")
                    self.assertEqual(event.context["resource"], expected)

    def test_no_consent_is_recorded_when_oauthlib_answers_with_an_error(self):
        # oauthlib returns some errors as a redirect instead of raising them.
        answer = (f"{REDIRECT_URI}?error=invalid_scope&state=client-state", {}, "", 302)
        with mock.patch.object(
            OAuthLibCore, "create_authorization_response", return_value=answer
        ):
            response = self.decide()

        self.assert_refused(response)
        self.assertEqual(response.json()["error"], "invalid_scope")
        self.assertFalse(Event.objects.filter(event_type__startswith="oauth").exists())

    def test_a_code_in_the_registered_redirect_uri_is_not_an_issued_code(self):
        uri = f"{REDIRECT_URI}?code=registered"
        Application.objects.filter(pk=self.application.pk).update(redirect_uris=uri)
        answer = (f"{uri}&error=invalid_scope&state=client-state", {}, "", 302)
        with mock.patch.object(
            OAuthLibCore, "create_authorization_response", return_value=answer
        ):
            response = self.decide(redirect_uri=uri)

        self.assert_refused(response)
        self.assertEqual(response.json()["error"], "invalid_scope")
        self.assertFalse(Event.objects.filter(event_type__startswith="oauth").exists())

    def test_registered_redirect_uri_params_dont_get_in_the_way(self):
        for param in ("code", "error"):
            with self.subTest(param=param):
                uri = f"{REDIRECT_URI}?{param}=registered"
                Application.objects.filter(pk=self.application.pk).update(
                    redirect_uris=uri
                )

                response = self.decide(redirect_uri=uri)

                self.assertEqual(response.status_code, 200, response.content)
                query = query_of(response.json()["redirect_url"])
                self.assertTrue(Grant.objects.filter(code=query["code"][-1]).exists())

    def test_no_grant_is_left_without_its_audit_event(self):
        self.client.raise_request_exception = False
        with mock.patch(
            "waldur_mastermind.mcp.views.event_logger.emit", side_effect=RuntimeError
        ):
            response = self.decide()

        self.assertEqual(response.status_code, 500)
        self.assertFalse(Grant.objects.exists())

    def test_nameless_client_is_audited_by_its_redirect_host(self):
        Application.objects.filter(pk=self.application.pk).update(name="")

        self.decide()

        event = Event.objects.get(event_type="oauth_consent_granted")
        self.assertIn("OAuth client claude.ai access", event.message)

    def test_client_name_is_audited_without_control_or_bidi_characters(self):
        Application.objects.filter(pk=self.application.pk).update(
            name="Claude‮\nofficial"
        )

        self.decide()

        event = Event.objects.get(event_type="oauth_consent_granted")
        self.assertIn("OAuth client Claude official access", event.message)
        self.assertEqual(event.context["client_name"], "Claude official")

    def test_client_name_cannot_break_the_event_message(self):
        Application.objects.filter(pk=self.application.pk).update(name="{evil}")

        response = self.decide()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(
            "{evil}", Event.objects.get(event_type="oauth_consent_granted").message
        )


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL, PAT_ENABLED=True)
class DelegatedAccessTest(ConsentTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])

    def assert_forbidden(self):
        requests = [
            lambda: self.get_consent(),
            lambda: self.decide(allow=True),
            lambda: self.decide(allow=False),
        ]
        for request in requests:
            response = request()
            self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Grant.objects.exists())
        self.assertFalse(Event.objects.filter(event_type__startswith="oauth").exists())

    def test_personal_access_token_cannot_decide(self):
        pat = create_pat(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat.plaintext}")

        self.assert_forbidden()

    def test_impersonated_session_cannot_decide(self):
        staff = factories.UserFactory(is_staff=True)
        token, _ = Token.objects.get_or_create(user=staff)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {token.key}",
            HTTP_X_IMPERSONATED_USER_UUID=self.user.uuid.hex,
        )

        self.assert_forbidden()

    def test_oidc_access_token_cannot_decide(self):
        self.client.force_authenticate(self.user, token={"sub": "someone"})

        self.assert_forbidden()


class ConsentGatingTest(ConsentTestMixin, test.APITestCase):
    @override_config(MCP_ENABLED=False, MCP_RESOURCE_URLS=MCP_URL)
    def test_consent_is_not_served_while_mcp_is_disabled(self):
        self.assertEqual(self.get_consent().status_code, 404)
        self.login()
        self.assertEqual(self.get_consent().status_code, 404)
        self.assertEqual(self.decide().status_code, 404)
        self.assertFalse(Grant.objects.exists())

    @override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
    def test_consent_is_not_open_to_every_origin(self):
        self.login()

        response = self.client.get(CONSENT_URL, HTTP_ORIGIN="https://evil.example")

        self.assertNotEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
