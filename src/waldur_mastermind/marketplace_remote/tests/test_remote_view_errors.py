"""The remote Waldur views do not hand the remote's answer back to the caller,
and what a remote can make them read is bounded."""

import gzip
import logging
import socket
import time
from unittest import mock

import httpx
import respx
from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.client import (
    REFUSED_HOST_MESSAGE,
    REMOTE_VIEW_LIMITS,
    RemoteCallLimits,
)
from waldur_core.core.pinned_transport import (
    EncodedResponseRefused,
    InvalidRemoteAnswer,
    RemoteCallTimeout,
    ResponseTooLarge,
    TooManyRequests,
)
from waldur_core.core.tests.helpers import create_pat, override_waldur_core_settings
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, GlobalRole
from waldur_core.permissions.models import RoleManager
from waldur_core.permissions.utils import grant_global_role
from waldur_core.structure.models import Customer
from waldur_core.structure.tests.factories import CustomerFactory, UserFactory
from waldur_mastermind.marketplace_remote.tests.dns_utils import (
    create_selective_dns_mock,
)

API_URL = "https://remote-waldur.com"
SECRET_BODY = "internal-service-says-hello"

REMOTE_ENDPOINTS = (
    ("/api/remote-waldur-api/remote_customers/", f"{API_URL}/api/customers/"),
    (
        "/api/remote-waldur-api/remote_categories/",
        f"{API_URL}/api/marketplace-categories/",
    ),
    (
        "/api/remote-waldur-api/shared_offerings/?customer_uuid=" + "a" * 32,
        f"{API_URL}/api/marketplace-public-offerings/",
    ),
)


def offering_creator():
    """A user who holds OFFERING.CREATE on one organization."""
    CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
    user = UserFactory()
    CustomerFactory().add_user(user, CustomerRole.OWNER)
    return user


def next_page_forever(request):
    """A hostile remote: every page links to another, up to a point that
    keeps a test of unbounded code from running forever."""
    page = int(request.url.params.get("page", "1"))
    headers = {}
    if page < 200:
        next_url = request.url.copy_merge_params({"page": page + 1})
        headers["Link"] = f'<{next_url}>; rel="next"'
    return httpx.Response(200, json=[], headers=headers)


class RemoteViewErrorTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()
        self.client.force_login(offering_creator())

    def tearDown(self):
        respx.stop()
        self.dns_patcher.stop()
        super().tearDown()

    def post(self, url, api_url=f"{API_URL}/api/"):
        return self.client.post(url, {"api_url": api_url, "token": "valid_token"})

    def test_non_200_answer_is_not_echoed(self):
        for view_url, remote_url in REMOTE_ENDPOINTS:
            with self.subTest(view_url=view_url):
                respx.get(url__startswith=remote_url).respond(500, text=SECRET_BODY)
                response = self.post(view_url)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(
                    response.data, "The remote Waldur answered with status 500."
                )
                content = response.content.decode()
                self.assertNotIn(SECRET_BODY, content)
                self.assertNotIn("remote-waldur.com", content)

    def test_transport_error_is_not_echoed(self):
        for view_url, remote_url in REMOTE_ENDPOINTS:
            with self.subTest(view_url=view_url):
                respx.get(url__startswith=remote_url).mock(
                    side_effect=httpx.ConnectError(f"refused by {SECRET_BODY}")
                )
                response = self.post(view_url)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(
                    response.data, "The remote Waldur could not be reached."
                )
                self.assertNotIn(SECRET_BODY, response.content.decode())

    def test_internal_address_is_refused_before_any_call(self):
        # IP literals need no DNS; the mock would answer 8.8.8.8 for them.
        self.dns_patcher.stop()
        self.dns_patcher = mock.patch("socket.getaddrinfo", wraps=socket.getaddrinfo)
        self.dns_patcher.start()
        route = respx.route()
        for api_url in (
            "http://[::ffff:127.0.0.1]:8000/any/path",
            "http://[::ffff:169.254.169.254]/latest/meta-data/",
            "http://0.0.0.0:8000/",
            "http://100.64.0.1/",
        ):
            with self.subTest(api_url=api_url):
                response = self.post(
                    "/api/remote-waldur-api/remote_customers/", api_url=api_url
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data["api_url"], [REFUSED_HOST_MESSAGE])
        self.assertFalse(route.called)

    def test_url_with_query_is_refused(self):
        route = respx.route()
        response = self.post(
            "/api/remote-waldur-api/remote_customers/",
            api_url=f"{API_URL}/api/customers/?x=",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("api_url", response.data)
        self.assertFalse(route.called)

    def test_hostile_pagination_is_cut_at_the_page_cap(self):
        for view_url, remote_url in REMOTE_ENDPOINTS:
            with self.subTest(view_url=view_url):
                route = respx.get(url__startswith=remote_url).mock(
                    side_effect=next_page_forever
                )
                response = self.post(view_url)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data, TooManyRequests.user_message)
                self.assertEqual(route.call_count, REMOTE_VIEW_LIMITS.max_requests)

    def test_slow_hostile_pagination_is_cut_at_the_deadline(self):
        def slow_page(request):
            time.sleep(0.05)
            return next_page_forever(request)

        limits = RemoteCallLimits(max_requests=1000, timeout=0.3, max_bytes=10**6)
        with mock.patch(
            "waldur_mastermind.marketplace_remote.views.REMOTE_VIEW_LIMITS", limits
        ):
            for view_url, remote_url in REMOTE_ENDPOINTS:
                with self.subTest(view_url=view_url):
                    route = respx.get(url__startswith=remote_url).mock(
                        side_effect=slow_page
                    )
                    response = self.post(view_url)
                    self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertEqual(response.data, RemoteCallTimeout.user_message)
                    self.assertLess(route.call_count, 20)

    def test_encoded_answer_is_refused(self):
        payload = gzip.compress(gzip.compress(b"[" + b" " * 1_000_000 + b"]"))
        for view_url, remote_url in REMOTE_ENDPOINTS:
            with self.subTest(view_url=view_url):
                route = respx.get(url__startswith=remote_url).respond(
                    200,
                    content=payload,
                    headers={
                        "Content-Encoding": "gzip, gzip",
                        "Content-Type": "application/json",
                    },
                )
                response = self.post(view_url)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data, EncodedResponseRefused.user_message)
                self.assertEqual(
                    route.calls.last.request.headers["Accept-Encoding"], "identity"
                )

    def test_oversized_answer_is_refused(self):
        with mock.patch("waldur_core.core.client.MAX_RESPONSE_BYTES", 1024):
            for view_url, remote_url in REMOTE_ENDPOINTS:
                with self.subTest(view_url=view_url):
                    respx.get(url__startswith=remote_url).respond(
                        200, json=[{"name": "x" * 2048}]
                    )
                    response = self.post(view_url)
                    self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertEqual(response.data, ResponseTooLarge.user_message)


