import gzip
import json
import logging
import socket
import time
import tracemalloc
import zlib
from unittest import mock

import httpx
import respx
from django.test import SimpleTestCase, override_settings
from rest_framework import test
from waldur_api_client.api.marketplace_categories import marketplace_categories_list
from waldur_api_client.models.marketplace_category_field_enum import (
    MarketplaceCategoryFieldEnum,
)

from waldur_core.core.client import (
    REFUSED_HOST_MESSAGE,
    ClientValidationError,
    RemoteCallLimits,
    check_url,
    fetch_url,
    get_waldur_client,
    remote_call_limits,
    sync_all_pages,
)
from waldur_core.core.pinned_transport import (
    EncodedResponseRefused,
    InvalidRemoteAnswer,
    RemoteCallTimeout,
    ResponseTooLarge,
    TooManyRequests,
)
from waldur_core.core.tests.dns_utils import (
    create_ssrf_dns_mock_with_error,
    create_ssrf_selective_dns_mock,
)

CATEGORY_UUID = "123e4567-e89b-12d3-a456-426614174000"


@override_settings(
    WALDUR_CORE={
        "SUBNET_BLACKLIST": [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "127.0.0.0/8",
        ]
    }
)
class SSRFProtectionTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        respx.start()

    def tearDown(self):
        respx.stop()
        super().tearDown()

    def test_allows_public_url(self):
        """
        This test should pass because the IP is not in the blocked range.
        """
        with create_ssrf_selective_dns_mock({"example.com": "8.8.8.8"}):
            check_url("https://example.com/api/endpoint")
            # No exception should be raised

    def test_blocks_localhost(self):
        """
        This test should fail because the IP is in the blocked range.
        """
        with create_ssrf_selective_dns_mock({"localhost": "127.0.0.1"}):
            with self.assertRaises(ClientValidationError):
                check_url("https://localhost/api/endpoint")

    def test_blocks_internal_ip(self):
        """
        This test should fail because the IP is in the blocked range.
        """
        with create_ssrf_selective_dns_mock({"internal.example.com": "192.168.1.1"}):
            with self.assertRaises(ClientValidationError) as exc_info:
                check_url("https://internal.example.com/api/endpoint")
            self.assertIn(REFUSED_HOST_MESSAGE, exc_info.exception.args[0])

    def test_blocks_kubernetes_ip(self):
        with create_ssrf_selective_dns_mock({"k8s.example.com": "10.43.0.1"}):
            with self.assertRaises(ClientValidationError) as exc_info:
                check_url("https://k8s.example.com/api/endpoint")
                print("After check_url")
            self.assertIn(REFUSED_HOST_MESSAGE, exc_info.exception.args[0])

    def test_blocks_dns_resolution_failure(self):
        with create_ssrf_dns_mock_with_error():
            with self.assertRaises(ClientValidationError) as exc_info:
                check_url("https://invalid.example.com/api/endpoint")
            self.assertIn(REFUSED_HOST_MESSAGE, exc_info.exception.args[0])

    def test_wrong_url_format_raises_error(self):
        """
        This test should fail because the URL is not valid.
        """
        with self.assertRaises(ClientValidationError) as exc_info:
            check_url("/api/endpoint")
        self.assertEqual("URL is not valid", exc_info.exception.args[0])

    def test_handles_requests_without_block(self):
        """
        Test that SSRF protection works for redirects:
        1. Initial request to public URL is allowed
        2. Redirect to public IP is allowed
        """
        with create_ssrf_selective_dns_mock({"example.com": "8.8.8.8"}):
            client = get_waldur_client("https://example.com", "test-token")
            respx.get(
                "https://example.com/api/marketplace-categories/?field=uuid&field=title"
            ).respond(
                json=[
                    {
                        "url": "https://public.example.com/api/marketplace-categories/123e4567-e89b-12d3-a456-426614174000",
                        "uuid": "123e4567-e89b-12d3-a456-426614174000",
                        "title": "Category 1",
                    }
                ]
            )

            remote_categories = marketplace_categories_list.sync(
                client=client,
                field=[
                    MarketplaceCategoryFieldEnum.UUID,
                    MarketplaceCategoryFieldEnum.TITLE,
                ],
            )
            self.assertEqual(len(remote_categories), 1)
            self.assertEqual(
                str(remote_categories[0].uuid), "123e4567-e89b-12d3-a456-426614174000"
            )
            self.assertEqual(remote_categories[0].title, "Category 1")

    def test_handles_requests_with_block(self):
        """
        This test should fail on client initialization because the IP is in the blocked range.
        """
        with create_ssrf_selective_dns_mock({"example.com": "192.168.1.1"}):
            with self.assertRaises(ClientValidationError) as exc_info:
                get_waldur_client("https://example.com", "test-token")
            self.assertIn(REFUSED_HOST_MESSAGE, exc_info.exception.args[0])


