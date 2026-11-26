"""Every use of the client address takes it from the trusted proxy.

Behind a proxy that appends to X-Forwarded-For, the first entry is whatever
the client sent. Network restrictions and the audit trail must use the entry
the trusted proxy added, counted from the right, and an address that cannot be
determined must not lift a restriction.
"""

import pathlib
import re
from datetime import timedelta
from unittest import mock

from axes.models import AccessAttempt
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core import utils as core_utils
from waldur_core.core import views as core_views
from waldur_core.core.models import PersonalAccessToken
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.core.views import login_failures_key
from waldur_core.logging import middleware as logging_middleware
from waldur_core.logging.models import UserDataAccessLog
from waldur_core.passkeys import ceremony_views
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.models import AccessSubnet
from waldur_core.structure.tests import factories, fixtures

# The ingress, which every request behind it comes from.
INGRESS = "10.42.0.7"
ALLOWED = "203.0.113.5"
OUTSIDER = "198.51.100.9"
# What a client outside the allowed subnet sends through a proxy that appends
# the address it saw.
FORGED = f"{ALLOWED}, {OUTSIDER}"
# An ingress whose own address the allowed subnet covers, as an allowlist of a
# private range would. A request it cannot attribute must still be refused.
INGRESS_INSIDE = "203.0.113.200"


def behind_proxy(forwarded_for, remote_addr=INGRESS):
    return {"HTTP_X_FORWARDED_FOR": forwarded_for, "REMOTE_ADDR": remote_addr}


class PortalSubnetTestCase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        AccessSubnet.objects.create(
            customer=self.customer, inet="203.0.113.0/24", applies_to_portal=True
        )

    def customers(self, user, **meta):
        self.client.force_authenticate(user)
        response = self.client.get(factories.CustomerFactory.get_list_url(), **meta)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}


class AccessSubnetTest(PortalSubnetTestCase):
    def test_the_proxy_added_entry_inside_the_subnet_is_allowed(self):
        self.assertEqual(
            self.customers(
                self.fixture.owner, **behind_proxy(f"{OUTSIDER}, {ALLOWED}")
            ),
            {self.customer.uuid.hex},
        )

    def test_a_forged_first_entry_does_not_satisfy_the_subnet(self):
        self.assertEqual(
            self.customers(self.fixture.owner, **behind_proxy(FORGED)), set()
        )

    def test_an_unparseable_header_does_not_lift_the_restriction(self):
        self.assertEqual(
            self.customers(self.fixture.owner, **behind_proxy("not-an-ip")), set()
        )

    def test_an_unparseable_header_from_an_allowed_ingress_is_refused(self):
        for header in ("not-an-ip", "{oops}", " , "):
            with self.subTest(header=header):
                self.assertEqual(
                    self.customers(
                        self.fixture.owner,
                        **behind_proxy(header, remote_addr=INGRESS_INSIDE),
                    ),
                    set(),
                )

    @override_waldur_core_settings(TRUSTED_PROXY_COUNT=2)
    def test_too_few_entries_from_an_allowed_ingress_are_refused(self):
        self.assertEqual(
            self.customers(
                self.fixture.owner, **behind_proxy(ALLOWED, remote_addr=INGRESS_INSIDE)
            ),
            set(),
        )

    def test_no_header_from_an_allowed_peer_is_allowed(self):
        # Without a proxy the peer is the client.
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            factories.CustomerFactory.get_list_url(), REMOTE_ADDR=ALLOWED
        )
        self.assertEqual(
            {item["uuid"] for item in response.data}, {self.customer.uuid.hex}
        )

    def test_no_address_at_all_is_outside_every_subnet(self):
        self.assertEqual(
            self.customers(
                self.fixture.owner, **behind_proxy("not-an-ip", remote_addr="")
            ),
            set(),
        )

    def test_staff_keep_their_bypass_without_an_address(self):
        self.assertIn(
            self.customer.uuid.hex,
            self.customers(
                self.fixture.staff, **behind_proxy("not-an-ip", remote_addr="")
            ),
        )

    def test_an_unknown_address_still_sees_unrestricted_organizations(self):
        # Fail closed means "seen as an outsider", not "sees nothing".
        project = factories.ProjectFactory()
        project.customer.add_user(self.fixture.owner, CustomerRole.OWNER)
        self.assertEqual(
            self.customers(
                self.fixture.owner, **behind_proxy("not-an-ip", remote_addr="")
            ),
            {project.customer.uuid.hex},
        )

    @override_waldur_core_settings(TRUSTED_PROXY_COUNT=2)
    def test_a_load_balancer_in_front_of_the_ingress(self):
        load_balancer = "10.0.0.2"
        self.assertEqual(
            self.customers(
                self.fixture.owner,
                **behind_proxy(f"{ALLOWED}, {OUTSIDER}, {load_balancer}"),
            ),
            set(),
        )
        self.assertEqual(
            self.customers(
                self.fixture.owner,
                **behind_proxy(f"{OUTSIDER}, {ALLOWED}, {load_balancer}"),
            ),
            {self.customer.uuid.hex},
        )


