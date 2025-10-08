from unittest.mock import Mock, patch

import requests
from constance.test import override_config
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.onboarding import enums
from waldur_core.onboarding.backends.base import ValidationRequest
from waldur_core.onboarding.backends.estonia import EstonianAriregisterBackend
from waldur_core.structure.tests import factories as structure_factories

from .factories import OnboardingVerificationFactory
from .fixtures import (
    AUTHORIZED_CIVIL_NUMBER,
    NONEXISTENT_CIVIL_NUMBER,
    UNAUTHORIZED_CIVIL_NUMBER,
    get_estonian_ariregister_empty_response,
    get_estonian_ariregister_success_response,
)


class EstonianAriregisterAPITest(APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(
            civil_number=AUTHORIZED_CIVIL_NUMBER
        )
        self.client.force_authenticate(user=self.user)
        self.legal_person_identifier = "70000310"

    def _request_validate_company(
        self,
        legal_person_identifier=None,
        legal_name="Test Company",
        user_submitted_customer_metadata=None,
    ):
        """
        Helper method to make a validate_company API request.

        Args:
            legal_person_identifier: Company registration code (defaults to self.legal_person_identifier)
            legal_name: Company name (defaults to "Test Company")
            user_submitted_customer_metadata: Additional customer data (defaults to empty dict)

        Returns:
            Response object from the API
        """
        data = {
            "country": "EE",
            "legal_person_identifier": legal_person_identifier
            or self.legal_person_identifier,
            "legal_name": legal_name,
            "user_submitted_customer_metadata": user_submitted_customer_metadata or {},
        }
        return self.client.post(
            "/api/onboarding-verifications/validate_company/", data, format="json"
        )

    def _mock_ariregister_response(self, mock_post, response_data):
        """
        Helper method to configure mock API response.

        Args:
            mock_post: Mock object for requests.post
            response_data: Data to return from the mocked API call
        """
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = response_data
        mock_post.return_value = mock_response

    def _assert_verification_status(
        self, verification, expected_status, expected_error_message=None
    ):
        """
        Helper method to assert verification status and error message.

        Args:
            verification: Verification data from API response
            expected_status: Expected status value
            expected_error_message: Expected error message (optional)
        """
        self.assertEqual(verification["status"], expected_status)
        if expected_error_message:
            self.assertEqual(verification["error_message"], expected_error_message)

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="",
        ONBOARDING_ARIREGISTER_PASSWORD="",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    def test_missing_username_raises_error(self):
        response = self._request_validate_company()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification = response.data

        self._assert_verification_status(verification, "failed", "CONFIGURATION_ERROR")
        self.assertIn("not configured", verification["error_traceback"])

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    def test_missing_password_raises_error(self):
        response = self._request_validate_company()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification = response.data

        self._assert_verification_status(verification, "failed", "CONFIGURATION_ERROR")
        self.assertIn("not configured", verification["error_traceback"])

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_user_has_authority_to_represent_company(self, mock_post):
        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

        response = self._request_validate_company()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification = response.data

        self._assert_verification_status(verification, "verified")
        self.assertEqual(verification["validation_method"], "ariregister")
        self.assertEqual(verification["verified_user_roles"], ["ASES"])

        # Verify company data was normalized correctly
        self.assertEqual(
            verification["verified_company_data"]["name"],
            "Registrite ja Infosüsteemide Keskus",
        )
        self.assertEqual(
            verification["verified_company_data"]["legal_person_identifier"], "70000310"
        )
        self.assertEqual(
            verification["verified_company_data"]["status"], "Entered into the register"
        )
        self.assertEqual(
            verification["verified_company_data"]["registry"],
            "Estonian Business Register",
        )

        # Verify API was called with correct parameters
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("username", call_args[1]["data"].decode("utf-8"))
        self.assertIn("password", call_args[1]["data"].decode("utf-8"))
        self.assertIn("70000310", call_args[1]["data"].decode("utf-8"))

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_user_does_not_exist_in_company_representatives(self, mock_post):
        different_user = structure_factories.UserFactory(
            civil_number=NONEXISTENT_CIVIL_NUMBER
        )
        self.client.force_authenticate(user=different_user)

        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

        response = self._request_validate_company()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification = response.data

        self._assert_verification_status(verification, "failed", "NOT_AUTHORIZED")
        self.assertEqual(verification["validation_method"], "ariregister")
        self.assertEqual(verification["verified_user_roles"], [])
        self.assertIn(
            "not listed as authorized representative", verification["error_traceback"]
        )

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_user_exists_but_has_no_representation_authority(self, mock_post):
        no_authority_user = structure_factories.UserFactory(
            civil_number=UNAUTHORIZED_CIVIL_NUMBER
        )
        self.client.force_authenticate(user=no_authority_user)

        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

        response = self._request_validate_company()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification = response.data
        self._assert_verification_status(verification, "failed", "NOT_AUTHORIZED")
        self.assertEqual(verification["validation_method"], "ariregister")
        self.assertEqual(verification["verified_user_roles"], ["KOAS"])
        self.assertIn(
            "not listed as authorized representative", verification["error_traceback"]
        )

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_api_request_exception_handling(self, mock_post):
        mock_post.side_effect = requests.RequestException("Connection failed")

        response = self._request_validate_company()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification = response.data
        self._assert_verification_status(verification, "failed", "API_ERROR")
        self.assertEqual(verification["validation_method"], "ariregister")
        self.assertIn("Äriregister API error", verification["error_traceback"])

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_company_not_found_in_response(self, mock_post):
        self._mock_ariregister_response(
            mock_post, get_estonian_ariregister_empty_response()
        )

        response = self._request_validate_company(legal_person_identifier="99999999")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification = response.data
        self._assert_verification_status(verification, "failed", "COMPANY_NOT_FOUND")
        self.assertEqual(verification["validation_method"], "ariregister")
        self.assertIn("not found", verification["error_traceback"])

    def test_create_customer_from_verified_validation(self):
        verification = OnboardingVerificationFactory(
            user=self.user,
            status="verified",
            validation_method=enums.ValidationMethod.ARIREGISTER,
            verified_company_data={
                "name": "Test Company Ltd",
                "legal_person_identifier": "12345678",
                "status": "Active",
                "registry": "Estonian Business Register",
            },
        )

        response = self.client.post(
            f"/api/onboarding-verifications/{verification.uuid}/create_customer/"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("uuid", response.data)

    def test_no_backend_available_for_unsupported_country(self):
        """Test validation fails when no backend is available for the country."""
        data = {
            "country": "US",  # Unsupported country
            "legal_person_identifier": "12345678",
            "legal_name": "Test Company",
            "user_submitted_customer_metadata": {},
        }

        response = self.client.post(
            "/api/onboarding-verifications/validate_company/", data, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Country 'US' is not supported", response.data["country"][0])


class EstonianAriregisterBackendTest(TestCase):
    """Direct backend unit tests - comprehensive testing coverage."""

    def setUp(self):
        self.backend = EstonianAriregisterBackend()
        self.user = structure_factories.UserFactory(
            civil_number=AUTHORIZED_CIVIL_NUMBER
        )
        self.legal_person_identifier = "70000310"

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="",
        ONBOARDING_ARIREGISTER_PASSWORD="",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    def test_missing_username_raises_error(self):
        request = ValidationRequest(
            country="EE",
            person_identifier=AUTHORIZED_CIVIL_NUMBER,
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company",
        )

        with self.assertRaises(ValueError) as context:
            self.backend.validate_company(request)

        self.assertIn("not configured in Constance settings", str(context.exception))

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    def test_missing_password_raises_error(self):
        request = ValidationRequest(
            country="EE",
            person_identifier=AUTHORIZED_CIVIL_NUMBER,
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company",
        )

        with self.assertRaises(ValueError) as context:
            self.backend.validate_company(request)

        self.assertIn("not configured in Constance settings", str(context.exception))

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_user_has_authority_to_represent_company(self, mock_post):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = get_estonian_ariregister_success_response(
            self.legal_person_identifier
        )
        mock_post.return_value = mock_response

        request = ValidationRequest(
            country="EE",
            person_identifier=AUTHORIZED_CIVIL_NUMBER,
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company",
        )

        result = self.backend.validate_company(request)

        self.assertTrue(result.is_valid)
        self.assertEqual(result.method_used, "ariregister")
        self.assertEqual(result.user_roles, ["ASES"])
        self.assertIsNone(result.error_code)
        self.assertIsNone(result.error_message)

        self.assertEqual(
            result.company_data["name"], "Registrite ja Infosüsteemide Keskus"
        )
        self.assertEqual(result.company_data["legal_person_identifier"], "70000310")
        self.assertEqual(result.company_data["status"], "Entered into the register")
        self.assertEqual(result.company_data["registry"], "Estonian Business Register")

        # Verify API was called with correct parameters
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("username", call_args[1]["data"].decode("utf-8"))
        self.assertIn("password", call_args[1]["data"].decode("utf-8"))
        self.assertIn("70000310", call_args[1]["data"].decode("utf-8"))

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_user_does_not_exist_in_company_representatives(self, mock_post):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = get_estonian_ariregister_success_response(
            self.legal_person_identifier
        )
        mock_post.return_value = mock_response

        request = ValidationRequest(
            country="EE",
            person_identifier=NONEXISTENT_CIVIL_NUMBER,
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company",
        )

        result = self.backend.validate_company(request)

        self.assertFalse(result.is_valid)
        self.assertEqual(result.method_used, "ariregister")
        self.assertEqual(result.user_roles, [])
        self.assertEqual(result.error_code, "NOT_AUTHORIZED")
        self.assertIn("not listed as authorized representative", result.error_message)

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_user_exists_but_has_no_representation_authority(self, mock_post):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = get_estonian_ariregister_success_response(
            self.legal_person_identifier
        )
        mock_post.return_value = mock_response

        request = ValidationRequest(
            country="EE",
            person_identifier=UNAUTHORIZED_CIVIL_NUMBER,
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company",
        )

        result = self.backend.validate_company(request)

        self.assertFalse(result.is_valid)
        self.assertEqual(result.method_used, "ariregister")
        self.assertEqual(result.user_roles, ["KOAS"])
        self.assertEqual(result.error_code, "NOT_AUTHORIZED")
        self.assertIn("not listed as authorized representative", result.error_message)

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_api_request_exception_handling(self, mock_post):
        mock_post.side_effect = requests.RequestException("Connection failed")

        request = ValidationRequest(
            country="EE",
            person_identifier=AUTHORIZED_CIVIL_NUMBER,
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company",
        )

        result = self.backend.validate_company(request)

        self.assertFalse(result.is_valid)
        self.assertEqual(result.method_used, "ariregister")
        self.assertEqual(result.error_code, "API_ERROR")
        self.assertIn("Äriregister API error", result.error_message)

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_company_not_found_in_response(self, mock_post):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = get_estonian_ariregister_empty_response()
        mock_post.return_value = mock_response

        request = ValidationRequest(
            country="EE",
            person_identifier=AUTHORIZED_CIVIL_NUMBER,
            legal_person_identifier="99999999",
            legal_name="Test Company",
        )

        result = self.backend.validate_company(request)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.method_used, "ariregister")
        self.assertEqual(result.error_code, "COMPANY_NOT_FOUND")
        self.assertIn("not found", result.error_message)
