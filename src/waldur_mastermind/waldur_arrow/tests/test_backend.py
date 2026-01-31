"""Tests for Arrow API backend client."""

from unittest import mock

import requests
from django.test import TestCase

from waldur_mastermind.waldur_arrow.backend import (
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
