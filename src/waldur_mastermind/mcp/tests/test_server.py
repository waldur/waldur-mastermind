import asyncio
import json
import threading
from datetime import timedelta
from unittest import mock

import httpx
from asgiref.sync import async_to_sync
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import DEFAULT_DB_ALIAS, OperationalError, connection, connections
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework import test
from rest_framework.authtoken.models import Token

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import PersonalAccessToken
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure.tests import factories, fixtures
from waldur_mastermind.mcp import auth, server

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
CALL_LIST_ORGANIZATIONS = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": "list_organizations", "arguments": {}},
}
ORIGIN = "http://localhost:6274"
PREFLIGHT_HEADERS = {
    "Origin": ORIGIN,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization, content-type, mcp-protocol-version",
}


def create_pat(user, scopes=None, expires_at=None, **kwargs):
    expires_at = expires_at or timezone.now() + timedelta(days=30)
    full_token, prefix, token_hash = PersonalAccessToken.generate_token(expires_at)
    pat = PersonalAccessToken.objects.create(
        user=user,
        name="mcp-pat",
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=scopes or [PermissionEnum.LIST_ORDERS.value],
        expires_at=expires_at,
        **kwargs,
    )
    pat.plaintext = full_token
    return pat


def parse_rpc(response):
    # Streamable HTTP answers with a single SSE event per request.
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    return response.json()


async def _request(method, path, body, headers, client_ip):
    app = server.build_app()
    transport = httpx.ASGITransport(app=app, client=(client_ip, 4321))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.request(method, path, json=body, headers=headers)


