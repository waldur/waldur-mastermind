from django.http import HttpRequest
from django.test import TestCase

from waldur_core.core.utils import get_ip_address


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
