from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from oauth2_provider.settings import oauth2_settings
from oauthlib.common import generate_token
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import User
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories, fixtures
from waldur_mastermind.mcp import auth, oauth, server
from waldur_mastermind.mcp.models import ConnectedApp
from waldur_mastermind.mcp.tests.test_consent import ConsentTestMixin
from waldur_mastermind.mcp.tests.test_oauth import (
    MCP_URL,
    VERIFIER,
    AccessToken,
    OAuthTestMixin,
    token_hash,
)
from waldur_mastermind.mcp.tests.test_server import (
    CALL_LIST_ORGANIZATIONS,
    INITIALIZE,
    MCP_HEADERS,
    ORIGIN,
    PREFLIGHT_HEADERS,
    TOOLS_LIST,
    CorsAssertionsMixin,
    _request,
    parse_rpc,
)
from waldur_mastermind.mcp.tests.test_tools import (
    END_USER_TOOLS,
    SUPPORT_TOOLS,
    call,
    listed_names,
    texts,
)

METADATA_URL = "https://api.example.com/.well-known/oauth-protected-resource/mcp"
OTHER_MCP_URL = "https://waldur.example.org/mcp"
PROXIED_MCP_URL = "https://api.example.com/api/mcp"


class ResourceServerTestCase(CorsAssertionsMixin, OAuthTestMixin, test.APITestCase):
    def setUp(self):
        # Registration throttles live in the cache.
        cache.clear()
        # The test transaction is only visible on this thread.
        patcher = mock.patch.object(auth, "ORM_THREAD_SENSITIVE", True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.application = self.create_public_client()
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner

    def create_token(
        self, user, scope="mcp:read", resource=(MCP_URL,), application=None, **kwargs
    ) -> str:
        # As DOT makes them: letters and digits, so never a PAT's w_ prefix.
        raw = generate_token()
        AccessToken.objects.create(
            application=application or self.application,
            user=user,
            token_checksum=token_hash(raw),
            scope=scope,
            resource=list(resource),
            expires=kwargs.pop("expires", timezone.now() + timedelta(hours=1)),
            **kwargs,
        )
        return raw

    def request(
        self, body=INITIALIZE, token=None, method="POST", path="/mcp", **headers
    ):
        headers = {**MCP_HEADERS, **headers}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return async_to_sync(_request)(method, path, body, headers, "203.0.113.5")

    def get_metadata(self, path="/mcp", **headers):
        return self.request(
            None, method="GET", path=oauth.RESOURCE_METADATA_PATH + path, **headers
        )

    def assert_rejected(self, response):
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(
            response.headers["www-authenticate"],
            'Bearer error="invalid_token", error_description="Authentication '
            f'required.", resource_metadata="{METADATA_URL}"',
        )

    def assert_insufficient_scope(self, response, missing_scope):
        self.assertEqual(response.status_code, 403, response.text)
        challenge = response.headers["www-authenticate"]
        self.assertTrue(challenge.startswith('Bearer error="insufficient_scope"'))
        self.assertIn(f'scope="mcp:read {missing_scope}"', challenge)
        self.assertIn(f'resource_metadata="{METADATA_URL}"', challenge)
        self.assertEqual(response.json()["error"], "insufficient_scope")


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class DiscoveryTest(ResourceServerTestCase):
    def test_unauthenticated_request_points_to_the_resource_metadata(self):
        response = self.request()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.headers["www-authenticate"],
            f'Bearer resource_metadata="{METADATA_URL}"',
        )

    @override_settings(ALLOWED_HOSTS=["api.example.com"])
    def test_metadata_names_the_api_as_authorization_server(self):
        response = self.get_metadata()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "resource": MCP_URL,
                "authorization_servers": ["https://api.example.com"],
                "scopes_supported": ["mcp:read"],
                "bearer_methods_supported": ["header"],
            },
        )
        # Clients fetch the authorization server's metadata and compare issuers.
        issuer = self.client.get(
            "/.well-known/oauth-authorization-server",
            HTTP_HOST="api.example.com",
            secure=True,
        ).json()["issuer"]
        self.assertEqual(response.json()["authorization_servers"], [issuer])

    @override_config(MCP_RESOURCE_URLS=f"{MCP_URL} {OTHER_MCP_URL}")
    def test_metadata_follows_the_host_the_client_reached(self):
        response = self.get_metadata(Host="waldur.example.org")

        self.assertEqual(response.json()["resource"], OTHER_MCP_URL)
        self.assertEqual(
            response.json()["authorization_servers"], ["https://waldur.example.org"]
        )
        self.assertEqual(
            self.request(Host="WALDUR.example.org").headers["www-authenticate"],
            'Bearer resource_metadata="https://waldur.example.org'
            '/.well-known/oauth-protected-resource/mcp"',
        )

    @override_config(MCP_RESOURCE_URLS=f"{MCP_URL} {OTHER_MCP_URL}")
    def test_metadata_for_an_unknown_host_names_the_first_url(self):
        self.assertEqual(self.get_metadata().json()["resource"], MCP_URL)

    @override_config(MCP_RESOURCE_URLS=PROXIED_MCP_URL)
    def test_metadata_is_served_under_the_configured_path(self):
        challenge = self.request().headers["www-authenticate"]
        self.assertEqual(
            challenge,
            'Bearer resource_metadata="https://api.example.com'
            '/.well-known/oauth-protected-resource/api/mcp"',
        )

        response = self.get_metadata("/api/mcp")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["resource"], PROXIED_MCP_URL)
        self.assertEqual(self.get_metadata().status_code, 404)

    @override_config(MCP_RESOURCE_URLS=f"{MCP_URL} {PROXIED_MCP_URL}")
    def test_metadata_names_the_url_with_the_requested_path(self):
        for path, expected in [("/mcp", MCP_URL), ("/api/mcp/", PROXIED_MCP_URL)]:
            with self.subTest(path=path):
                self.assertEqual(self.get_metadata(path).json()["resource"], expected)

        for path in ["", "/other"]:
            with self.subTest(path=path):
                self.assertEqual(self.get_metadata(path).status_code, 404)

    def test_pinned_issuer_is_named(self):
        with mock.patch.object(
            oauth2_settings, "OIDC_ISS_ENDPOINT", "https://auth.example.com"
        ):
            response = self.get_metadata()

        self.assertEqual(
            response.json()["authorization_servers"], ["https://auth.example.com"]
        )

    @override_config(MCP_RESOURCE_URLS="")
    def test_without_resource_urls_there_is_no_metadata(self):
        self.assertEqual(self.get_metadata().status_code, 404)
        self.assertEqual(self.request().headers["www-authenticate"], "Bearer")

    @override_config(MCP_ENABLED=False)
    def test_metadata_is_not_served_while_mcp_is_disabled(self):
        self.assertEqual(self.get_metadata().status_code, 404)

    def test_metadata_is_readable_from_any_origin(self):
        preflight = self.request(
            None,
            method="OPTIONS",
            path=oauth.RESOURCE_METADATA_PATH + "/mcp",
            **{**PREFLIGHT_HEADERS, "Access-Control-Request-Method": "GET"},
        )
        response = self.get_metadata(Origin=ORIGIN)

        for answer in [preflight, response]:
            with self.subTest(method=answer.request.method):
                self.assertEqual(answer.status_code, 200)
                self.assert_readable_from_any_origin(answer)


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class TokenTest(ResourceServerTestCase):
    def test_token_acts_as_its_user(self):
        token = self.create_token(self.user)

        response = self.request(CALL_LIST_ORGANIZATIONS, token=token)

        self.assertEqual(response.status_code, 200)
        result = parse_rpc(response)["result"]["structuredContent"]
        self.assertEqual(
            [organization["uuid"] for organization in result["organizations"]],
            [self.fixture.customer.uuid.hex],
        )

    def test_token_for_another_resource_is_rejected(self):
        for resource in [("https://other.example.com/mcp",), ()]:
            with self.subTest(resource=resource):
                token = self.create_token(self.user, resource=resource)
                self.assert_rejected(self.request(token=token))

    def test_token_for_a_resource_no_longer_served_is_rejected(self):
        token = self.create_token(self.user)

        with override_config(MCP_RESOURCE_URLS=OTHER_MCP_URL):
            response = self.request(token=token)

        self.assertEqual(response.status_code, 401)

    def test_token_without_the_read_scope_is_rejected(self):
        self.assert_rejected(self.request(token=self.create_token(self.user, "")))

    def test_expired_token_is_rejected(self):
        token = self.create_token(self.user, expires=timezone.now())
        self.assert_rejected(self.request(token=token))

    def test_token_of_an_inactive_user_is_rejected(self):
        token = self.create_token(self.user)
        # Saving would also delete the token, leaving this check unexercised.
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        self.assert_rejected(self.request(token=token))

    def test_registration_token_is_rejected(self):
        response = self.client.post(
            "/oauth/register/",
            {
                "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "token_endpoint_auth_method": "none",
            },
            format="json",
        )
        token = response.json()["registration_access_token"]

        self.assert_rejected(self.request(token=token))

    def test_unknown_token_is_rejected(self):
        self.assert_rejected(self.request(token="not-a-token"))

    def test_unknown_token_reads_no_settings(self):
        with mock.patch.object(oauth, "get_allowed_resources") as allowed:
            self.assertIsNone(auth.load_oauth_token("not-a-token"))

        allowed.assert_not_called()

    def test_refusal_takes_one_trip_to_the_orm_threads(self):
        with mock.patch.object(
            auth, "run_orm_async", wraps=auth.run_orm_async
        ) as trips:
            self.assert_rejected(self.request(token="not-a-token"))

        self.assertEqual(trips.await_count, 1)

    def test_revoked_token_is_rejected(self):
        token = self.create_token(self.user)
        self.assertEqual(self.request(token=token).status_code, 200)

        response = self.post_form(
            "/oauth/revoke_token/",
            {"token": token, "client_id": self.application.client_id},
        )
        self.assertEqual(response.status_code, 200)

        self.assert_rejected(self.request(token=token))

    def test_revoking_the_connected_app_rejects_its_token(self):
        token = self.create_token(self.user)
        self.assertEqual(self.request(token=token).status_code, 200)
        connected_app = ConnectedApp.objects.get(
            user=self.user, application=self.application
        )

        self.client.force_authenticate(self.user)
        response = self.client.delete(
            f"/api/oauth-connected-apps/{connected_app.uuid.hex}/"
        )
        self.assertEqual(response.status_code, 204)

        self.assert_rejected(self.request(token=token))

    def test_token_revoked_after_verification_is_refused(self):
        raw = self.create_token(self.user)
        token = auth.verify_oauth_token(raw)
        AccessToken.objects.filter(token_checksum=token_hash(raw)).delete()

        result = call(token, "list_projects")

        self.assertTrue(result.is_error)
        self.assertEqual(texts(result), [server.INVALID_TOKEN])

    def test_same_client_answers_each_user_with_their_own_data(self):
        other = fixtures.CustomerFixture()
        seen = {}
        for user in (self.user, other.owner):
            response = self.request(
                CALL_LIST_ORGANIZATIONS, token=self.create_token(user)
            )
            result = parse_rpc(response)["result"]["structuredContent"]
            seen[user] = {item["uuid"] for item in result["organizations"]}

        self.assertEqual(seen[self.user], {self.fixture.customer.uuid.hex})
        self.assertEqual(seen[other.owner], {other.customer.uuid.hex})

    def test_tools_follow_the_role_not_pat_permissions(self):
        support = factories.UserFactory(is_support=True)
        cases = [
            (self.user, "mcp:read", END_USER_TOOLS),
            (support, "mcp:read mcp:support", SUPPORT_TOOLS),
        ]
        for user, scope, expected in cases:
            with self.subTest(scope=scope):
                token = auth.verify_oauth_token(self.create_token(user, scope))
                self.assertEqual(set(listed_names(token)), expected)

    def test_tools_list_over_http(self):
        response = self.request(TOOLS_LIST, token=self.create_token(self.user))

        names = {tool["name"] for tool in parse_rpc(response)["result"]["tools"]}
        self.assertEqual(names, END_USER_TOOLS)


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class ElevatedScopeTest(ResourceServerTestCase):
    def setUp(self):
        super().setUp()
        self.staff = factories.UserFactory(is_staff=True)
        self.support = factories.UserFactory(is_support=True)
        self.other_project = fixtures.ProjectFixture().project

    def test_staff_token_without_staff_scope_gets_insufficient_scope(self):
        for scope in ["mcp:read", "mcp:read mcp:support"]:
            with self.subTest(scope=scope):
                token = self.create_token(self.staff, scope)
                self.assert_insufficient_scope(self.request(token=token), "mcp:staff")

    def test_support_token_without_support_scope_gets_insufficient_scope(self):
        token = self.create_token(self.support)
        self.assert_insufficient_scope(self.request(token=token), "mcp:support")

    def test_insufficient_scope_challenge_is_readable_from_any_origin(self):
        token = self.create_token(self.support)

        response = self.request(token=token, Origin=ORIGIN)

        self.assert_insufficient_scope(response, "mcp:support")
        self.assert_challenge_readable_from_any_origin(response)

    def test_staff_and_support_needs_the_staff_scope(self):
        user = factories.UserFactory(is_staff=True, is_support=True)
        token = self.create_token(user, "mcp:read mcp:support")
        self.assert_insufficient_scope(self.request(token=token), "mcp:staff")

    def test_elevated_scope_is_accepted(self):
        cases = [
            (self.staff, "mcp:read mcp:staff"),
            (self.support, "mcp:read mcp:support"),
            (self.support, "mcp:read mcp:staff"),
        ]
        for user, scope in cases:
            with self.subTest(user=user.username, scope=scope):
                token = self.create_token(user, scope)
                self.assertEqual(self.request(token=token).status_code, 200)

    def test_elevated_scope_without_the_role_acts_as_the_user(self):
        token = auth.verify_oauth_token(
            self.create_token(self.user, "mcp:read mcp:staff")
        )

        self.assertEqual(set(listed_names(token)), END_USER_TOOLS)

    def test_staff_and_support_see_what_the_portal_shows(self):
        cases = [
            (self.staff, "mcp:read mcp:staff"),
            (self.support, "mcp:read mcp:support"),
        ]
        for user, scope in cases:
            with self.subTest(user=user.username):
                token = auth.verify_oauth_token(self.create_token(user, scope))
                result = call(token, "list_projects")
                seen = {item["uuid"] for item in result.structured_content["projects"]}

                self.client.force_authenticate(user)
                response = self.client.get(factories.ProjectFactory.get_list_url())
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(seen, {item["uuid"] for item in response.data})
                self.assertIn(self.other_project.uuid.hex, seen)

    def test_role_gained_after_verification_is_refused_at_the_call(self):
        token = auth.verify_oauth_token(self.create_token(self.user))
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])

        result = call(token, "list_projects")

        self.assertEqual(texts(result), [server.INVALID_TOKEN])


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class AuditTest(ResourceServerTestCase):
    def tool_event(self):
        return Event.objects.get(event_type="mcp_tool_called")

    def test_tool_call_event_names_the_oauth_client(self):
        self.request(CALL_LIST_ORGANIZATIONS, token=self.create_token(self.user))

        event = self.tool_event()
        self.assertEqual(event.context["client"], "Claude")
        self.assertEqual(event.context["oauth_client_id"], self.application.client_id)
        self.assertEqual(event.context["ip_address"], "203.0.113.5")
        self.assertNotIn("pat_uuid", event.context)
        self.assertIn("with Claude.", event.message)

    def test_nameless_client_is_named_as_in_connected_apps(self):
        self.application.name = ""
        self.application.save(update_fields=["name"])

        self.request(CALL_LIST_ORGANIZATIONS, token=self.create_token(self.user))

        self.assertEqual(self.tool_event().context["client"], "claude.ai")


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class UsageTest(ResourceServerTestCase):
    def last_used_at(self, application=None):
        return ConnectedApp.objects.get(
            user=self.user, application=application or self.application
        ).last_used_at

    def test_use_is_recorded_on_the_connected_app(self):
        # The row is made when the token is issued.
        token = self.create_token(self.user)
        self.assertIsNone(self.last_used_at())
        before = timezone.now()

        self.request(token=token)

        self.assertGreaterEqual(self.last_used_at(), before)

    def test_use_is_recorded_at_most_every_ten_minutes(self):
        token = self.create_token(self.user)
        recent = timezone.now() - timedelta(minutes=5)
        ConnectedApp.objects.update(last_used_at=recent)

        self.request(token=token)
        self.assertEqual(self.last_used_at(), recent)

        stale = timezone.now() - timedelta(minutes=11)
        ConnectedApp.objects.update(last_used_at=stale)

        self.request(token=token)
        self.assertGreater(self.last_used_at(), stale)

    def test_use_is_recorded_per_client(self):
        other_application = self.create_public_client()
        self.create_token(self.user)
        token = self.create_token(self.user, application=other_application)

        self.request(token=token)

        self.assertIsNotNone(self.last_used_at(other_application))
        self.assertIsNone(self.last_used_at())

    def test_refused_request_records_no_use(self):
        staff = factories.UserFactory(is_staff=True)
        token = self.create_token(staff)

        self.assert_insufficient_scope(self.request(token=token), "mcp:staff")

        self.assertIsNone(
            ConnectedApp.objects.get(
                user=staff, application=self.application
            ).last_used_at
        )

    def test_token_without_a_connection_still_works(self):
        # Tokens issued before connections were recorded have no row.
        token = self.create_token(self.user)
        ConnectedApp.objects.all().delete()

        response = self.request(token=token)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ConnectedApp.objects.exists())