class CorsAssertionsMixin:
    def assert_readable_from_any_origin(self, response):
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertNotIn("access-control-allow-credentials", response.headers)

    def assert_challenge_readable_from_any_origin(self, response):
        self.assert_readable_from_any_origin(response)
        # A browser client reads resource_metadata and the scope from it.
        self.assertIn(
            "WWW-Authenticate", response.headers["access-control-expose-headers"]
        )


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class MCPServerTest(CorsAssertionsMixin, test.APITestCase):
    def setUp(self):
        # PAT usage and audit debounces live in the cache.
        cache.clear()
        # The test transaction is only visible on this thread.
        patcher = mock.patch.object(auth, "ORM_THREAD_SENSITIVE", True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])

    def request(
        self,
        body=INITIALIZE,
        authorization=None,
        method="POST",
        path="/mcp",
        client_ip="203.0.113.5",
        **headers,
    ):
        headers = {**MCP_HEADERS, **headers}
        if authorization:
            headers["Authorization"] = authorization
        return async_to_sync(_request)(method, path, body, headers, client_ip)

    def request_with(self, pat, body=INITIALIZE, **kwargs):
        return self.request(body, authorization=f"Bearer {pat.plaintext}", **kwargs)

    def assert_rejected(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.headers["www-authenticate"].startswith("Bearer"))

    def assert_rejection_audited(self, pat, reason):
        event = Event.objects.get(event_type="pat_authentication_rejected")
        self.assertEqual(event.context["pat_uuid"], pat.uuid.hex)
        self.assertEqual(event.context["reason"], reason)
        self.assertEqual(event.context["source_ip"], "203.0.113.5")

    def test_unauthenticated_request_is_rejected(self):
        self.assert_rejected(self.request())

    def test_valid_pat_can_initialize_and_list_tools(self):
        pat = create_pat(self.user)

        response = self.request_with(pat)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_rpc(response)["result"]["serverInfo"]["name"], "Waldur")

        response = self.request_with(pat, TOOLS_LIST)
        self.assertEqual(response.status_code, 200)
        tools = parse_rpc(response)["result"]["tools"]
        self.assertIn("list_organizations", [tool["name"] for tool in tools])

    def test_tool_acts_as_token_owner(self):
        pat = create_pat(self.user)

        response = self.request_with(pat, CALL_LIST_ORGANIZATIONS)

        result = parse_rpc(response)["result"]["structuredContent"]
        self.assertEqual(
            [organization["uuid"] for organization in result["organizations"]],
            [self.fixture.customer.uuid.hex],
        )

    def test_audit_event_records_the_address_seen_over_http(self):
        pat = create_pat(self.user)

        self.request_with(pat, CALL_LIST_ORGANIZATIONS)

        event = Event.objects.get(event_type="mcp_tool_called")
        self.assertEqual(event.context["ip_address"], "203.0.113.5")

    def test_valid_pat_records_usage(self):
        pat = create_pat(self.user)

        self.request_with(pat)

        pat.refresh_from_db()
        self.assertEqual(pat.use_count, 1)
        self.assertEqual(pat.last_used_ip, "203.0.113.5")

    def test_unknown_pat_is_rejected(self):
        # A future embedded expiry gets past the pre-check to the lookup.
        expires_at = timezone.now() + timedelta(days=1)
        token, _, _ = PersonalAccessToken.generate_token(expires_at)
        self.assert_rejected(self.request(authorization=f"Bearer {token}"))

    def test_expired_pat_is_rejected(self):
        pat = create_pat(self.user, expires_at=timezone.now() - timedelta(minutes=1))
        self.assert_rejected(self.request_with(pat))

    def test_revoked_pat_is_rejected(self):
        pat = create_pat(self.user, is_active=False)
        self.assert_rejected(self.request_with(pat))

    def test_pat_outside_network_acl_is_rejected(self):
        pat = create_pat(self.user, allowed_networks=["10.0.0.0/8"])
        self.assert_rejected(self.request_with(pat))

    def test_forwarded_address_is_checked_against_network_acl(self):
        pat = create_pat(self.user, allowed_networks=["10.0.0.0/8"])

        response = self.request_with(pat, **{"X-Forwarded-For": "10.1.2.3, 192.0.2.1"})

        self.assertEqual(response.status_code, 200)

    def test_inactive_user_is_rejected(self):
        pat = create_pat(self.user)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assert_rejected(self.request_with(pat))

    def test_user_without_pat_permission_is_rejected(self):
        pat = create_pat(self.user)
        self.user.can_use_personal_access_tokens = False
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        self.assert_rejected(self.request_with(pat))

    def test_entity_bound_pat_is_rejected_without_recording_usage(self):
        pat = create_pat(
            self.user,
            allowed_scopes=[
                {
                    "content_type_id": ContentType.objects.get_for_model(
                        self.fixture.customer
                    ).id,
                    "object_id": self.fixture.customer.id,
                }
            ],
        )

        self.assert_rejected(self.request_with(pat))

        pat.refresh_from_db()
        self.assertEqual(pat.use_count, 0)
        self.assert_rejection_audited(pat, "mcp_entity_bound")

    def test_staff_pat_without_staff_access_is_rejected(self):
        staff = factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        pat = create_pat(staff, scopes=[PermissionEnum.SUPPORT_ACCESS.value])
        self.assert_rejected(self.request_with(pat))
        self.assert_rejection_audited(pat, "mcp_staff_access_missing")

    def test_staff_and_support_pat_with_only_support_access_is_rejected(self):
        # REST would grant support-level access, but tools see is_staff and
        # cannot narrow it, so the token is refused rather than widened.
        user = factories.UserFactory(
            is_staff=True, is_support=True, can_use_personal_access_tokens=True
        )
        pat = create_pat(user, scopes=[PermissionEnum.SUPPORT_ACCESS.value])
        self.assert_rejected(self.request_with(pat))

    def test_staff_pat_with_staff_access_is_accepted(self):
        staff = factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        pat = create_pat(staff, scopes=[PermissionEnum.STAFF_ACCESS.value])
        self.assertEqual(self.request_with(pat).status_code, 200)

    def test_support_pat_without_support_access_is_rejected(self):
        support = factories.UserFactory(
            is_support=True, can_use_personal_access_tokens=True
        )
        pat = create_pat(support)
        self.assert_rejected(self.request_with(pat))
        self.assert_rejection_audited(pat, "mcp_support_access_missing")

    def test_support_pat_with_support_access_is_accepted(self):
        support = factories.UserFactory(
            is_support=True, can_use_personal_access_tokens=True
        )
        pat = create_pat(support, scopes=[PermissionEnum.SUPPORT_ACCESS.value])
        self.assertEqual(self.request_with(pat).status_code, 200)

    def test_api_token_is_rejected(self):
        key = Token.objects.get_or_create(user=self.user)[0].key
        self.assert_rejected(self.request(authorization=f"Token {key}"))
        self.assert_rejected(self.request(authorization=f"Bearer {key}"))

    @override_config(PAT_ENABLED=False)
    def test_pat_is_rejected_while_pats_are_disabled(self):
        pat = create_pat(self.user)
        self.assert_rejected(self.request_with(pat))

    @override_config(MCP_ENABLED=False)
    def test_mcp_is_not_served_while_disabled(self):
        pat = create_pat(self.user)
        self.assertEqual(self.request_with(pat).status_code, 404)
        self.assertEqual(self.request().status_code, 404)

    @override_config(MCP_ENABLED=False)
    def test_other_paths_read_no_settings(self):
        with mock.patch.object(server, "is_mcp_enabled") as enabled:
            response = self.request(None, method="GET", path="/other")

        self.assertEqual(response.status_code, 404)
        enabled.assert_not_called()

    @override_config(MCP_ENABLED=False)
    def test_health_is_served_while_disabled(self):
        response = self.request(None, method="GET", path="/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_trailing_slash_is_served_like_mcp(self):
        pat = create_pat(self.user)

        response = self.request_with(pat, path="/mcp/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_rpc(response)["result"]["serverInfo"]["name"], "Waldur")
        self.assert_rejected(self.request(path="/mcp/"))

    def test_no_path_is_redirected(self):
        for path in ["/mcp//", "/health/"]:
            with self.subTest(path=path):
                response = self.request(path=path)

                self.assertEqual(response.status_code, 404)
                self.assertNotIn("location", response.headers)

    def test_preflight_from_any_origin_is_allowed(self):
        response = self.request(None, method="OPTIONS", **PREFLIGHT_HEADERS)

        self.assertEqual(response.status_code, 200)
        self.assert_readable_from_any_origin(response)
        allowed = response.headers["access-control-allow-headers"].lower()
        for header in ["authorization", "content-type", "mcp-protocol-version"]:
            self.assertIn(header, allowed)

    def test_preflight_allows_the_routing_headers_of_modern_requests(self):
        requested = "authorization, content-type, mcp-method, mcp-name, mcp-param-region, mcp-protocol-version"

        response = self.request(
            None,
            method="OPTIONS",
            **{**PREFLIGHT_HEADERS, "Access-Control-Request-Headers": requested},
        )

        self.assertEqual(response.status_code, 200)
        allowed = response.headers["access-control-allow-headers"].lower()
        for header in ["mcp-method", "mcp-name", "mcp-param-region"]:
            self.assertIn(header, allowed)

    def test_answers_are_readable_from_any_origin(self):
        pat = create_pat(self.user)
        response = self.request_with(pat, Origin=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assert_readable_from_any_origin(response)

        response = self.request(Origin=ORIGIN)
        self.assert_rejected(response)
        self.assert_challenge_readable_from_any_origin(response)

    @override_config(MCP_ENABLED=False)
    def test_not_found_answer_is_readable_too(self):
        response = self.request(Origin=ORIGIN)

        self.assertEqual(response.status_code, 404)
        self.assert_readable_from_any_origin(response)


class DropBrokenConnectionsTest(test.APITestCase):
    def test_only_broken_connections_are_closed(self):
        broken = mock.Mock(**{"is_usable.return_value": False})
        healthy = mock.Mock(**{"is_usable.return_value": True})
        unopened = mock.Mock(connection=None)

        with mock.patch.object(auth, "connections") as connections:
            connections.all.return_value = [broken, healthy, unopened]
            auth.drop_broken_connections()

        broken.close.assert_called_once_with()
        healthy.close.assert_not_called()
        unopened.close.assert_not_called()


def without_statement_timeout(test_case):
    # Bounding statements opens a transaction, so a database these tests don't have.
    patcher = mock.patch.object(auth, "statement_timeout_ms", 0)
    patcher.start()
    test_case.addCleanup(patcher.stop)


class RunOrmAsyncTest(SimpleTestCase):
    def setUp(self):
        without_statement_timeout(self)

    def test_orm_calls_leave_the_calling_thread(self):
        caller = threading.get_ident()

        ran_on = async_to_sync(auth.run_orm_async)(threading.get_ident)

        self.assertNotEqual(ran_on, caller)

    def test_orm_calls_are_bounded_by_the_thread_pool(self):
        barrier = threading.Barrier(2, timeout=5)

        def call():
            # Holds two threads at once, so a pool smaller than requested shows.
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return threading.current_thread()

        async def run_many():
            return await asyncio.gather(*[auth.run_orm_async(call) for _ in range(8)])

        auth.configure_orm_threads(2)
        self.addCleanup(auth.configure_orm_threads, auth.DEFAULT_ORM_THREADS)

        threads = async_to_sync(run_many)()

        self.assertEqual(len({thread.ident for thread in threads}), 2)
        self.assertTrue(all(t.name.startswith("mcp-orm") for t in threads))


def show_statement_timeout():
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        return cursor.fetchone()[0]


class StatementTimeoutTest(SimpleTestCase):
    """On a real pool thread, with its own connection to the test database."""

    databases = {DEFAULT_DB_ALIAS}

    def setUp(self):
        auth.configure_orm_threads(1)
        self.addCleanup(auth.configure_orm_threads, auth.DEFAULT_ORM_THREADS)
        # Before the pool is replaced, so the connection doesn't outlive the test.
        self.addCleanup(
            lambda: auth.orm_executor.submit(connections.close_all).result()
        )
        patcher = mock.patch.object(auth, "statement_timeout_ms", 100)
        patcher.start()
        self.addCleanup(patcher.stop)

    def on_pool(self, func):
        return async_to_sync(auth.run_orm_async)(func)

    def test_slow_query_is_cancelled_and_the_thread_serves_the_next_call(self):
        threads = []

        def slow():
            threads.append(threading.get_ident())
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_sleep(5)")

        with self.assertRaisesMessage(OperationalError, "statement timeout"):
            self.on_pool(slow)
        threads.append(self.on_pool(threading.get_ident))
        self.assertEqual(self.on_pool(show_statement_timeout), "100ms")

        self.assertEqual(threads[0], threads[1])

    def test_timeout_does_not_outlive_the_call(self):
        default = auth.orm_executor.submit(show_statement_timeout).result()

        self.assertEqual(self.on_pool(show_statement_timeout), "100ms")
        # The same thread and connection, outside run_orm.
        after = auth.orm_executor.submit(show_statement_timeout).result()

        self.assertNotEqual(default, "100ms")
        self.assertEqual(after, default)

    def test_zero_turns_it_off(self):
        default = auth.orm_executor.submit(show_statement_timeout).result()
        auth.statement_timeout_ms = 0

        self.assertEqual(self.on_pool(show_statement_timeout), default)


class BusyPoolTest(SimpleTestCase):
    """The gate and token checks share the pool with tool calls.

    A separate pool would only answer refusals sooner: an accepted request
    still waits for a thread to run its tool, and every thread is one more
    connection. The statement timeout bounds how long any thread stays busy.
    """

    def setUp(self):
        without_statement_timeout(self)
        auth.configure_orm_threads(1)
        self.addCleanup(auth.configure_orm_threads, auth.DEFAULT_ORM_THREADS)

    def test_gate_check_waits_for_a_thread_and_then_answers(self):
        release = threading.Event()
        app = mock.AsyncMock()
        gate = server.MCPEnabledGate(app)

        async def scenario():
            busy = asyncio.ensure_future(auth.run_orm_async(release.wait, 5))
            checked = asyncio.ensure_future(
                gate({"type": "http", "path": "/mcp"}, None, None)
            )
            await asyncio.sleep(0.2)
            waited = not checked.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(busy, checked), 5)
            return waited

        with mock.patch.object(server, "is_mcp_enabled", return_value=True):
            waited = async_to_sync(scenario)()

        self.assertTrue(waited)
        app.assert_awaited_once()

    def test_token_check_waits_for_a_thread_and_then_answers(self):
        release = threading.Event()
        app = mock.AsyncMock()
        middleware = auth.AuthMiddleware(app)
        token = mock.Mock(claims={}, scopes=[])
        scope = {"type": "http", "path": "/mcp", "headers": []}

        async def scenario():
            busy = asyncio.ensure_future(auth.run_orm_async(release.wait, 5))
            checked = asyncio.ensure_future(middleware(scope, None, None))
            await asyncio.sleep(0.2)
            waited = not checked.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(busy, checked), 5)
            return waited

        with mock.patch.object(auth, "authenticate", return_value=(token, None)):
            reset_token = auth.client_ip_var.set(None)
            try:
                waited = async_to_sync(scenario)()
            finally:
                auth.client_ip_var.reset(reset_token)

        self.assertTrue(waited)
        app.assert_awaited_once()


class CommandTest(SimpleTestCase):
    def run_command(self, *args):
        with (
            mock.patch("uvicorn.run"),
            mock.patch.object(auth, "configure_orm_threads"),
            mock.patch.object(auth, "configure_statement_timeout") as configure,
        ):
            call_command("mcp_server", *args)
        return configure

    def test_statement_timeout_has_a_default(self):
        configure = self.run_command()

        configure.assert_called_once_with(auth.DEFAULT_STATEMENT_TIMEOUT)

    def test_statement_timeout_is_configurable(self):
        configure = self.run_command("--statement-timeout", "5")

        configure.assert_called_once_with(5)

    def test_negative_statement_timeout_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command("--statement-timeout", "-1")

    def test_statement_timeout_above_the_postgres_limit_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command("--statement-timeout", str(auth.MAX_STATEMENT_TIMEOUT + 1))

    def test_statement_timeout_at_the_postgres_limit_is_accepted(self):
        configure = self.run_command(
            "--statement-timeout", str(auth.MAX_STATEMENT_TIMEOUT)
        )

        configure.assert_called_once_with(auth.MAX_STATEMENT_TIMEOUT)
