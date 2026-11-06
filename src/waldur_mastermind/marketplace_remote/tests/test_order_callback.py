"""Order callback URLs: accepted but not stored on create, checked before each call."""

import logging
import socket
import uuid
from unittest import mock
from urllib.parse import urlsplit

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework import status, test
from rest_framework.throttling import ScopedRateThrottle

from waldur_core.core.metadata import WaldurCore
from waldur_core.core.tests.test_outbound_address_check import FakeServer
from waldur_core.core.utils import serialize_instance
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.server.celeryconf import PriorityRouter
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, order_callback
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    OfferingStates,
    OrderStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace_remote import tasks, views

DEFAULT_BLACKLIST = WaldurCore.__fields__["SUBNET_BLACKLIST"].default

# What a local Waldur sets when it creates an order on this one: its own
# public pull endpoint for that order (processors.build_callback_url).
PULL_PATH = "/api/remote-waldur-api/pull_order/0123456789abcdef0123456789abcdef"
PULL_URL = "https://local-waldur.example.com" + PULL_PATH
HTTP_PULL_URL = "http://local-waldur.example.com" + PULL_PATH

HOST_REFUSED = "Callback URL host is not allowed or cannot be resolved."
PATH_REFUSED = "Callback URL must be the order pull endpoint of a Waldur."

REFUSED_PATHS = (
    "/",
    "/api/marketplace-orders/",
    "/api/remote-waldur-api/pull_order/abc",
    "/api/remote-waldur-api/pull_order/" + "A" * 32,
    PULL_PATH + "/",
    PULL_PATH + "/extra",
    "/api/remote-waldur-api/pull_order/%30" + "0" * 31,
    # A prefix a server would resolve into another path.
    "/../admin" + PULL_PATH,
    "/x/.." + PULL_PATH,
    "/%2e%2e" + PULL_PATH,
    # Empty segments beyond the one a trailing-slash MASTERMIND_URL leaves.
    "//127.0.0.1" + PULL_PATH,
    "///" + PULL_PATH.lstrip("/"),
    "/waldur///" + PULL_PATH.lstrip("/"),
)

ACCEPTED_URLS = (
    PULL_URL,
    "http://local-waldur.example.com:8000" + PULL_PATH,
    # MASTERMIND_URL with a path prefix, or a trailing slash.
    "https://local-waldur.example.com/waldur" + PULL_PATH,
    "https://local-waldur.example.com/" + PULL_PATH,
    "https://local-waldur.example.com/waldur/" + PULL_PATH,
)

HOSTS = {
    "local-waldur.example.com": ["8.8.8.8"],
    "metadata.example.com": ["169.254.169.254"],
    "loopback.example.com": ["127.0.0.1"],
    "mapped.example.com": ["::ffff:10.0.0.1"],
    "nat64.example.com": ["64:ff9b::a9fe:a9fe"],
    "mixed.example.com": ["8.8.8.8", "10.0.0.1"],
}


def dns(mapping):
    """Answer lookups from ``mapping``; a host given as a list of lists gets
    its answers in turn. IP literals resolve as usual, anything else fails."""
    real_getaddrinfo = socket.getaddrinfo
    calls = []

    def getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        if host not in mapping:
            return real_getaddrinfo(
                host, port, *args, **{**kwargs, "flags": socket.AI_NUMERICHOST}
            )
        answer = mapping[host]
        if answer and isinstance(answer[0], list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port or 0),
            )
            for address in answer
        ]

    patcher = mock.patch("socket.getaddrinfo", side_effect=getaddrinfo)
    patcher.calls = calls
    return patcher