API = "https://remote.example.com"
CATEGORIES = f"{API}/api/marketplace-categories/"


def next_page_forever(request):
    """A hostile remote: every page links to another, up to a point that
    keeps a test of unbounded code from running forever."""
    page = int(request.url.params.get("page", "1"))
    headers = {}
    if page < 200:
        headers["Link"] = f'<{CATEGORIES}?page={page + 1}>; rel="next"'
    return httpx.Response(200, json=[], headers=headers)


@override_settings(WALDUR_CORE={"SUBNET_BLACKLIST": ["10.0.0.0/8", "127.0.0.0/8"]})
class RemoteCallBoundsTest(SimpleTestCase):
    """What one remote Waldur can make an SDK client read is bounded."""

    def setUp(self):
        self.dns = create_ssrf_selective_dns_mock({"remote.example.com": "8.8.8.8"})
        self.dns.start()
        self.respx = respx.mock(assert_all_called=False)
        self.respx.start()
        self.client = get_waldur_client(API, "token")

    def tearDown(self):
        self.respx.stop()
        self.dns.stop()

    def test_plain_json_pages_are_read_and_asked_for_unencoded(self):
        route = self.respx.get(url__startswith=CATEGORIES).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=[{"uuid": CATEGORY_UUID, "title": "One"}],
                    headers={"Link": f'<{CATEGORIES}?page=2>; rel="next"'},
                ),
                httpx.Response(200, json=[{"uuid": CATEGORY_UUID, "title": "Two"}]),
            ]
        )
        categories = sync_all_pages(marketplace_categories_list, client=self.client)
        self.assertEqual([c.title for c in categories], ["One", "Two"])
        self.assertEqual(route.call_count, 2)
        for call in route.calls:
            self.assertEqual(call.request.headers["Accept-Encoding"], "identity")

    def test_encoded_answers_are_refused(self):
        body = json.dumps([{"title": "x" * 1000}] * 1000).encode()
        for encoding, payload in (
            ("gzip", gzip.compress(body)),
            # Each layer would be decompressed in full before any size check.
            ("gzip, gzip", gzip.compress(gzip.compress(body))),
            ("deflate", zlib.compress(body)),
            ("br", b"\x0b\x02\x80\x5b\x5d\x03"),
        ):
            with self.subTest(encoding=encoding):
                self.respx.get(url__startswith=CATEGORIES).respond(
                    200,
                    content=payload,
                    headers={
                        "Content-Encoding": encoding,
                        "Content-Type": "application/json",
                    },
                )
                with self.assertRaises(EncodedResponseRefused) as caught:
                    marketplace_categories_list.sync(client=self.client)
                self.assertIsInstance(caught.exception, httpx.TransportError)

    def test_identity_encoding_is_accepted(self):
        self.respx.get(url__startswith=CATEGORIES).respond(
            200, json=[], headers={"Content-Encoding": "identity"}
        )
        self.assertEqual(marketplace_categories_list.sync(client=self.client), [])

    def test_oversized_answer_is_refused(self):
        with mock.patch("waldur_core.core.client.MAX_RESPONSE_BYTES", 1024):
            client = get_waldur_client(API, "token")
        big = json.dumps([{"title": "x" * 100}] * 100).encode()

        def chunks():
            # No Content-Length: the cap must hold while reading.
            for start in range(0, len(big), 256):
                yield big[start : start + 256]

        for name, response in (
            ("declared", httpx.Response(200, content=big)),
            ("streamed", httpx.Response(200, content=chunks())),
        ):
            with self.subTest(name):
                self.respx.get(url__startswith=CATEGORIES).mock(return_value=response)
                with self.assertRaises(ResponseTooLarge):
                    marketplace_categories_list.sync(client=client)

    def test_hostile_pagination_stops_at_the_page_cap(self):
        route = self.respx.get(url__startswith=CATEGORIES).mock(
            side_effect=next_page_forever
        )
        limits = RemoteCallLimits(max_requests=5, timeout=60, max_bytes=10**6)
        with self.assertRaises(TooManyRequests):
            sync_all_pages(
                marketplace_categories_list, client=self.client, limits=limits
            )
        self.assertEqual(route.call_count, 5)

    def test_every_sync_all_has_a_page_cap_by_default(self):
        route = self.respx.get(url__startswith=CATEGORIES).mock(
            side_effect=next_page_forever
        )
        limits = RemoteCallLimits(max_requests=7, timeout=60, max_bytes=10**6)
        with mock.patch("waldur_core.core.client.SYNC_ALL_LIMITS", limits):
            with self.assertRaises(TooManyRequests):
                sync_all_pages(marketplace_categories_list, client=self.client)
        self.assertEqual(route.call_count, 7)

    def test_slow_hostile_pagination_stops_at_the_deadline(self):
        def slow_page(request):
            time.sleep(0.05)
            return next_page_forever(request)

        route = self.respx.get(url__startswith=CATEGORIES).mock(side_effect=slow_page)
        limits = RemoteCallLimits(max_requests=1000, timeout=0.3, max_bytes=10**6)
        started = time.monotonic()
        with self.assertRaises(RemoteCallTimeout):
            sync_all_pages(
                marketplace_categories_list, client=self.client, limits=limits
            )
        self.assertLess(time.monotonic() - started, 2)
        self.assertLess(route.call_count, 20)

    def test_bytes_of_one_call_are_capped(self):
        page = json.dumps([{"title": "x" * 100}] * 10).encode()

        def page_after_page(request):
            number = int(request.url.params.get("page", "1"))
            headers = {}
            if number < 200:
                headers["Link"] = f'<{CATEGORIES}?page={number + 1}>; rel="next"'
            return httpx.Response(200, content=page, headers=headers)

        self.respx.get(url__startswith=CATEGORIES).mock(side_effect=page_after_page)
        limits = RemoteCallLimits(
            max_requests=1000, timeout=60, max_bytes=len(page) * 3
        )
        with self.assertRaises(ResponseTooLarge):
            sync_all_pages(
                marketplace_categories_list, client=self.client, limits=limits
            )

    def test_request_timeouts_are_cut_to_the_deadline(self):
        seen = []

        def record(request):
            seen.append(request.extensions.get("timeout"))
            return httpx.Response(200, json=[])

        self.respx.get(url__startswith=CATEGORIES).mock(side_effect=record)
        limits = RemoteCallLimits(max_requests=5, timeout=2, max_bytes=10**6)
        sync_all_pages(marketplace_categories_list, client=self.client, limits=limits)
        self.assertTrue(seen and seen[0])
        self.assertTrue(all(0 < value <= 2 for value in seen[0].values()))


