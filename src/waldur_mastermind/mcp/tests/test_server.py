import asyncio
import json
import threading
from datetime import timedelta
from unittest import mock

import httpx
from asgiref.sync import async_to_sync
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
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
CALL_WHOAMI = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": "whoami", "arguments": {}},
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
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.request(method, path, json=body, headers=headers)


@override_config(PAT_ENABLED=True, MCP_ENABLED=True)
class MCPServerTest(test.APITestCase):
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
        self.assertEqual([tool["name"] for tool in tools], ["whoami"])

    def test_tool_acts_as_token_owner(self):
        pat = create_pat(self.user)

        response = self.request_with(pat, CALL_WHOAMI)

        result = parse_rpc(response)["result"]["structuredContent"]
        self.assertEqual(
            result, {"uuid": self.user.uuid.hex, "username": self.user.username}
        )

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
    def test_health_is_served_while_disabled(self):
        response = self.request(None, method="GET", path="/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


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


class RunOrmAsyncTest(SimpleTestCase):
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
