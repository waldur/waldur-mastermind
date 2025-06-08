from django.test import override_settings
from rest_framework import test
from waldur_api_client.api.marketplace_categories import marketplace_categories_list
from waldur_api_client.models.marketplace_categories_list_field_item import (
    MarketplaceCategoriesListFieldItem,
)

import respx
from waldur_core.core.client import ClientValidationError, check_url, get_waldur_client
from waldur_core.core.tests.dns_utils import (
    create_ssrf_dns_mock_with_error,
    create_ssrf_selective_dns_mock,
)


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
class SSRFProtectionTest(test.APITransactionTestCase):
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
            self.assertIn(
                "Access to internal IPs is not allowed", exc_info.exception.args[0]
            )

    def test_blocks_kubernetes_ip(self):
        with create_ssrf_selective_dns_mock({"k8s.example.com": "10.43.0.1"}):
            with self.assertRaises(ClientValidationError) as exc_info:
                check_url("https://k8s.example.com/api/endpoint")
                print("After check_url")
            self.assertIn(
                "Access to internal IPs is not allowed", exc_info.exception.args[0]
            )

    def test_blocks_dns_resolution_failure(self):
        with create_ssrf_dns_mock_with_error():
            with self.assertRaises(ClientValidationError) as exc_info:
                check_url("https://invalid.example.com/api/endpoint")
            self.assertIn("Error resolving client hostname", exc_info.exception.args[0])

    def test_wrong_url_format_raises_error(self):
        """
        This test should fail because the URL is not valid.
        """
        with self.assertRaises(ClientValidationError) as exc_info:
            check_url("/api/endpoint")
        self.assertIn("Error resolving client hostname", exc_info.exception.args[0])

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
                    MarketplaceCategoriesListFieldItem.UUID,
                    MarketplaceCategoriesListFieldItem.TITLE,
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
            self.assertIn(
                "Access to internal IPs is not allowed", exc_info.exception.args[0]
            )