@override_settings(WALDUR_CORE={"SUBNET_BLACKLIST": ["10.0.0.0/8", "127.0.0.0/8"]})
class RefusedHostAnswerTest(SimpleTestCase):
    """An unresolvable host and an internal one get the same answer."""

    def refusal(self, dns_patcher):
        with dns_patcher, self.assertLogs("waldur_core.core.client", "DEBUG") as logs:
            with self.assertRaises(ClientValidationError) as caught:
                get_waldur_client("https://some.internal.name/api/", "token")
        return caught.exception, logs

    def test_unresolvable_and_internal_hosts_are_refused_alike(self):
        unresolvable, unresolvable_logs = self.refusal(
            create_ssrf_dns_mock_with_error()
        )
        internal, internal_logs = self.refusal(
            create_ssrf_selective_dns_mock({"some.internal.name": "10.1.2.3"})
        )
        self.assertEqual(str(unresolvable), REFUSED_HOST_MESSAGE)
        self.assertEqual(str(internal), str(unresolvable))
        # The log keeps the difference.
        self.assertIn("does not resolve", unresolvable.reason)
        self.assertIn("internal address", internal.reason)
        self.assertIn("does not resolve", "\n".join(unresolvable_logs.output))
        self.assertIn("10.1.2.3", "\n".join(internal_logs.output))

    def test_refusals_are_not_logged_as_errors(self):
        for dns_patcher in (
            create_ssrf_dns_mock_with_error(),
            create_ssrf_selective_dns_mock({"some.internal.name": "10.1.2.3"}),
        ):
            _, logs = self.refusal(dns_patcher)
            self.assertTrue(logs.records)
            for record in logs.records:
                self.assertEqual(record.levelno, logging.WARNING)