class UserPermissionTest(PortalSubnetTestCase):
    def permissions(self, **meta):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get("/api/user-permissions/", **meta)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_the_proxy_added_entry_inside_the_subnet_is_allowed(self):
        self.assertEqual(
            len(self.permissions(**behind_proxy(f"{OUTSIDER}, {ALLOWED}"))), 1
        )

    def test_a_forged_first_entry_does_not_satisfy_the_subnet(self):
        self.assertEqual(self.permissions(**behind_proxy(FORGED)), [])

    def test_no_address_does_not_lift_the_restriction(self):
        self.assertEqual(
            self.permissions(**behind_proxy("not-an-ip", remote_addr="")), []
        )

    def test_an_unparseable_header_from_an_allowed_ingress_is_refused(self):
        self.assertEqual(
            self.permissions(**behind_proxy("not-an-ip", remote_addr=INGRESS_INSIDE)),
            [],
        )


@override_config(PAT_ENABLED=True)
class PersonalAccessTokenNetworkTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.user = fixtures.CustomerFixture().owner
        expires_at = timezone.now() + timedelta(days=30)
        token, prefix, token_hash = PersonalAccessToken.generate_token(expires_at)
        PersonalAccessToken.objects.create(
            user=self.user,
            name="acl",
            token_prefix=prefix,
            token_hash=token_hash,
            scopes=[PermissionEnum.LIST_ORDERS.value],
            allowed_networks=["203.0.113.0/24"],
            expires_at=expires_at,
        )
        self.token = token

    def get(self, **meta):
        return self.client.get(
            "/api/customers/", HTTP_AUTHORIZATION=f"Bearer {self.token}", **meta
        )

    def test_the_proxy_added_entry_inside_the_acl_is_allowed(self):
        response = self.get(**behind_proxy(f"{OUTSIDER}, {ALLOWED}"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_forged_first_entry_does_not_satisfy_the_acl(self):
        response = self.get(**behind_proxy(FORGED))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_an_unparseable_header_is_denied(self):
        response = self.get(**behind_proxy("not-an-ip", remote_addr=""))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_an_unparseable_header_from_an_allowed_ingress_is_denied(self):
        response = self.get(**behind_proxy("not-an-ip", remote_addr=INGRESS_INSIDE))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class AuditTrailTest(test.APITestCase):
    factory = test.APIRequestFactory()

    def tearDown(self):
        logging_middleware.reset_event_context()

    def event_address(self, **meta):
        request = self.factory.get("/", **meta)
        logging_middleware.CaptureEventContextMiddleware(
            lambda r: None
        ).process_request(request)
        return (logging_middleware.get_event_context() or {}).get("ip_address")

    def test_events_record_the_proxy_added_entry(self):
        self.assertEqual(self.event_address(**behind_proxy(FORGED)), OUTSIDER)

    def test_events_record_no_address_for_an_unparseable_header(self):
        # The ingress's own address would name the wrong party.
        self.assertIsNone(self.event_address(**behind_proxy("{oops}")))

    def test_an_unresolved_header_is_logged_once_a_minute(self):
        with mock.patch.object(core_utils, "_last_unresolved_warning", 0.0):
            with self.assertLogs("waldur_core.core.utils", "WARNING") as logs:
                for _ in range(3):
                    self.event_address(**behind_proxy("not-an-ip"))
        self.assertEqual(len(logs.records), 1)

    def test_rate_limits_count_an_unresolved_request_under_the_peer(self):
        request = self.factory.get("/", **behind_proxy("not-an-ip"))
        self.assertIsNone(core_utils.get_ip_address(request))
        self.assertEqual(core_utils.get_rate_limit_address(request), INGRESS)
        request = self.factory.get("/", **behind_proxy(FORGED))
        self.assertEqual(core_utils.get_rate_limit_address(request), OUTSIDER)

    def test_passkey_use_records_the_proxy_added_entry(self):
        request = self.factory.get("/", **behind_proxy(FORGED))
        self.assertEqual(ceremony_views._client_ip(request), OUTSIDER)

    def test_users_me_reports_the_proxy_added_entry(self):
        self.client.force_authenticate(factories.UserFactory())
        response = self.client.get(
            factories.UserFactory.get_list_url("me"), **behind_proxy(FORGED)
        )
        self.assertEqual(response.data["ip_address"], OUTSIDER)

    @override_config(USER_DATA_ACCESS_LOGGING_ENABLED=True)
    def test_data_access_log_records_the_proxy_added_entry(self):
        target = factories.UserFactory()
        self.client.force_authenticate(factories.UserFactory(is_staff=True))
        self.client.get(factories.UserFactory.get_url(target), **behind_proxy(FORGED))
        self.assertEqual(
            UserDataAccessLog.objects.filter(target_user=target).get().ip_address,
            OUTSIDER,
        )


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LoginLockoutTest(test.APITestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_failures_count_under_the_proxy_added_entry(self):
        user = factories.UserFactory()
        for forged in ("192.0.2.1", "192.0.2.2"):
            self.client.post(
                "/api-auth/password/",
                {"username": user.username, "password": "wrong"},
                **behind_proxy(f"{forged}, {OUTSIDER}"),
            )
        self.assertEqual(cache.get(login_failures_key(user.username, OUTSIDER)), 2)

    def fail_until_blocked(self, **meta):
        user = factories.UserFactory()
        with mock.patch.object(core_views, "emit_user_blocked_event") as emit:
            for _ in range(core_views.MAX_LOGIN_FAILURES):
                self.client.post(
                    "/api-auth/password/",
                    {"username": user.username, "password": "wrong"},
                    **meta,
                )
        emit.assert_called_once()
        return user, emit.call_args.args

    def test_the_blocked_event_names_the_proxy_added_entry(self):
        user, args = self.fail_until_blocked(**behind_proxy(FORGED))
        self.assertEqual(args[:2], (user.username, OUTSIDER))

    def test_the_blocked_event_names_no_address_when_unresolved(self):
        # The counter still keys on the ingress; the event must not name it.
        user, args = self.fail_until_blocked(**behind_proxy("not-an-ip"))
        self.assertEqual(args[:2], (user.username, None))
        self.assertEqual(
            cache.get(login_failures_key(user.username, INGRESS)),
            core_views.MAX_LOGIN_FAILURES,
        )

    def test_axes_records_the_proxy_added_entry(self):
        user = factories.UserFactory()
        self.client.post(
            "/api-auth/password/",
            {"username": user.username, "password": "wrong"},
            **behind_proxy(FORGED),
        )
        self.assertEqual(
            AccessAttempt.objects.get(username=user.username).ip_address, OUTSIDER
        )


class OneResolverTest(SimpleTestCase):
    """Nothing reads the client address except the resolver in core.utils."""

    # X-Forwarded-For or REMOTE_ADDR from a Django request, or a single line
    # of X-Forwarded-For from a Starlette one.
    DIRECT_READ = re.compile(
        r"""META\s*(\.get\(|\[)\s*["'](HTTP_X_FORWARDED_FOR|REMOTE_ADDR)["']"""
        r"""|headers\s*(\.get\(|\[)\s*["']x-forwarded-for["']""",
        re.IGNORECASE,
    )
    # The MCP server's adapter, which joins every header line for the resolver.
    STARLETTE_JOIN = 'headers.getlist("x-forwarded-for")'

    def sources(self):
        src = pathlib.Path(__file__).resolve().parents[3]
        for path in src.rglob("*.py"):
            if "tests" not in path.parts and "migrations" not in path.parts:
                yield path.relative_to(src), path.read_text(encoding="utf-8")

    def test_no_other_module_reads_the_address(self):
        readers = {
            path for path, text in self.sources() if self.DIRECT_READ.search(text)
        }
        self.assertEqual(readers, {pathlib.Path("waldur_core/core/utils.py")})

    def test_only_the_mcp_adapter_joins_starlette_header_lines(self):
        joiners = {path for path, text in self.sources() if self.STARLETTE_JOIN in text}
        self.assertEqual(joiners, {pathlib.Path("waldur_mastermind/mcp/auth.py")})