class CallbackUrlOnCreateTest(test.APITestCase):
    """Order create checks the URL's shape and path, resolves nothing and
    stores nothing: order callbacks are off."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_ORDER)
        self.offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.client.force_authenticate(self.fixture.admin)

    def create_order(self, callback_url):
        payload = {
            "project": structure_factories.ProjectFactory.get_url(self.fixture.project),
            "offering": factories.OfferingFactory.get_public_url(self.offering),
            "plan": factories.PlanFactory.get_public_url(self.plan),
            "attributes": {},
            "callback_url": callback_url,
        }
        resolver = dns(dict(HOSTS))
        with resolver:
            response = self.client.post(
                factories.OrderFactory.get_list_url(), payload, format="json"
            )
        if callback_url:
            # Nothing on this path may resolve the caller's host.
            host = urlsplit(callback_url).hostname
            self.assertNotIn(host, resolver.calls)
        return response

    def assert_refused(self, url, reason=None):
        response = self.create_order(url)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("callback_url", response.data)
        if reason:
            self.assertIn(reason, str(response.data["callback_url"]))
        self.assertFalse(models.Order.objects.filter(callback_url=url).exists())
        return response

    def assert_accepted_not_stored(self, url):
        response = self.create_order(url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotIn("callback_url", response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(order.callback_url)

    def test_other_schemes_are_refused(self):
        # ftp passes the model's URL validator; the others never get this far.
        self.assert_refused(
            "ftp://local-waldur.example.com" + PULL_PATH,
            reason="Only http and https URLs are allowed",
        )
        for url in (
            "file://" + PULL_PATH,
            "gopher://local-waldur.example.com" + PULL_PATH,
        ):
            with self.subTest(url=url):
                self.assert_refused(url)

    def test_credentials_query_and_fragment_are_refused(self):
        for url, reason in (
            (
                "https://user:secret@local-waldur.example.com" + PULL_PATH,
                "must not contain credentials",
            ),
            (
                "https://local-waldur.example.com@127.0.0.1" + PULL_PATH,
                "must not contain credentials",
            ),
            (PULL_URL + "?next=http://127.0.0.1/", "must not contain a query"),
            (PULL_URL + "#fragment", "must not contain a query or a fragment"),
        ):
            with self.subTest(url=url):
                self.assert_refused(url, reason=reason)

    def test_urls_other_than_the_pull_endpoint_are_refused(self):
        for path in REFUSED_PATHS:
            url = "https://local-waldur.example.com" + path
            with self.subTest(url=url):
                self.assert_refused(url, reason=PATH_REFUSED)

    def test_pull_url_of_a_local_waldur_is_accepted_and_not_stored(self):
        for url in ACCEPTED_URLS:
            with self.subTest(url=url):
                self.assert_accepted_not_stored(url)

    def test_host_is_not_looked_up_on_create(self):
        # The address check runs only when a stored URL is called: field
        # validation runs before the order's permission checks.
        for url in (
            PULL_URL,
            "http://metadata.example.com" + PULL_PATH,
            "http://unknown.example.com" + PULL_PATH,
        ):
            with self.subTest(url=url):
                self.assert_accepted_not_stored(url)

    def test_valid_callback_url_enqueues_no_callback(self):
        with mock.patch.object(tasks.trigger_order_callback, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.create_order(PULL_URL)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            order = models.Order.objects.get(uuid=response.data["uuid"])
            # A state change would trigger the callback of a stored URL.
            with self.captureOnCommitCallbacks(execute=True):
                order.set_state_erred()
                order.save()
        delay.assert_not_called()

    def test_order_without_callback_url_is_accepted(self):
        for value in (None, ""):
            with self.subTest(value=value):
                response = self.create_order(value)
                self.assertEqual(
                    response.status_code, status.HTTP_201_CREATED, response.data
                )

    def test_callback_url_is_not_shown(self):
        order = factories.OrderFactory(
            project=self.fixture.project, callback_url=PULL_URL
        )
        response = self.client.get(factories.OrderFactory.get_url(order))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("callback_url", response.data)


@override_settings(
    WALDUR_CORE={**settings.WALDUR_CORE, "SUBNET_BLACKLIST": DEFAULT_BLACKLIST}
)
class CheckCallbackUrlTest(SimpleTestCase):
    """The full check, run before each call."""

    def check(self, url):
        with dns(dict(HOSTS)):
            return order_callback.check_callback_url(url)

    def test_internal_addresses_are_refused(self):
        for host in (
            "127.0.0.1",
            "169.254.169.254",
            "10.0.0.1:8080",
            "[::1]",
            "[::ffff:127.0.0.1]",
            "[64:ff9b::a9fe:a9fe]",
            "[2002:7f00:1::]",
            "metadata.example.com",
            "loopback.example.com:8000",
            "mapped.example.com",
            "nat64.example.com",
            "mixed.example.com",
        ):
            for scheme in ("http", "https"):
                url = f"{scheme}://{host}{PULL_PATH}"
                with self.subTest(url=url):
                    with self.assertRaisesMessage(
                        order_callback.CallbackUrlRefused, HOST_REFUSED
                    ):
                        self.check(url)

    def test_refused_and_unresolvable_hosts_get_the_same_answer(self):
        # Otherwise the answer would tell which internal names exist.
        for host in ("metadata.example.com", "unknown.example.com"):
            with self.subTest(host=host):
                with self.assertRaisesMessage(
                    order_callback.CallbackUrlRefused, HOST_REFUSED
                ):
                    self.check(f"http://{host}{PULL_PATH}")

    def test_paths_are_checked_before_the_lookup(self):
        for path in REFUSED_PATHS:
            url = "https://local-waldur.example.com" + path
            with self.subTest(url=url):
                resolver = dns(dict(HOSTS))
                with resolver:
                    with self.assertRaisesMessage(
                        order_callback.CallbackUrlRefused, PATH_REFUSED
                    ):
                        order_callback.check_callback_url(url)
                self.assertEqual(resolver.calls, [])

    def test_pull_url_on_a_public_host_passes(self):
        for url in ACCEPTED_URLS:
            with self.subTest(url=url):
                self.assertEqual(
                    self.check(url), ("local-waldur.example.com", ["8.8.8.8"])
                )


class SocketGuard:
    """Records, and refuses, any connection made around FakeServer."""

    def __init__(self):
        self.addresses = []

    def connect(self, sock, address):
        self.addresses.append(address)
        raise ConnectionRefusedError("test: no connections")

    def patch(self):
        guard = self

        def connect(sock, address):
            return guard.connect(sock, address)

        return mock.patch.object(socket.socket, "connect", connect)


@override_settings(
    WALDUR_CORE={**settings.WALDUR_CORE, "SUBNET_BLACKLIST": DEFAULT_BLACKLIST}
)
class CallbackSendTest(test.APITestCase):
    def setUp(self):
        # Plain http: FakeServer does not speak TLS.
        self.order = factories.OrderFactory(callback_url=HTTP_PULL_URL)
        self.server = FakeServer()
        self.guard = SocketGuard()

    def tearDown(self):
        self.server.close()
        super().tearDown()

    def send(self, hosts):
        resolver = dns(hosts)
        with resolver, self.server.patch(), self.guard.patch():
            with self.assertLogs(tasks.logger, level=logging.DEBUG) as logs:
                tasks.trigger_order_callback(serialize_instance(self.order))
        return resolver, logs

    def test_callback_is_posted_to_the_checked_address(self):
        resolver, logs = self.send({"local-waldur.example.com": [["8.8.8.8"]]})
        self.assertEqual(self.server.addresses, [("8.8.8.8", 80)])
        self.assertEqual(resolver.calls, ["local-waldur.example.com"])
        self.assertEqual(self.guard.addresses, [])
        self.assertTrue(self.server.received.startswith(b"POST "))
        self.assertIn("answered with status 200", "\n".join(logs.output))

    def test_callback_is_not_sent_when_dns_now_answers_internal(self):
        _, logs = self.send({"local-waldur.example.com": ["169.254.169.254"]})
        self.assertEqual(self.server.addresses, [])
        self.assertEqual(self.guard.addresses, [])
        self.assertIn("refused", "\n".join(logs.output))

    def test_refused_callback_is_logged_as_a_warning_at_most(self):
        # Whoever stored the URL can make this happen: not an error of ours,
        # and nothing for Sentry, which records ERROR and above.
        for hosts in (
            {"local-waldur.example.com": ["169.254.169.254"]},
            {"local-waldur.example.com": []},
        ):
            with self.subTest(hosts=hosts):
                with dns(hosts), self.server.patch(), self.guard.patch():
                    with self.assertLogs(level=logging.DEBUG) as logs:
                        tasks.trigger_order_callback(serialize_instance(self.order))
                levels = {record.levelno for record in logs.records}
                self.assertIn(logging.WARNING, levels)
                self.assertLess(max(levels), logging.ERROR)
                self.assertEqual(self.server.addresses, [])

    def test_rebinding_after_the_check_does_not_reach_the_new_address(self):
        resolver, _ = self.send(
            {"local-waldur.example.com": [["8.8.8.8"], ["127.0.0.1"]]}
        )
        self.assertEqual(self.server.addresses, [("8.8.8.8", 80)])
        self.assertEqual(resolver.calls, ["local-waldur.example.com"])
        self.assertEqual(self.guard.addresses, [])

    def test_stored_url_of_bad_shape_is_not_called(self):
        # Set before the write check existed, or by import_structure.
        for url in (
            "ftp://local-waldur.example.com" + PULL_PATH,
            "http://user@local-waldur.example.com" + PULL_PATH,
            "http://127.0.0.1" + PULL_PATH,
            "http://local-waldur.example.com/admin/",
        ):
            with self.subTest(url=url):
                models.Order.objects.filter(pk=self.order.pk).update(callback_url=url)
                self.send({"local-waldur.example.com": ["8.8.8.8"]})
                self.assertEqual(self.server.addresses, [])
                self.assertEqual(self.guard.addresses, [])

    def http_callback(self, response):
        self.server.close()
        self.server = FakeServer(response)
        return self.send(
            {"local-waldur.example.com": ["8.8.8.8"], "other.example.com": ["8.8.4.4"]}
        )

    def test_redirect_is_not_followed(self):
        # To a public host, so only not following it keeps the count at one.
        self.http_callback(
            b"HTTP/1.1 302 Found\r\nLocation: http://other.example.com/x\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        self.assertEqual(self.server.addresses, [("8.8.8.8", 80)])
        self.assertEqual(self.guard.addresses, [])

    def test_request_is_a_bodiless_post_to_the_url(self):
        self.http_callback(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        request = self.server.received.decode()
        self.assertTrue(
            request.startswith(
                "POST /api/remote-waldur-api/pull_order/"
                "0123456789abcdef0123456789abcdef HTTP/1.1\r\n"
            ),
            request,
        )
        self.assertIn("\r\nHost: local-waldur.example.com\r\n", request)

    def test_response_body_is_not_read_or_echoed(self):
        secret = b"internal-secret-" + b"x" * 64
        _, logs = self.http_callback(
            b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: "
            + str(len(secret)).encode()
            + b"\r\n\r\n"
            + secret
        )
        self.assertNotIn("internal-secret", "\n".join(logs.output))
        self.order.refresh_from_db()
        self.assertEqual(self.order.error_message, "")
        self.assertNotIn("internal-secret", self.order.output or "")

    def test_only_the_status_is_read(self):
        # Reading the body would fail: far less comes than announced.
        _, logs = self.http_callback(
            b"HTTP/1.1 200 OK\r\nContent-Length: 10000000\r\n\r\npartial"
        )
        output = "\n".join(logs.output)
        self.assertIn("answered with status 200", output)
        self.assertNotIn("failed", output)

    def test_failures_are_logged_not_raised(self):
        _, logs = self.http_callback(b"not HTTP\r\n\r\n")
        self.assertIn("failed", "\n".join(logs.output))

    def test_deleted_order_is_skipped(self):
        serialized = serialize_instance(self.order)
        self.order.delete()
        with self.server.patch(), self.guard.patch():
            tasks.trigger_order_callback(serialized)
        self.assertEqual(self.server.addresses, [])

    def test_timeouts_are_short_and_redirects_off(self):
        with dns({"local-waldur.example.com": ["8.8.8.8"]}):
            with mock.patch.object(tasks.httpx, "Client") as client_class:
                client = client_class.return_value.__enter__.return_value
                client.stream.return_value.__enter__.return_value.status_code = 200
                tasks.trigger_order_callback(serialize_instance(self.order))
        kwargs = client_class.call_args.kwargs
        timeout = kwargs["timeout"]
        self.assertLessEqual(timeout.connect, tasks.CALLBACK_CONNECT_TIMEOUT)
        self.assertLessEqual(timeout.read, tasks.CALLBACK_TIMEOUT)
        self.assertLessEqual(timeout.write, tasks.CALLBACK_TIMEOUT)
        self.assertLessEqual(tasks.CALLBACK_TIMEOUT, 10)
        self.assertFalse(kwargs["follow_redirects"])

    def test_task_has_a_short_overall_time_limit_off_the_default_queue(self):
        task = tasks.trigger_order_callback
        self.assertIsNotNone(task.soft_time_limit)
        self.assertLessEqual(task.soft_time_limit, 10)
        self.assertGreater(task.time_limit, task.soft_time_limit)
        self.assertLessEqual(task.time_limit, 15)
        self.assertEqual(
            PriorityRouter().route_for_task(task.name),
            {"queue": "background-durable"},
        )

    def test_time_limit_during_the_lookup_is_logged_not_raised(self):
        with mock.patch.object(
            order_callback, "check_callback_url", side_effect=SoftTimeLimitExceeded
        ):
            with self.assertLogs(tasks.logger, level=logging.WARNING) as logs:
                tasks.trigger_order_callback(serialize_instance(self.order))
        self.assertIn("failed", "\n".join(logs.output))


class PullOrderViewInputTest(test.APITestCase):
    def setUp(self):
        self.order = factories.OrderFactory(
            offering=factories.OfferingFactory(type=REMOTE_OFFERING),
            state=OrderStates.EXECUTING,
        )

    def test_malformed_uuid_is_a_client_error(self):
        # The route accepts any run of hex digits.
        for value in ("abc", "0" * 31, "f" * 40):
            with self.subTest(value=value):
                response = self.client.post(
                    f"/api/remote-waldur-api/pull_order/{value}"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_order_is_not_found(self):
        response = self.client.post(
            f"/api/remote-waldur-api/pull_order/{uuid.uuid4().hex}"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PullOrderViewThrottleTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.order = factories.OrderFactory(
            offering=factories.OfferingFactory(type=REMOTE_OFFERING),
            state=OrderStates.EXECUTING,
        )
        self.url = f"/api/remote-waldur-api/pull_order/{self.order.uuid.hex}"

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_view_has_a_configured_throttle_scope(self):
        scope = views.PullOrderView.throttle_scope
        self.assertTrue(scope)
        self.assertIn(scope, settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])

    @mock.patch("waldur_mastermind.marketplace_remote.tasks.OrderPullTask")
    def test_anonymous_caller_is_throttled_but_not_refused(self, _):
        scope = views.PullOrderView.throttle_scope

        class Throttle(ScopedRateThrottle):
            THROTTLE_RATES = {scope: "2/min"}

        with mock.patch.object(views.PullOrderView, "throttle_classes", [Throttle]):
            codes = [self.client.post(self.url).status_code for _ in range(3)]
        self.assertEqual(
            codes,
            [
                status.HTTP_200_OK,
                status.HTTP_200_OK,
                status.HTTP_429_TOO_MANY_REQUESTS,
            ],
        )