@override_settings(WALDUR_CORE={"SUBNET_BLACKLIST": ["10.0.0.0/8", "127.0.0.0/8"]})
class ParsedSizeBoundsTest(SimpleTestCase):
    """A small body of many empty objects costs far more once parsed."""

    def setUp(self):
        self.dns = create_ssrf_selective_dns_mock({"remote.example.com": "8.8.8.8"})
        self.dns.start()
        self.respx = respx.mock(assert_all_called=False)
        self.respx.start()

    def tearDown(self):
        self.respx.stop()
        self.dns.stop()

    def test_response_with_too_many_objects_is_refused(self):
        with mock.patch("waldur_core.core.client.MAX_RESPONSE_VALUES", 1000):
            client = get_waldur_client(API, "token")
        self.respx.get(url__startswith=CATEGORIES).respond(
            200, content=b"[" + b",".join([b"{}"] * 2000) + b"]"
        )
        with self.assertRaises(ResponseTooLarge):
            marketplace_categories_list.sync(client=client)

    def test_objects_of_one_call_are_capped(self):
        client = get_waldur_client(API, "token")
        page = b"[" + b",".join([b"{}"] * 100) + b"]"

        def page_after_page(request):
            number = int(request.url.params.get("page", "1"))
            headers = {}
            if number < 200:
                headers["Link"] = f'<{CATEGORIES}?page={number + 1}>; rel="next"'
            return httpx.Response(200, content=page, headers=headers)

        self.respx.get(url__startswith=CATEGORIES).mock(side_effect=page_after_page)
        limits = RemoteCallLimits(
            max_requests=1000, timeout=60, max_bytes=10**7, max_values=350
        )
        with self.assertRaises(ResponseTooLarge):
            sync_all_pages(marketplace_categories_list, client=client, limits=limits)

    def test_pages_that_parse_large_are_refused_before_they_are_parsed(self):
        size = 8 * 1024 * 1024 - 100
        count = size // 5
        # One emoji makes Python store a whole string at four bytes a
        # character: 8 MB of such text is 32 MB of str.
        wide = '"' + "a" * 2000 + "\U0001f600" + '"'
        pages = {
            # Three containers, but 1.6 million strings: ~90 MB parsed.
            "short strings": '[{"title":"t","junk":["'
            + '","'.join(["ab"] * count)
            + '"]}]',
            "four-byte text": '[{"title":"t","junk":['
            + ",".join([wide] * (size // (len(wide.encode()) + 1)))
            + "]}]",
        }
        for name, page in pages.items():
            with self.subTest(name):
                body = page.encode()
                self.respx.get(url__startswith=CATEGORIES).respond(200, content=body)
                client = get_waldur_client(API, "token")
                tracemalloc.start()
                try:
                    with self.assertRaises(ResponseTooLarge):
                        sync_all_pages(marketplace_categories_list, client=client)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                # Parsed, either page would take 30 MB or more.
                self.assertLess(peak, 16 * 1024 * 1024)

    def test_text_is_charged_at_the_width_python_stores_it(self):
        ascii_page = ('[{"title":"' + "a" * 100_000 + '"}]').encode()
        wide_page = ('[{"title":"' + "a" * 99_998 + "\U0001f600" + '"}]').encode()
        # 100 kB of ASCII is 2,000 values; the same with an emoji is 8,000.
        limits = RemoteCallLimits(
            max_requests=5, timeout=60, max_bytes=10**7, max_values=5000
        )
        client = get_waldur_client(API, "token")
        self.respx.get(url__startswith=CATEGORIES).respond(200, content=ascii_page)
        sync_all_pages(marketplace_categories_list, client=client, limits=limits)
        self.respx.get(url__startswith=CATEGORIES).respond(200, content=wide_page)
        with self.assertRaises(ResponseTooLarge):
            sync_all_pages(marketplace_categories_list, client=client, limits=limits)

    def test_unreadable_json_is_a_refused_call(self):
        client = get_waldur_client(API, "token")
        for name, body in (
            ("invalid", b"[{"),
            ("deep", b"[" * 100_000 + b"]" * 100_000),
            ("long integer", b"[" + b"9" * 5000 + b"]"),
            ("wrong shape", b'{"title": "not a list"}'),
        ):
            with self.subTest(name):
                self.respx.get(url__startswith=CATEGORIES).respond(200, content=body)
                with self.assertRaises(InvalidRemoteAnswer) as caught:
                    sync_all_pages(marketplace_categories_list, client=client)
                self.assertIsInstance(caught.exception, httpx.TransportError)

    def test_refusal_reason_is_in_the_log_text_only(self):
        self.respx.get(url__startswith=CATEGORIES).respond(
            200, content=gzip.compress(b"[]"), headers={"Content-Encoding": "gzip"}
        )
        client = get_waldur_client(API, "token")
        with self.assertRaises(EncodedResponseRefused) as caught:
            marketplace_categories_list.sync(client=client)
        self.assertIn("gzip", str(caught.exception))
        self.assertNotIn("gzip", caught.exception.user_message)


@override_settings(WALDUR_CORE={"SUBNET_BLACKLIST": ["10.0.0.0/8", "127.0.0.0/8"]})
class FetchWithinBudgetTest(SimpleTestCase):
    def test_spent_budget_stops_fetches_before_dns(self):
        lookups = []

        def slow_lookup(host, *args, **kwargs):
            lookups.append(host)
            time.sleep(0.3)
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")

        errors = []
        started = time.monotonic()
        with mock.patch("socket.getaddrinfo", side_effect=slow_lookup):
            limits = RemoteCallLimits(max_requests=60, timeout=0.5, max_bytes=10**6)
            with remote_call_limits(limits):
                for number in range(10):
                    try:
                        fetch_url(f"http://img{number}.example.com/x.png")
                    except httpx.HTTPError as exc:
                        errors.append(exc)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertLessEqual(len(lookups), 2)
        self.assertIsInstance(errors[-1], RemoteCallTimeout)
