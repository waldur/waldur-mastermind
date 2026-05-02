import logging

from django.http import HttpRequest
from django.test import TestCase

from waldur_core.core.models import User
from waldur_core.core.utils import chunked_queryset, get_ip_address


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
