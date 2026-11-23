"""Tests for Arrow API backend client."""

from unittest import mock

import requests
from django.test import TestCase

from waldur_mastermind.waldur_arrow.backend import (
    ArrowBackendError,
    ArrowClient,
    ArrowCredentials,
)


class ArrowCredentialsTest(TestCase):
    """Tests for ArrowCredentials dataclass."""

    def test_get_base_url_adds_trailing_slash(self):
        creds = ArrowCredentials(
            api_url="https://api.arrow.test",
            api_key="test-key",
        )
        self.assertEqual(creds.get_base_url(), "https://api.arrow.test/")

    def test_get_base_url_preserves_existing_slash(self):
        creds = ArrowCredentials(
            api_url="https://api.arrow.test/",
            api_key="test-key",
        )
        self.assertEqual(creds.get_base_url(), "https://api.arrow.test/")


class ArrowClientPingTest(TestCase):
    """Tests for ArrowClient.ping() method."""

    def setUp(self):
        self.credentials = ArrowCredentials(
            api_url="https://api.arrow.test/",
            api_key="test-key",
        )
        self.client = ArrowClient(self.credentials)

    @mock.patch("requests.Session.get")
    def test_ping_success(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": 200,
            "data": {
                "reference": "XSP12345",
                "companyName": "Test Company",
            },
        }

        result = self.client.ping()

        self.assertTrue(result["valid"])
        self.assertEqual(result["data"]["reference"], "XSP12345")
        self.assertEqual(result["data"]["companyName"], "Test Company")

    @mock.patch("requests.Session.get")
    def test_ping_failure_non_200_status(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": 401,
            "message": "Unauthorized",
        }

        result = self.client.ping()

        self.assertFalse(result["valid"])

    @mock.patch("requests.Session.get")
    def test_ping_handles_request_exception(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection error")

        result = self.client.ping()

        self.assertFalse(result["valid"])
        self.assertIn("error", result)


class ArrowClientCustomersTest(TestCase):
    """Tests for ArrowClient customer methods."""

    def setUp(self):
        self.credentials = ArrowCredentials(
            api_url="https://api.arrow.test/",
            api_key="test-key",
        )
        self.client = ArrowClient(self.credentials)

    @mock.patch("requests.Session.get")
    def test_list_customers(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "data": {
                "customers": [
                    {"reference": "XSP001", "companyName": "Customer 1"},
                    {"reference": "XSP002", "companyName": "Customer 2"},
                ]
            },
            "pagination": {"totalPages": 1},
        }

        result = self.client.list_customers()

        self.assertEqual(len(result["data"]["customers"]), 2)
        mock_get.assert_called_once()

    @mock.patch("requests.Session.get")
    def test_get_customer(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "data": {
                "customer": {
                    "reference": "XSP001",
                    "companyName": "Test Customer",
                }
            }
        }

        result = self.client.get_customer("XSP001")

        self.assertEqual(result["data"]["customer"]["reference"], "XSP001")


class ArrowClientBillingTest(TestCase):
    """Tests for ArrowClient billing methods."""

    def setUp(self):
        self.credentials = ArrowCredentials(
            api_url="https://api.arrow.test/",
            api_key="test-key",
        )
        self.client = ArrowClient(self.credentials)

    @mock.patch("requests.Session.get")
    def test_list_export_types(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "data": {
                "exportTypes": [
                    {"reference": "TYPE1", "name": "Export Type 1"},
                    {"reference": "TYPE2", "name": "Export Type 2"},
                ]
            }
        }

        result = self.client.list_export_types()

        self.assertEqual(len(result["data"]["exportTypes"]), 2)

    @mock.patch("requests.Session.post")
    def test_export_billing_sync(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "data": {
                "headers": ["Line Reference", "Sell Total Price"],
                "values": [
                    ["LINE-001", "100.00"],
                    ["LINE-002", "200.00"],
                ],
            },
            "pagination": {"perPage": 1000, "currentPage": 1},
        }

        result = self.client.export_billing_sync(
            export_type_reference="TYPE1",
            period_from="2024-01",
            period_to="2024-01",
        )

        self.assertEqual(len(result["data"]["values"]), 2)
        mock_post.assert_called_once()

    def test_parse_billing_export_to_dicts(self):
        export_data = {
            "headers": ["Line Reference", "Sell Total Price", "Vendor Name"],
            "values": [
                ["LINE-001", "100.00", "Microsoft"],
                ["LINE-002", "200.00", "Amazon"],
            ],
        }

        result = self.client.parse_billing_export_to_dicts(export_data)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["Line Reference"], "LINE-001")
        self.assertEqual(result[0]["Sell Total Price"], "100.00")
        self.assertEqual(result[0]["Vendor Name"], "Microsoft")
        self.assertEqual(result[1]["Line Reference"], "LINE-002")


