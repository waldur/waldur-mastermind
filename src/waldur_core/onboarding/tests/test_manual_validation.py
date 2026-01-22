import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.onboarding import enums
from waldur_core.onboarding.models import (
    OnboardingJustification,
    OnboardingJustificationDocumentation,
    OnboardingQuestionMetadata,
    OnboardingVerification,
)
from waldur_core.structure.tests import factories as structure_factories

from . import factories


class CreateJustificationTest(APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        # Create an escalated verification using start_verification action
        start_url = factories.OnboardingVerificationFactory.get_list_url(
            action="start_verification"
        )
        verification_data = {
            "country": "EE",
            "legal_person_identifier": "12345678",
            "legal_name": "Test Company OÜ",
        }
        response = self.client.post(start_url, verification_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.escalated_verification = OnboardingVerification.objects.get(
            uuid=response.data["uuid"]
        )
        self.assertEqual(
            self.escalated_verification.status, enums.VerificationStatus.ESCALATED
        )

        self.url = factories.OnboardingJustificationFactory.get_list_url(
            action="create_justification"
        )

    def _create_justification(self, verification_uuid):
        data = {
            "verification_uuid": str(verification_uuid),
            "user_justification": "Pls validate",
        }
        return self.client.post(self.url, data, format="json")

    def test_create_justification_for_escalated_verification_success(self):
        response = self._create_justification(
            self.escalated_verification.uuid,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            OnboardingJustification.objects.filter(
                verification=self.escalated_verification
            ).count(),
            1,
        )

        justification = OnboardingJustification.objects.get(
            verification=self.escalated_verification
        )
        self.assertEqual(justification.user, self.user)
        self.assertEqual(
            justification.validation_decision, enums.ReviewDecision.PENDING
        )

    def test_create_justification_for_failed_verification_success(self):
        failed_verification = factories.OnboardingVerificationFactory(
            user=self.user,
            status=enums.VerificationStatus.FAILED,
        )

        response = self._create_justification(
            failed_verification.uuid,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_create_justification_for_verified_verification_success(self):
        verified_verification = factories.OnboardingVerificationFactory(
            user=self.user,
            status=enums.VerificationStatus.VERIFIED,
        )

        response = self._create_justification(
            verified_verification.uuid,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        verified_verification.refresh_from_db()
        self.assertEqual(
            verified_verification.status, enums.VerificationStatus.ESCALATED
        )
        self.assertIn("automatically verified", verified_verification.error_traceback)

    def test_create_justification_without_permission_fails(self):
        other_user = structure_factories.UserFactory()
        other_user_verification = factories.OnboardingVerificationFactory(
            user=other_user,
            status=enums.VerificationStatus.ESCALATED,
        )

        response = self._create_justification(
            other_user_verification.uuid,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("error", response.data)

    def test_create_justification_with_nonexistent_verification_fails(self):
        response = self._create_justification(
            uuid.uuid4().hex,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("verification_uuid", response.data)

    def test_attach_documents_to_justification(self):
        justification = factories.OnboardingJustificationFactory(
            verification=self.escalated_verification,
            user=self.user,
        )

        url = factories.OnboardingJustificationFactory.get_url(
            justification, action="attach_document"
        )

        # Attach first document
        file1 = SimpleUploadedFile(
            "authorization_letter.pdf",
            b"PDF content here",
            content_type="application/pdf",
        )
        response = self.client.post(url, {"file": file1}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            OnboardingJustificationDocumentation.objects.filter(
                justification=justification
            ).count(),
            1,
        )

        # Attach second document
        file2 = SimpleUploadedFile(
            "email_confirmation.pdf",
            b"Another PDF content",
            content_type="application/pdf",
        )
        response = self.client.post(url, {"file": file2}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            OnboardingJustificationDocumentation.objects.filter(
                justification=justification
            ).count(),
            2,
        )


class JustificationDecisionTest(APITestCase):
    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.regular_user = structure_factories.UserFactory()

        self.escalated_verification = factories.OnboardingVerificationFactory(
            user=self.regular_user,
            status=enums.VerificationStatus.ESCALATED,
            legal_person_identifier="12345678",
        )

        self.justification = factories.OnboardingJustificationFactory(
            verification=self.escalated_verification,
            user=self.regular_user,
            user_justification="I am authorized to represent this company.",
        )

    def test_staff_can_approve_justification(self):
        self.client.force_authenticate(user=self.staff_user)

        url = factories.OnboardingJustificationFactory.get_url(
            self.justification, action="approve"
        )
        data = {
            "staff_notes": "Good justification.",
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.justification.refresh_from_db()
        self.assertEqual(
            self.justification.validation_decision, enums.ReviewDecision.APPROVED
        )
        self.assertEqual(self.justification.validated_by, self.staff_user)
        self.assertIsNotNone(self.justification.validated_at)
        self.assertEqual(
            self.justification.staff_notes,
            data["staff_notes"],
        )

        self.escalated_verification.refresh_from_db()
        self.assertEqual(
            self.escalated_verification.status, enums.VerificationStatus.VERIFIED
        )

    def test_staff_can_reject_justification(self):
        self.client.force_authenticate(user=self.staff_user)

        url = factories.OnboardingJustificationFactory.get_url(
            self.justification, action="reject"
        )
        data = {
            "staff_notes": "Bad justification.",
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.justification.refresh_from_db()
        self.assertEqual(
            self.justification.validation_decision, enums.ReviewDecision.REJECTED
        )
        self.assertEqual(self.justification.validated_by, self.staff_user)
        self.assertIsNotNone(self.justification.validated_at)
        self.assertEqual(
            self.justification.staff_notes,
            data["staff_notes"],
        )

        self.escalated_verification.refresh_from_db()
        self.assertEqual(
            self.escalated_verification.status, enums.VerificationStatus.FAILED
        )

    def test_regular_user_cannot_approve_justification(self):
        self.client.force_authenticate(user=self.regular_user)

        url = factories.OnboardingJustificationFactory.get_url(
            self.justification, action="approve"
        )
        data = {
            "staff_notes": "I want to approve my own justification.",
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.justification.refresh_from_db()
        self.assertEqual(
            self.justification.validation_decision, enums.ReviewDecision.PENDING
        )

    def test_approve_justification_updates_related_verification(self):
        self.client.force_authenticate(user=self.staff_user)
        url = factories.OnboardingJustificationFactory.get_url(
            self.justification, action="approve"
        )
        response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cannot_approve_justification_if_customer_already_exists(self):
        existing_customer = structure_factories.CustomerFactory(
            registration_code=self.escalated_verification.legal_person_identifier,
            name="Existing Customer",
        )

        self.client.force_authenticate(user=self.staff_user)
        url = factories.OnboardingJustificationFactory.get_url(
            self.justification, action="approve"
        )
        data = {
            "staff_notes": "Trying to approve even though customer exists.",
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already exists", str(response.data))
        self.assertIn(existing_customer.name, str(response.data))

        self.justification.refresh_from_db()
        self.assertEqual(
            self.justification.validation_decision, enums.ReviewDecision.PENDING
        )

        self.escalated_verification.refresh_from_db()
        self.assertEqual(
            self.escalated_verification.status, enums.VerificationStatus.ESCALATED
        )

    def test_create_customer_blocked_when_required_checklist_incomplete(self):
        """
        Test that customer creation after justification approval is blocked when required checklist questions are not completed.
        This applies to manual validation (no validation_method).
        """
        # Create CUSTOMER checklist
        checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA,
        )

        # Add a required question to the CUSTOMER checklist
        required_question = checklist_factories.QuestionFactory(
            checklist=checklist,
            description="VAT code (Required)",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
            required=True,
            order=1,
        )
        OnboardingQuestionMetadata.objects.create(
            question=required_question,
            maps_to_customer_field="vat_code",
        )

        # Create escalated verification with manual validation (no validation_method set)
        verification = factories.OnboardingVerificationFactory(
            user=self.regular_user,
            status=enums.VerificationStatus.ESCALATED,
            legal_person_identifier="87654321",
            legal_name="Manual Validation Company OÜ",
            validation_method="",  # No automatic validation method
        )

        justification = factories.OnboardingJustificationFactory(
            verification=verification,
            user=self.regular_user,
            user_justification="Please validate our company for manual review.",
        )

        # Get the checklist to see required questions (customer checklist for manual validation)
        completion = verification.get_or_create_checklist_completion(
            checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA
        )
        self.assertIsNotNone(completion)

        # Verify checklist has questions but none are answered
        self.assertTrue(completion.checklist.questions.exists())
        self.assertFalse(completion.is_completed)

        self.client.force_authenticate(user=self.staff_user)
        url = factories.OnboardingJustificationFactory.get_url(
            justification, action="approve"
        )
        data = {
            "staff_notes": "Approve with incomplete checklist.",
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        create_customer_url = factories.OnboardingVerificationFactory.get_url(
            verification, action="create_customer"
        )
        response = self.client.post(create_customer_url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertIn("checklist", str(response.data).lower())
        self.assertIn("required", str(response.data).lower())

    def test_create_customer_succeeds_when_both_checklists_completed(self):
        """
        Test that customer creation succeeds when both customer and intent checklists
        are defined with required questions and user has submitted answers to all.
        """
        customer_checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA,
        )

        intent_checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA,
        )

        # Add a required question to the CUSTOMER checklist
        customer_question = checklist_factories.QuestionFactory(
            checklist=customer_checklist,
            description="VAT code (Required)",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
            required=True,
            order=1,
        )
        OnboardingQuestionMetadata.objects.create(
            question=customer_question,
            maps_to_customer_field="vat_code",
        )

        # Add a required question to the INTENT checklist
        intent_question = checklist_factories.QuestionFactory(
            checklist=intent_checklist,
            description="What is your business intent?",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
            required=True,
            order=1,
        )
        OnboardingQuestionMetadata.objects.create(
            question=intent_question,
            intent_field="business_intent",
        )

        # Create escalated verification with manual validation
        verification = factories.OnboardingVerificationFactory(
            user=self.regular_user,
            status=enums.VerificationStatus.ESCALATED,
            legal_person_identifier="11223344",
            legal_name="Complete Checklist Company OÜ",
            validation_method="",  # No automatic validation method
        )

        # Submit answers to both checklists
        # Answer customer checklist question
        customer_completion = verification.get_or_create_checklist_completion(
            checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA
        )
        submit_answers_url = factories.OnboardingVerificationFactory.get_url(
            verification, action="submit_answers"
        )

        self.client.force_authenticate(user=self.regular_user)
        customer_answer_data = [
            {
                "question_uuid": str(customer_question.uuid),
                "answer_data": "EE123456789",
            }
        ]
        response = self.client.post(
            submit_answers_url, customer_answer_data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Answer intent checklist question
        intent_completion = verification.get_or_create_checklist_completion(
            checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
        )
        intent_answer_data = [
            {
                "question_uuid": str(intent_question.uuid),
                "answer_data": "We want to expand our digital services",
            }
        ]
        response = self.client.post(
            submit_answers_url, intent_answer_data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify both checklists are completed
        customer_completion.refresh_from_db()
        intent_completion.refresh_from_db()
        self.assertTrue(customer_completion.is_completed)
        self.assertTrue(intent_completion.is_completed)

        # Create justification
        justification = factories.OnboardingJustificationFactory(
            verification=verification,
            user=self.regular_user,
            user_justification="All checklists completed, please validate.",
        )

        # Approve justification
        self.client.force_authenticate(user=self.staff_user)
        approve_url = factories.OnboardingJustificationFactory.get_url(
            justification, action="approve"
        )
        response = self.client.post(
            approve_url, {"staff_notes": "Approved"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify that verification is now VERIFIED
        verification.refresh_from_db()
        self.assertEqual(verification.status, enums.VerificationStatus.VERIFIED)

        # Now create customer should succeed
        create_customer_url = factories.OnboardingVerificationFactory.get_url(
            verification, action="create_customer"
        )
        response = self.client.post(create_customer_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify customer was created with correct data
        self.assertIn("uuid", response.data)
        self.assertEqual(response.data["registration_code"], "11223344")
        self.assertEqual(response.data["name"], "Complete Checklist Company OÜ")
        self.assertEqual(response.data["vat_code"], "EE123456789")