@override_waldur_core_settings(SUBNET_BLACKLIST=["10.0.0.0/8", "127.0.0.0/8"])
class RefusedUrlAnswerTest(test.APITestCase):
    """The views do not tell which internal names resolve."""

    def setUp(self):
        super().setUp()
        original = socket.getaddrinfo

        def getaddrinfo(host, port, *args, **kwargs):
            if host == "gone.example.com":
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            if host == "intranet.example.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))]
            return original(host, port, *args, **kwargs)

        self.dns_patcher = mock.patch("socket.getaddrinfo", side_effect=getaddrinfo)
        self.dns_patcher.start()
        self.client.force_login(offering_creator())

    def tearDown(self):
        self.dns_patcher.stop()
        super().tearDown()

    def post(self, view_url, host):
        return self.client.post(
            view_url, {"api_url": f"https://{host}/api/", "token": "token"}
        )

    def test_unresolvable_and_internal_hosts_get_the_same_answer(self):
        for view_url, _ in REMOTE_ENDPOINTS:
            with self.subTest(view_url=view_url):
                with self.assertLogs("waldur_core.core.client", "DEBUG") as logs:
                    unresolvable = self.post(view_url, "gone.example.com")
                    internal = self.post(view_url, "intranet.example.com")
                self.assertEqual(unresolvable.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(internal.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(unresolvable.content, internal.content)
                self.assertEqual(unresolvable.data, {"api_url": [REFUSED_HOST_MESSAGE]})
                # Only the log tells them apart, and not as an error.
                output = "\n".join(logs.output)
                self.assertIn("does not resolve", output)
                self.assertIn("10.0.0.7 is an internal address", output)
                self.assertTrue(
                    all(record.levelno < logging.ERROR for record in logs.records)
                )


class RemoteViewAccessTest(test.APITestCase):
    """Who may make Waldur call a URL of their choosing."""

    def setUp(self):
        super().setUp()
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()

    def tearDown(self):
        respx.stop()
        self.dns_patcher.stop()
        super().tearDown()

    def assert_access(self, user, allowed):
        self.client.force_authenticate(user)
        for view_url, remote_url in REMOTE_ENDPOINTS:
            with self.subTest(view_url=view_url):
                route = respx.get(url__startswith=remote_url).respond(200, json=[])
                response = self.client.post(
                    view_url, {"api_url": f"{API_URL}/api/", "token": "token"}
                )
                if allowed:
                    self.assertEqual(response.status_code, status.HTTP_200_OK)
                    self.assertTrue(route.called)
                else:
                    self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                    self.assertFalse(route.called)

    def test_user_without_a_role_is_refused(self):
        self.assert_access(UserFactory(), allowed=False)

    def test_role_without_offering_create_is_refused(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_OFFERING)
        user = UserFactory()
        CustomerFactory().add_user(user, CustomerRole.OWNER)
        self.assert_access(user, allowed=False)

    def test_offering_creator_is_allowed(self):
        self.assert_access(offering_creator(), allowed=True)

    def test_holder_of_a_site_wide_grant_is_allowed(self):
        CustomerFactory()
        user = UserFactory()
        role = GlobalRole.get("GLOBAL.SUPPORT")
        role.add_permission(PermissionEnum.CREATE_OFFERING)
        grant_global_role(user, role)
        self.assert_access(user, allowed=True)

    def test_staff_is_allowed(self):
        self.assert_access(UserFactory(is_staff=True), allowed=True)

    def test_inactive_offering_creator_is_refused(self):
        user = offering_creator()
        user.is_active = False
        user.save()
        self.assert_access(user, allowed=False)


def customer_binding(customer):
    return {
        "content_type_id": ContentType.objects.get_for_model(Customer).id,
        "object_id": customer.id,
    }


@override_config(PAT_ENABLED=True)
class RemoteViewTokenAccessTest(test.APITestCase):
    """A personal access token reaches the remote views as it would reach
    the import itself."""

    def setUp(self):
        super().setUp()
        RoleManager.clear_cache()
        self.addCleanup(RoleManager.clear_cache)
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()
        respx.get(url__startswith=f"{API_URL}/api/").respond(200, json=[])

    def tearDown(self):
        respx.stop()
        self.dns_patcher.stop()
        super().tearDown()

    def token(self, user, scopes, bindings=None):
        user.can_use_personal_access_tokens = True
        user.save()
        _, token = create_pat(user, scopes=scopes, allowed_scopes=bindings or [])
        return token

    def status_with(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return self.client.post(
            "/api/remote-waldur-api/remote_categories/",
            {"api_url": f"{API_URL}/api/", "token": "t"},
        ).status_code

    def owner(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        user = UserFactory()
        customer = CustomerFactory()
        customer.add_user(user, CustomerRole.OWNER)
        return user, customer

    def test_owner_token_without_the_scope_is_refused(self):
        user, _ = self.owner()
        token = self.token(user, ["CUSTOMER.LIST"])
        self.assertEqual(self.status_with(token), status.HTTP_403_FORBIDDEN)

    def test_owner_token_with_the_scope_is_allowed(self):
        user, _ = self.owner()
        token = self.token(user, [PermissionEnum.CREATE_OFFERING.value])
        self.assertEqual(self.status_with(token), status.HTTP_200_OK)

    def test_owner_token_bound_to_the_organization_is_allowed(self):
        user, customer = self.owner()
        token = self.token(
            user, [PermissionEnum.CREATE_OFFERING.value], [customer_binding(customer)]
        )
        self.assertEqual(self.status_with(token), status.HTTP_200_OK)

    def test_owner_token_bound_to_another_organization_is_refused(self):
        user, _ = self.owner()
        token = self.token(
            user,
            [PermissionEnum.CREATE_OFFERING.value],
            [customer_binding(CustomerFactory())],
        )
        self.assertEqual(self.status_with(token), status.HTTP_403_FORBIDDEN)

    def test_staff_token_without_staff_or_offering_scope_is_refused(self):
        token = self.token(UserFactory(is_staff=True), ["CUSTOMER.LIST"])
        self.assertEqual(self.status_with(token), status.HTTP_403_FORBIDDEN)

    def test_staff_token_without_staff_scope_counts_as_its_user_only(self):
        # Without STAFF.ACCESS the token does not act as staff (#987), so it
        # gets what has_permission gives the user on the bound organization:
        # nothing, as the user holds no role there.
        customer = CustomerFactory()
        user = UserFactory(is_staff=True)
        token = self.token(
            user, [PermissionEnum.CREATE_OFFERING.value], [customer_binding(customer)]
        )
        self.assertEqual(self.status_with(token), status.HTTP_403_FORBIDDEN)
        # With a role there that grants the permission, it is allowed.
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        customer.add_user(user, CustomerRole.OWNER)
        self.assertEqual(self.status_with(token), status.HTTP_200_OK)


class RemoteViewUnreadableAnswerTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()
        self.client.force_login(offering_creator())

    def tearDown(self):
        respx.stop()
        self.dns_patcher.stop()
        super().tearDown()

    def test_unreadable_answers_are_a_bad_request(self):
        for name, body in (
            ("invalid", b"[{"),
            ("deep", b"[" * 100_000 + b"]" * 100_000),
            ("long integer", b"[" + b"9" * 5000 + b"]"),
            ("wrong shape", b'{"name": "not a list"}'),
        ):
            for view_url, remote_url in REMOTE_ENDPOINTS:
                with self.subTest(name=name, view_url=view_url):
                    respx.get(url__startswith=remote_url).respond(200, content=body)
                    with self.assertNoLogs(level=logging.ERROR):
                        response = self.client.post(
                            view_url, {"api_url": f"{API_URL}/api/", "token": "t"}
                        )
                    self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertEqual(response.data, InvalidRemoteAnswer.user_message)
