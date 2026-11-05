"""The remote Waldur views do not hand the remote's answer back to the caller."""

import socket
from unittest import mock

import httpx
import respx
from rest_framework import status, test

from waldur_core.structure.tests.factories import UserFactory
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


class RemoteViewErrorTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()
        self.client.force_login(UserFactory())

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
                self.assertEqual(
                    response.data["api_url"],
                    ["Access to internal IPs is not allowed"],
                )
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
