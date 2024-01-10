import responses
from django.test import TestCase, override_settings
from rest_framework import status

from waldur_core.structure.tests.factories import UserFactory


class ClientTest(TestCase):
    URL = "/api-auth/bcc/user-details/"

    @override_settings(WALDUR_AUTH_BCC={"ENABLED": False})
    def test_feature_is_disabled(self):
        self.client.force_login(UserFactory())
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(WALDUR_AUTH_BCC={"ENABLED": True})
    def test_required_params_are_validated(self):
        self.client.force_login(UserFactory())
        response = self.client.get(self.URL, {"civil_number": "nid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @responses.activate
    @override_settings(
        WALDUR_AUTH_BCC={
            "ENABLED": True,
            "BASE_API_URL": "http://example.com/",
            "USERNAME": "admin",
            "PASSWORD": "secret",
        }
    )
    def test_error_is_handled(self):
        self.client.force_login(UserFactory())
        responses.add(
            responses.GET, "http://example.com/", json={"error": "Invalid request"}
        )
        response = self.client.get(
            self.URL, {"civil_number": "123", "tax_number": "456-789"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["details"], "Invalid request")

    @responses.activate
    @override_settings(
        WALDUR_AUTH_BCC={
            "ENABLED": True,
            "BASE_API_URL": "http://example.com/",
            "USERNAME": "admin",
            "PASSWORD": "secret",
        }
    )
    def test_empty_response_is_rendered_as_error(self):
        self.client.force_login(UserFactory())
        responses.add(
            responses.GET,
            "http://example.com/",
            json={
                "nameen": "",
                "namebn": "",
                "desig": "",
                "office": "",
            },
        )
        response = self.client.get(
            self.URL, {"civil_number": "123", "tax_number": "456-789"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @responses.activate
    @override_settings(
        WALDUR_AUTH_BCC={
            "ENABLED": True,
            "BASE_API_URL": "http://example.com/",
            "USERNAME": "admin",
            "PASSWORD": "secret",
        }
    )
    def test_user_details_are_rendered(self):
        self.client.force_login(UserFactory())
        responses.add(
            responses.GET,
            "http://example.com/",
            json={
                "nameen": "User",
                "namebn": "User",
                "desig": "সহকারী সচিব",
                "office": "Secretariat",
            },
        )
        response = self.client.get(
            self.URL, {"civil_number": "123", "tax_number": "456-789"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "full_name": "User",
                "native_name": "User",
                "job_title": "সহকারী সচিব",
                "organization": "Secretariat",
            },
        )