def _response(payload):
    response = mock.Mock(status_code=200, ok=True, text="{}")
    response.json.return_value = payload
    return response


class ArrowClientPaginationTest(TestCase):
    """Pagination as Arrow's SDKs describe it."""

    def setUp(self):
        self.client = ArrowClient(
            ArrowCredentials(api_url="https://api.arrow.test/", api_key="key")
        )

    @mock.patch("requests.Session.get")
    def test_list_all_customers_follows_snake_case_total_page(self, mock_get):
        mock_get.side_effect = [
            _response(
                {
                    "data": {"customers": [{"Reference": "XSP1"}]},
                    "pagination": {"per_page": 1, "current_page": 1, "total_page": 2},
                }
            ),
            _response(
                {
                    "data": {"customers": [{"Reference": "XSP2"}]},
                    "pagination": {"per_page": 1, "current_page": 2, "total_page": 2},
                }
            ),
        ]

        customers = self.client.list_all_customers()

        self.assertEqual([c["Reference"] for c in customers], ["XSP1", "XSP2"])

    @mock.patch("requests.Session.post")
    def test_export_sends_page_as_query_and_stops_at_total_pages(self, mock_post):
        full_page = [[f"L{i}"] for i in range(1000)]
        mock_post.side_effect = [
            _response(
                {
                    "data": {"headers": ["Sequence"], "values": full_page},
                    "pagination": {"perPage": 1000, "currentPage": 1, "totalPages": 2},
                }
            ),
            _response(
                {
                    "data": {
                        "headers": ["Sequence"],
                        "values": [[f"M{i}"] for i in range(1000)],
                    },
                    "pagination": {"perPage": 1000, "currentPage": 2, "totalPages": 2},
                }
            ),
        ]

        result = self.client.export_billing_all_pages("TYPE", "2024-01", "2024-01")

        self.assertEqual(len(result["values"]), 2000)
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(
            [c.kwargs["params"] for c in mock_post.call_args_list],
            [{"page": 1}, {"page": 2}],
        )
        self.assertEqual(mock_post.call_args_list[1].kwargs["json"]["page"], 2)

    @mock.patch("requests.Session.post")
    def test_export_raises_when_server_repeats_a_page(self, mock_post):
        mock_post.return_value = _response(
            {
                "data": {
                    "headers": ["Sequence"],
                    "values": [[f"L{i}"] for i in range(1000)],
                },
                "pagination": {"perPage": 1000},
            }
        )

        with self.assertRaises(ArrowBackendError):
            self.client.export_billing_all_pages("TYPE", "2024-01", "2024-01")
        self.assertEqual(mock_post.call_count, 2)


class ArrowClientTimeoutTest(TestCase):
    def setUp(self):
        self.client = ArrowClient(
            ArrowCredentials(api_url="https://api.arrow.test/", api_key="key")
        )

    @mock.patch("requests.Session.get")
    def test_get_has_timeout(self, mock_get):
        mock_get.return_value = _response({"status": 200, "data": {}})

        self.client.ping()

        self.assertEqual(
            mock_get.call_args.kwargs["timeout"], ArrowClient.REQUEST_TIMEOUT
        )

    @mock.patch("requests.Session.post")
    def test_post_has_timeout(self, mock_post):
        mock_post.return_value = _response({"data": {"headers": [], "values": []}})

        self.client.export_billing_sync("TYPE", "2024-01", "2024-01")

        self.assertEqual(
            mock_post.call_args.kwargs["timeout"], ArrowClient.REQUEST_TIMEOUT
        )

    @mock.patch("requests.Session.get", side_effect=requests.exceptions.ReadTimeout())
    def test_timeout_becomes_backend_error(self, mock_get):
        with self.assertRaises(ArrowBackendError):
            self.client.get_customer("XSP1")


class ArrowClientLicensesTest(TestCase):
    """licenses/v2 with its search_after cursor (v1 returns 410 since 2026-09-30)."""

    def setUp(self):
        self.client = ArrowClient(
            ArrowCredentials(api_url="https://api.arrow.test/", api_key="key")
        )

    @mock.patch("requests.Session.get")
    def test_list_all_licenses_follows_cursor(self, mock_get):
        mock_get.side_effect = [
            _response(
                {
                    "data": {"licenses": [{"license_id": "XSP1"}]},
                    "pagination": {"next": "/v2/licenses?per_page=1&search_after=42"},
                }
            ),
            _response(
                {
                    "data": {"licenses": [{"license_id": "XSP2"}]},
                    "pagination": {"next": None},
                }
            ),
        ]

        licenses = self.client.list_all_licenses()

        self.assertEqual([lic["license_id"] for lic in licenses], ["XSP1", "XSP2"])
        first, second = mock_get.call_args_list
        self.assertTrue(first.args[0].endswith("/licenses/v2"))
        self.assertNotIn("search_after", first.kwargs["params"])
        self.assertEqual(second.kwargs["params"]["search_after"], "42")