@override_config(MCP_ENABLED=True, MCP_RESOURCE_URLS=MCP_URL)
class StepUpTest(ConsentTestMixin, ResourceServerTestCase):
    """A staff user connects, is asked for the staff scope, and approves it."""

    def setUp(self):
        ResourceServerTestCase.setUp(self)
        self.user = factories.UserFactory(is_staff=True)

    def connect(self, scope):
        self.login()
        decision = self.decide(scope=scope)
        self.assertEqual(decision.status_code, 200, decision.content)
        code = parse_qs(urlparse(decision.json()["redirect_url"]).query)["code"][0]
        self.client.credentials()
        response = self.post_form(
            "/oauth/token/",
            {
                "grant_type": "authorization_code",
                "client_id": self.application.client_id,
                "code": code,
                "redirect_uri": self.params()["redirect_uri"],
                "code_verifier": VERIFIER,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["access_token"]

    def test_staff_user_steps_up_to_the_staff_scope(self):
        response = self.request(token=self.connect("mcp:read"))
        self.assert_insufficient_scope(response, "mcp:staff")

        response = self.request(token=self.connect("mcp:read mcp:staff"))
        self.assertEqual(response.status_code, 200)

    def test_staff_scope_alone_connects(self):
        response = self.request(token=self.connect("mcp:staff"))

        self.assertEqual(response.status_code, 200)
