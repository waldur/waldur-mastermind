from unittest.mock import Mock, patch

import requests
from constance.test import override_config
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.onboarding import enums, models
from waldur_core.onboarding.backends.base import ValidationRequest
from waldur_core.onboarding.backends.estonia import EstonianAriregisterBackend
from waldur_core.structure.tests import factories as structure_factories

from . import factories
from .fixtures import (
    AUTHORIZED_CIVIL_NUMBER,
    NONEXISTENT_CIVIL_NUMBER,
    UNAUTHORIZED_CIVIL_NUMBER,
    get_estonian_ariregister_empty_response,
    get_estonian_ariregister_success_response,
)


class EstonianAriregisterAPITest(APITestCase):
    """Tests for the checklist-based onboarding flow with Estonia."""

    def setUp(self):
        self.user = structure_factories.UserFactory(
            civil_number=AUTHORIZED_CIVIL_NUMBER
        )
        self.client.force_authenticate(user=self.user)
        self.legal_person_identifier = "70000310"

        # Create Estonia onboarding checklist
        self.checklist = self._create_estonia_checklist()

        # Create country configuration
        self.country_config = (
            models.OnboardingCountryChecklistConfiguration.objects.create(
                country="EE",
                checklist=self.checklist,
                is_active=True,
            )
        )

    def _create_estonia_checklist(self):
        """Create a basic Estonia onboarding checklist with optional supplemental questions."""
        checklist = checklist_factories.ChecklistFactory(
            name="Estonia SME Onboarding",
            description="Onboarding form for Estonian companies",
            checklist_type=checklist_enums.ChecklistTypes.CUSTOMER_ONBOARDING,
        )

        # Question 1: Company Email (optional customer field)
        q1 = checklist_factories.QuestionFactory(
            checklist=checklist,
            description="Company Email Address",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
            required=False,
            order=1,
        )
        models.OnboardingQuestionMetadata.objects.create(
            question=q1,
            maps_to_customer_field="email",
        )

        # Question 2: VAT Code (optional customer field)
        q2 = checklist_factories.QuestionFactory(
            checklist=checklist,
            description="VAT Registration Number (KMKR)",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
            required=False,
            order=2,
        )
        models.OnboardingQuestionMetadata.objects.create(
            question=q2,
            maps_to_customer_field="vat_code",
        )

        return checklist

    def _start_verification(
        self, country="EE", legal_person_identifier=None, legal_name=None
    ):
        """Start a new verification by calling start_verification action."""
        url = factories.OnboardingVerificationFactory.get_list_url(
            action="start_verification"
        )
        data = {"country": country}
        if legal_person_identifier:
            data["legal_person_identifier"] = legal_person_identifier
        if legal_name:
            data["legal_name"] = legal_name
        return self.client.post(url, data, format="json")

    def _get_verification_url(self, verification_uuid, action):
        """Helper to construct verification action URL from UUID string."""
        url = "http://testserver" + reverse(
            "onboarding-verification-detail", kwargs={"uuid": verification_uuid}
        )
        return url + action + "/"

    def _submit_answers(self, verification_uuid, answers):
        url = self._get_verification_url(verification_uuid, "submit_answers")
        return self.client.post(url, answers, format="json")

    def _run_validation(self, verification_uuid):
        url = self._get_verification_url(verification_uuid, "run_validation")
        return self.client.post(url, format="json")

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
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    def test_missing_username_raises_error(self):
        response = self._start_verification(
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification_uuid = response.data["uuid"]

        response = self._run_validation(verification_uuid)
        verification = response.data

        self._assert_verification_status(
            verification, enums.VerificationStatus.ESCALATED, "CONFIGURATION_ERROR"
        )
        self.assertIn("not configured", verification["error_traceback"])

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="",
        ONBOARDING_ARIREGISTER_PASSWORD="",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    def test_complete_flow_with_checklist_success(self):
        """Test the complete onboarding flow: start with required fields -> optionally submit checklist answers -> validate."""
        # Step 1: Start verification with required fields
        response = self._start_verification(
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company OÜ",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification_uuid = response.data["uuid"]
        self.assertEqual(response.data["status"], "pending")
        self.assertEqual(response.data["country"], "EE")
        self.assertEqual(
            response.data["legal_person_identifier"], self.legal_person_identifier
        )
        self.assertEqual(response.data["legal_name"], "Test Company OÜ")

        # Step 2: Optionally get checklist and submit supplemental data (email, VAT)
        checklist_url = self._get_verification_url(verification_uuid, "checklist")
        response = self.client.get(checklist_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        questions = response.data["questions"]
        self.assertEqual(len(questions), 2)

        # Step 3: Submit optional supplemental answers
        answers = [
            {
                "question_uuid": questions[0]["uuid"],
                "answer_data": "info@testcompany.ee",
            },
            {
                "question_uuid": questions[1]["uuid"],
                "answer_data": "EE123456789",
            },
        ]
        response = self._submit_answers(verification_uuid, answers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Step 4: Run validation (should fail due to missing config)
        response = self._run_validation(verification_uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        verification = response.data

        self._assert_verification_status(
            verification, enums.VerificationStatus.ESCALATED, "CONFIGURATION_ERROR"
        )
        self.assertIn("not configured", verification["error_traceback"])

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_user_has_authority_to_represent_company(self, mock_post):
        """Test successful validation when user is authorized representative."""
        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

        # Step 1: Start verification with required fields
        response = self._start_verification(
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company OÜ",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification_uuid = response.data["uuid"]

        # Step 2: Run validation directly (no checklist needed for basic validation)
        response = self._run_validation(verification_uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
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
        """Test validation fails when user is not a company representative."""
        different_user = structure_factories.UserFactory(
            civil_number=NONEXISTENT_CIVIL_NUMBER
        )
        self.client.force_authenticate(user=different_user)

        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

        response = self._start_verification(
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company OÜ",
        )
        verification_uuid = response.data["uuid"]

        response = self._run_validation(verification_uuid)
        verification = response.data

        self._assert_verification_status(
            verification, enums.VerificationStatus.ESCALATED, "NOT_AUTHORIZED"
        )
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

        response = self._start_verification(
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company OÜ",
        )
        verification_uuid = response.data["uuid"]

        response = self._run_validation(verification_uuid)
        verification = response.data

        self._assert_verification_status(
            verification, enums.VerificationStatus.ESCALATED, "NOT_AUTHORIZED"
        )
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
        """Test validation handles API exceptions gracefully."""
        mock_post.side_effect = requests.RequestException("Connection failed")

        response = self._start_verification(
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company OÜ",
        )
        verification_uuid = response.data["uuid"]

        response = self._run_validation(verification_uuid)
        verification = response.data

        self._assert_verification_status(
            verification, enums.VerificationStatus.ESCALATED, "API_ERROR"
        )
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
        """Test validation fails when company is not found."""
        self._mock_ariregister_response(
            mock_post, get_estonian_ariregister_empty_response()
        )

        response = self._start_verification(
            legal_person_identifier="99999999", legal_name="Nonexistent Company"
        )
        verification_uuid = response.data["uuid"]

        response = self._run_validation(verification_uuid)
        verification = response.data

        self._assert_verification_status(
            verification, enums.VerificationStatus.ESCALATED, "COMPANY_NOT_FOUND"
        )
        self.assertEqual(verification["validation_method"], "ariregister")
        self.assertIn("not found", verification["error_traceback"])

    def test_create_customer_from_verified_validation(self):
        """Test creating a customer from a verified onboarding verification."""
        # Create a verified verification with checklist completion
        verification = factories.OnboardingVerificationFactory(
            user=self.user,
            country="EE",
            status="verified",
            legal_person_identifier="12345678",
            legal_name="Test Company Ltd",
            validation_method=enums.ValidationMethod.ARIREGISTER,
            verified_company_data={
                "name": "Test Company Ltd",
                "legal_person_identifier": "12345678",
                "status": "Active",
                "registry": "Estonian Business Register",
            },
        )

        # Create checklist completion for this verification
        completion = verification.get_or_create_checklist_completion()
        self.assertIsNotNone(completion)

        url = factories.OnboardingVerificationFactory.get_url(
            verification, action="create_customer"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("uuid", response.data)
        self.assertIn("name", response.data)

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_create_customer_blocked_when_required_checklist_incomplete(
        self, mock_post
    ):
        """Test that customer creation is blocked when required checklist fields are not completed."""
        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

        # Add a required question to the checklist
        q_purpose = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="What is your intended use? (Required)",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
            required=True,
            order=3,
        )
        models.OnboardingQuestionMetadata.objects.create(
            question=q_purpose,
            intent_field="intended_use",
        )

        response = self._start_verification(
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company OÜ",
        )
        verification_uuid = response.data["uuid"]

        checklist_url = self._get_verification_url(verification_uuid, "checklist")
        response = self.client.get(checklist_url)
        questions = response.data["questions"]

        answers = [
            {
                "question_uuid": questions[0]["uuid"],
                "answer_data": "info@testcompany.ee",
            },
            {
                "question_uuid": questions[1]["uuid"],
                "answer_data": "EE123456789",
            },
            # Missing required q_purpose
        ]
        response = self._submit_answers(verification_uuid, answers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Run validation - should succeed and set status to verified
        response = self._run_validation(verification_uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        verification = response.data
        self._assert_verification_status(verification, "verified")

        # create customer should FAIL because checklist is incomplete
        create_customer_url = self._get_verification_url(
            verification_uuid, "create_customer"
        )
        response = self.client.post(create_customer_url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("checklist", str(response.data).lower())
        self.assertIn("required", str(response.data).lower())

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_create_customer_succeeds_when_required_checklist_completed(
        self, mock_post
    ):
        """Test that customer creation succeeds when required checklist fields are completed."""
        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

        q_purpose = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="What is your intended use? (Required)",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
            required=True,
            order=3,
        )
        models.OnboardingQuestionMetadata.objects.create(
            question=q_purpose,
            intent_field="intended_use",
        )

        response = self._start_verification(
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company OÜ",
        )
        verification_uuid = response.data["uuid"]

        checklist_url = self._get_verification_url(verification_uuid, "checklist")
        response = self.client.get(checklist_url)
        questions = response.data["questions"]

        intent_question = None
        for q in questions:
            if "intended use" in q["description"].lower():
                intent_question = q
                break

        answers = [
            {
                "question_uuid": questions[0]["uuid"],
                "answer_data": "info@testcompany.ee",
            },
            {
                "question_uuid": questions[1]["uuid"],
                "answer_data": "EE123456789",
            },
            {
                "question_uuid": intent_question["uuid"],
                "answer_data": "Research and Development",
            },
        ]
        response = self._submit_answers(verification_uuid, answers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self._run_validation(verification_uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        verification = response.data
        self._assert_verification_status(verification, "verified")

        create_customer_url = self._get_verification_url(
            verification_uuid, "create_customer"
        )
        response = self.client.post(create_customer_url)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("uuid", response.data)
        self.assertIn("name", response.data)

    def test_no_backend_available_for_unsupported_country(self):
        """Test that unsupported countries create verification with escalated status for manual review."""
        # Create checklist for unsupported country (US doesn't have auto-validation backend)
        unsupported_checklist = self._create_estonia_checklist()
        models.OnboardingCountryChecklistConfiguration.objects.create(
            country="US",
            checklist=unsupported_checklist,
            is_active=True,
        )

        # Start verification for unsupported country with required fields - should succeed
        response = self._start_verification(
            country="US",
            legal_person_identifier="12345678",
            legal_name="Test Company Inc",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification_uuid = response.data["uuid"]
        self.assertEqual(response.data["country"], "US")
        self.assertEqual(response.data["status"], "pending")

        # Run validation - should escalate because no backend available for US
        response = self._run_validation(verification_uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        verification = response.data

        self._assert_verification_status(
            verification, enums.VerificationStatus.ESCALATED, "NO_BACKEND_AVAILABLE"
        )
        self.assertIn(
            "No validation backend available", verification["error_traceback"]
        )
        self.assertIn("US", verification["error_traceback"])

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_no_checklist_configured_for_country(self, mock_post):
        """Test that starting verification and validation work even when no checklist is configured (checklist is optional)."""
        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

        # Delete checklist configuration
        models.OnboardingCountryChecklistConfiguration.objects.filter(
            country="EE"
        ).delete()

        # Should succeed even without checklist - checklist is now optional
        response = self._start_verification(
            country="EE",
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company OÜ",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["country"], "EE")
        self.assertEqual(
            response.data["legal_person_identifier"], self.legal_person_identifier
        )

        # Can proceed directly to validation without checklist
        verification_uuid = response.data["uuid"]

        # Run validation - should succeed even without checklist
        response = self._run_validation(verification_uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        verification = response.data

        # Verify validation succeeded
        self._assert_verification_status(verification, "verified")
        self.assertEqual(verification["validation_method"], "ariregister")
        self.assertEqual(verification["verified_user_roles"], ["ASES"])

    def test_research_group_without_legal_person_identifier(self):
        """Test that research groups without legal_person_identifier can still create verification and get escalated status."""
        # Start verification without legal_person_identifier (e.g., research group)
        response = self._start_verification(
            country="EE", legal_name="University Research Group"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verification_uuid = response.data["uuid"]
        self.assertEqual(response.data["legal_name"], "University Research Group")
        self.assertEqual(response.data["legal_person_identifier"], "")

        # Run validation - should fail and escalate since no legal_person_identifier
        response = self._run_validation(verification_uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        verification = response.data

        self._assert_verification_status(
            verification, enums.VerificationStatus.ESCALATED, "NO_BACKEND_AVAILABLE"
        )

    @override_config(
        ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
        ONBOARDING_ARIREGISTER_USERNAME="username",
        ONBOARDING_ARIREGISTER_PASSWORD="password",
        ONBOARDING_ARIREGISTER_TIMEOUT=30,
    )
    @patch("waldur_core.onboarding.backends.estonia.requests.post")
    def test_required_intent_field_with_options(self, mock_post):
        """
        This tests that intent fields with options (education, commercial, etc.)
        work correctly and the data is stored in onboarding_metadata.
        """
        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

        # Add intent field as multi-select with options
        q3 = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="What is your intended use of the platform?",
            question_type=checklist_enums.QuestionTypes.MULTI_SELECT,
            required=True,
            order=3,
        )
        models.OnboardingQuestionMetadata.objects.create(
            question=q3,
            intent_field="intent",
        )

        # Add options
        opt1 = checklist_factories.QuestionOptionFactory(
            question=q3,
            label="Education",
            order=1,
        )
        checklist_factories.QuestionOptionFactory(
            question=q3,
            label="Commercial",
            order=2,
        )
        opt3 = checklist_factories.QuestionOptionFactory(
            question=q3,
            label="Research & Development",
            order=3,
        )

        response = self._start_verification(
            legal_person_identifier=self.legal_person_identifier,
            legal_name="Test Company OÜ",
        )
        verification_uuid = response.data["uuid"]

        checklist_url = self._get_verification_url(verification_uuid, "checklist")
        response = self.client.get(checklist_url)
        questions = response.data["questions"]
        self.assertEqual(len(questions), 3)  # 2 setup + 1 intent

        # Find the intent question
        intent_question = None
        for q in questions:
            if q["description"] == "What is your intended use of the platform?":
                intent_question = q
                break

        self.assertIsNotNone(intent_question, "Intent question should be visible")
        self.assertEqual(intent_question["question_type"], "multi_select")
        self.assertEqual(len(intent_question["question_options"]), 3)

        answers = [
            {
                "question_uuid": questions[0]["uuid"],
                "answer_data": "info@testcompany.ee",
            },
            {
                "question_uuid": questions[1]["uuid"],
                "answer_data": "EE123456789",
            },
            {
                "question_uuid": intent_question["uuid"],
                "answer_data": [opt1.uuid, opt3.uuid],
            },
        ]

        response = self._submit_answers(verification_uuid, answers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self._run_validation(verification_uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        verification = response.data

        self._assert_verification_status(verification, "verified")

        self.assertIn("intent", verification["onboarding_metadata"])
        self.assertEqual(
            set(verification["onboarding_metadata"]["intent"]),
            {str(opt1.uuid), str(opt3.uuid)},
        )
        self.assertNotIn("intent", verification["verified_company_data"])


class EstonianAriregisterBackendTest(TestCase):
    """Direct backend unit tests - comprehensive testing coverage."""

    def setUp(self):
        self.backend = EstonianAriregisterBackend()
        self.user = structure_factories.UserFactory(
            civil_number=AUTHORIZED_CIVIL_NUMBER
        )
        self.legal_person_identifier = "70000310"

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
        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

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
        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

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
        self._mock_ariregister_response(
            mock_post,
            get_estonian_ariregister_success_response(self.legal_person_identifier),
        )

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
        self._mock_ariregister_response(
            mock_post, get_estonian_ariregister_empty_response()
        )

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
