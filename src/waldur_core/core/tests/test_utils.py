import logging
import warnings
from unittest import mock

import requests
from django.http import HttpRequest
from django.test import TestCase
from urllib3.exceptions import InsecureRequestWarning

from waldur_core.core.models import User
from waldur_core.core.utils import (
    QuietSession,
    chunked_queryset,
    get_ip_address,
    ip_in_networks,
    merge_access_subnets,
    normalize_ip_address,
)


class GetIpAddressTest(TestCase):
    def test_get_ip_address_from_x_forwarded_for_header(self):
        request = HttpRequest()
        request.META["HTTP_X_FORWARDED_FOR"] = "192.168.1.100, 10.0.0.1"

        result = get_ip_address(request)

        self.assertEqual(result, "192.168.1.100")

    def test_get_ip_address_from_x_forwarded_for_header_single_ip(self):
        request = HttpRequest()
        request.META["HTTP_X_FORWARDED_FOR"] = "203.0.113.45"

        result = get_ip_address(request)

        self.assertEqual(result, "203.0.113.45")

    def test_get_ip_address_from_x_forwarded_for_header_with_spaces(self):
        request = HttpRequest()
        request.META["HTTP_X_FORWARDED_FOR"] = "  192.168.1.100  , 10.0.0.1"

        result = get_ip_address(request)

        self.assertEqual(result, "192.168.1.100")

    def test_get_ip_address_from_remote_addr_when_no_x_forwarded_for(self):
        request = HttpRequest()
        request.META["REMOTE_ADDR"] = "192.168.1.50"

        result = get_ip_address(request)

        self.assertEqual(result, "192.168.1.50")

    def test_get_ip_address_prefers_x_forwarded_for_over_remote_addr(self):
        request = HttpRequest()
        request.META["HTTP_X_FORWARDED_FOR"] = "192.168.1.100"
        request.META["REMOTE_ADDR"] = "192.168.1.50"

        result = get_ip_address(request)

        self.assertEqual(result, "192.168.1.100")

    def test_get_ip_address_returns_none_when_no_headers_present(self):
        request = HttpRequest()

        result = get_ip_address(request)

        self.assertIsNone(result)

    def test_get_ip_address_returns_none_when_headers_empty(self):
        request = HttpRequest()
        request.META["HTTP_X_FORWARDED_FOR"] = ""
        request.META["REMOTE_ADDR"] = ""

        result = get_ip_address(request)

        self.assertEqual(result, "")


class ChunkedQuerysetTest(TestCase):
    def setUp(self):
        self.users = [
            User.objects.create(username=f"chunked-user-{i}") for i in range(7)
        ]

    def test_yields_all_rows_across_multiple_chunks(self):
        result = list(chunked_queryset(User.objects.all(), chunk_size=3))
        self.assertEqual(len(result), len(self.users))
        self.assertEqual({u.pk for u in result}, {u.pk for u in self.users})

    def test_yields_in_pk_order(self):
        result = list(chunked_queryset(User.objects.all(), chunk_size=2))
        pks = [u.pk for u in result]
        self.assertEqual(pks, sorted(pks))

    def test_empty_queryset(self):
        User.objects.all().delete()
        self.assertEqual(list(chunked_queryset(User.objects.all(), chunk_size=10)), [])

    def test_respects_filter(self):
        target_pks = {u.pk for u in self.users[:3]}
        qs = User.objects.filter(pk__in=target_pks)
        result = list(chunked_queryset(qs, chunk_size=2))
        self.assertEqual({u.pk for u in result}, target_pks)

    def test_max_records_truncates_and_warns(self):
        with self.assertLogs("waldur_core.core.utils", level=logging.WARNING) as logs:
            result = list(
                chunked_queryset(User.objects.all(), chunk_size=2, max_records=3)
            )
        self.assertEqual(len(result), 3)
        self.assertTrue(
            any("max_records=3" in line for line in logs.output),
            f"expected truncation warning in {logs.output!r}",
        )

    def test_chunk_size_larger_than_dataset(self):
        result = list(chunked_queryset(User.objects.all(), chunk_size=100))
        self.assertEqual(len(result), len(self.users))


class MergeAccessSubnetsTest(TestCase):
    def test_adjacent_hosts_collapse(self):
        result = merge_access_subnets(["192.168.1.0/32", "192.168.1.1/32"])
        self.assertEqual([str(n) for n in result], ["192.168.1.0/31"])

    def test_non_adjacent_stay_separate_and_sorted(self):
        result = merge_access_subnets(["10.0.5.20/32", "192.168.1.5/32"])
        self.assertEqual([str(n) for n in result], ["10.0.5.20/32", "192.168.1.5/32"])

    def test_invalid_and_null_values_are_skipped(self):
        result = merge_access_subnets([None, "not-an-ip", "192.168.1.5/32"])
        self.assertEqual([str(n) for n in result], ["192.168.1.5/32"])

    def test_empty_input(self):
        self.assertEqual(merge_access_subnets([]), [])


