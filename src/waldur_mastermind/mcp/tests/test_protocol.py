import asyncio
from unittest import mock

import httpx
from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import SimpleTestCase
from rest_framework import test

from waldur_core import __version__
from waldur_core.configuration.testing import override_config
from waldur_core.core import models
from waldur_core.structure.tests import fixtures
from waldur_mastermind.mcp import auth, server
from waldur_mastermind.mcp.tests.test_server import (
    CALL_LIST_ORGANIZATIONS,
    INITIALIZE,
    MCP_HEADERS,
    TOOLS_LIST,
    create_pat,
    parse_rpc,
)

# This revision has no initialize handshake: each request describes its client.
MODERN_VERSION = "2026-07-28"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


async def send_concurrently(requests):
    app = server.build_app()
    transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 4321))
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            calls = [client.request(**request) for request in requests]
            # A held-open stream fails the test instead of hanging it.
            return await asyncio.wait_for(asyncio.gather(*calls), timeout=30)


def bearer(pat):
    return {"Authorization": f"Bearer {pat.plaintext}"}


def post(body, *header_sets):
    headers = dict(MCP_HEADERS)
    for header_set in header_sets:
        headers.update(header_set)
    return {"method": "POST", "url": "/mcp", "json": body, "headers": headers}


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class MCPProtocolTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        # The test transaction is only visible on this thread.
        patcher = mock.patch.object(auth, "ORM_THREAD_SENSITIVE", True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.customer, self.user, self.pat = self.create_owner()

    def create_owner(self):
        fixture = fixtures.CustomerFixture()
        user = fixture.owner
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        return fixture.customer, user, create_pat(user)

    def send(self, *requests):
        return async_to_sync(send_concurrently)(requests)

    def assert_lists_only(self, response, customer):
        self.assertEqual(response.status_code, 200)
        data = parse_rpc(response)["result"]["structuredContent"]
        self.assertEqual(
            [organization["uuid"] for organization in data["organizations"]],
            [customer.uuid.hex],
        )

    def test_handshake_clients_can_initialize_list_and_call(self):
        for version in ("2025-06-18", "2025-11-25"):
            with self.subTest(version=version):
                headers = {**bearer(self.pat), "MCP-Protocol-Version": version}
                initialize = {
                    **INITIALIZE,
                    "params": {**INITIALIZE["params"], "protocolVersion": version},
                }

                initialized, listed, called = self.send(
                    post(initialize, headers),
                    post(TOOLS_LIST, headers),
                    post(CALL_LIST_ORGANIZATIONS, headers),
                )

                self.assertEqual(
                    parse_rpc(initialized)["result"]["protocolVersion"], version
                )
                tools = parse_rpc(listed)["result"]["tools"]
                tool = next(t for t in tools if t["name"] == "list_organizations")
                self.assertTrue(tool["annotations"]["readOnlyHint"])
                self.assertIn("outputSchema", tool)
                self.assert_lists_only(called, self.customer)

    def test_modern_client_can_list_and_call_without_initializing(self):
        headers = {**bearer(self.pat), "MCP-Protocol-Version": MODERN_VERSION}
        tools_list = {**TOOLS_LIST, "params": {"_meta": MODERN_META}}
        call_tool = {
            **CALL_LIST_ORGANIZATIONS,
            "params": {**CALL_LIST_ORGANIZATIONS["params"], "_meta": MODERN_META},
        }

        listed, called = self.send(
            post(tools_list, headers, {"Mcp-Method": "tools/list"}),
            post(
                call_tool,
                headers,
                {"Mcp-Method": "tools/call", "Mcp-Name": "list_organizations"},
            ),
        )

        tools = parse_rpc(listed)["result"]["tools"]
        self.assertIn("list_organizations", [tool["name"] for tool in tools])
        self.assert_lists_only(called, self.customer)

    def test_modern_request_needs_a_token(self):
        tools_list = {**TOOLS_LIST, "params": {"_meta": MODERN_META}}
        headers = {"MCP-Protocol-Version": MODERN_VERSION, "Mcp-Method": "tools/list"}

        (response,) = self.send(post(tools_list, headers))

        self.assertEqual(response.status_code, 401)

    def test_initialize_falls_back_to_the_latest_handshake_revision(self):
        initialize = {
            **INITIALIZE,
            "params": {**INITIALIZE["params"], "protocolVersion": MODERN_VERSION},
        }
        headers = {
            key: value
            for key, value in MCP_HEADERS.items()
            if key != "MCP-Protocol-Version"
        }
        request = post(initialize, bearer(self.pat))
        request["headers"] = {**headers, **bearer(self.pat)}

        (response,) = self.send(request)

        self.assertEqual(parse_rpc(response)["result"]["protocolVersion"], "2025-11-25")

    def test_server_advertises_only_tools_and_its_own_version(self):
        (response,) = self.send(post(INITIALIZE, bearer(self.pat)))

        result = parse_rpc(response)["result"]
        self.assertEqual(list(result["capabilities"]), ["tools"])
        self.assertEqual(result["serverInfo"]["version"], __version__)

    def test_get_is_refused_rather_than_held_open(self):
        for version in ("2025-06-18", MODERN_VERSION):
            with self.subTest(version=version):
                (response,) = self.send(
                    {
                        "method": "GET",
                        "url": "/mcp",
                        "headers": {
                            "Accept": "text/event-stream",
                            "MCP-Protocol-Version": version,
                            **bearer(self.pat),
                        },
                    }
                )

                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.headers["allow"], "POST")

    def test_delete_is_refused_without_touching_the_token(self):
        (response,) = self.send(
            {"method": "DELETE", "url": "/mcp", "headers": bearer(self.pat)}
        )

        self.assertEqual(response.status_code, 405)
        self.pat.refresh_from_db()
        self.assertIsNone(self.pat.last_used_at)

    def test_concurrent_callers_keep_their_own_identity(self):
        other_customer, _, other_pat = self.create_owner()

        responses = self.send(
            *[
                post(CALL_LIST_ORGANIZATIONS, bearer(pat))
                for _ in range(10)
                for pat in (self.pat, other_pat)
            ]
        )

        for index, response in enumerate(responses):
            expected = self.customer if index % 2 == 0 else other_customer
            self.assert_lists_only(response, expected)


class ClientIPMiddlewareTest(SimpleTestCase):
    def test_concurrent_requests_keep_their_own_address(self):
        seen = {}

        async def app(scope, receive, send):
            # Lets the other request set its address before this one reads.
            await asyncio.sleep(0)
            seen[scope["client"][0]] = auth.client_ip_var.get()

        middleware = auth.ClientIPMiddleware(app)

        def http_scope(address):
            return {"type": "http", "headers": [], "client": (address, 4321)}

        async def run():
            await asyncio.gather(
                middleware(http_scope("10.1.2.3"), None, None),
                middleware(http_scope("192.0.2.1"), None, None),
            )

        async_to_sync(run)()

        self.assertEqual(seen, {"10.1.2.3": "10.1.2.3", "192.0.2.1": "192.0.2.1"})

    def test_verifier_without_the_middleware_fails(self):
        verify_token = auth.WaldurTokenVerifier().verify_token

        with self.assertRaises(LookupError):
            async_to_sync(verify_token)(f"{models.PAT_PREFIX}token")