class IpInNetworksTest(TestCase):
    def test_address_inside_network(self):
        self.assertTrue(ip_in_networks("203.0.113.5", ["203.0.113.0/24"]))

    def test_address_outside_network(self):
        self.assertFalse(ip_in_networks("198.51.100.5", ["203.0.113.0/24"]))

    def test_ipv6_match(self):
        self.assertTrue(ip_in_networks("2001:db8::1", ["2001:db8::/32"]))

    def test_version_mismatch_does_not_match(self):
        self.assertFalse(ip_in_networks("203.0.113.5", ["2001:db8::/32"]))

    def test_ipv4_mapped_ipv6_matches_ipv4_network(self):
        # A dual-stack listener reports IPv4 clients as ::ffff:203.0.113.5.
        # Without unwrapping, the version mismatch would deny a legitimate
        # client under fail-closed enforcement.
        self.assertTrue(ip_in_networks("::ffff:203.0.113.5", ["203.0.113.0/24"]))

    def test_ipv4_mapped_ipv6_outside_network_does_not_match(self):
        self.assertFalse(ip_in_networks("::ffff:198.51.100.9", ["203.0.113.0/24"]))

    def test_none_never_matches(self):
        self.assertFalse(ip_in_networks(None, ["203.0.113.0/24"]))

    def test_garbage_never_matches(self):
        self.assertFalse(ip_in_networks("not-an-ip", ["203.0.113.0/24"]))

    def test_address_with_port_is_matched(self):
        # get_ip_address returns the raw header value; a proxy that appends
        # host:port to X-Forwarded-For must still resolve to a matching address.
        self.assertTrue(ip_in_networks("203.0.113.5:5678", ["203.0.113.0/24"]))

    def test_bracketed_ipv6_with_port_is_matched(self):
        self.assertTrue(ip_in_networks("[2001:db8::1]:5678", ["2001:db8::/32"]))

    def test_garbage_port_never_matches(self):
        self.assertFalse(ip_in_networks("203.0.113.5:notaport", ["203.0.113.0/24"]))


class NormalizeIpAddressTest(TestCase):
    def test_plain_ipv4(self):
        self.assertEqual(normalize_ip_address("203.0.113.5"), "203.0.113.5")

    def test_plain_ipv6_is_canonicalised(self):
        self.assertEqual(normalize_ip_address("2001:DB8::1"), "2001:db8::1")

    def test_ipv4_mapped_ipv6_is_unwrapped(self):
        self.assertEqual(normalize_ip_address("::ffff:203.0.113.5"), "203.0.113.5")

    def test_host_port_is_stripped(self):
        self.assertEqual(normalize_ip_address("1.2.3.4:5678"), "1.2.3.4")

    def test_bracketed_ipv6_port_is_stripped(self):
        self.assertEqual(normalize_ip_address("[2001:db8::1]:5678"), "2001:db8::1")

    def test_garbage_returns_none(self):
        self.assertIsNone(normalize_ip_address("not-an-ip"))

    def test_braces_return_none(self):
        # A format-placeholder-looking header must never survive to an event
        # message .format() template or an inet column.
        self.assertIsNone(normalize_ip_address("{oops}"))

    def test_none_returns_none(self):
        self.assertIsNone(normalize_ip_address(None))


class QuietSessionTest(TestCase):
    """QuietSession must swallow urllib3's InsecureRequestWarning on unverified
    requests without over-suppressing verified ones.

    The transport is faked at requests.Session.request (QuietSession.request's
    super()) so no real HTTP happens; the fake simply emits the warning the way
    urllib3 does on a new insecure connection.
    """

    def _record_warnings(self, session, verify):
        # The reset + simplefilter("always") MUST live inside the recorder.
        # openstacksdk installs a process-global filterwarnings("ignore",
        # InsecureRequestWarning) the first time it connects with verify off
        # (issue #66, Defect 2); without resetting it here a prior test in the
        # same process would silence the warning and make every assertion below
        # pass vacuously. The positive control (plain Session) keeps this honest.
        def emit_insecure_warning(*args, **kwargs):
            warnings.warn("Unverified HTTPS request", InsecureRequestWarning)
            return mock.sentinel.response

        with warnings.catch_warnings(record=True) as recorded:
            warnings.resetwarnings()
            warnings.simplefilter("always", InsecureRequestWarning)
            with mock.patch.object(
                requests.Session, "request", side_effect=emit_insecure_warning
            ):
                session.request("GET", "https://example.invalid", verify=verify)
        return [w for w in recorded if issubclass(w.category, InsecureRequestWarning)]

    def test_plain_session_emits_warning(self):
        # Positive control: without QuietSession the warning reaches the caller.
        # If this ever stops recording, the negative assertions are worthless.
        session = requests.Session()
        recorded = self._record_warnings(session, verify=False)
        self.assertEqual(len(recorded), 1)

    def test_quiet_session_suppresses_warning_when_unverified(self):
        session = QuietSession()
        recorded = self._record_warnings(session, verify=False)
        self.assertEqual(recorded, [])

    def test_quiet_session_does_not_suppress_when_verified(self):
        # No over-suppression: a verified request still surfaces the warning
        # (it just would not normally be raised for a verified connection).
        session = QuietSession()
        recorded = self._record_warnings(session, verify=True)
        self.assertEqual(len(recorded), 1)
