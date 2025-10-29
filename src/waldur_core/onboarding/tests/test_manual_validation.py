import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.onboarding import enums
from waldur_core.onboarding.models import (
    OnboardingJustification,
    OnboardingJustificationDocumentation,
)
from waldur_core.structure.tests import factories as structure_factories

from . import factories


class CreateJustificationTest(APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        # An escalated verification (automatic validation failed)
        self.escalated_verification = factories.OnboardingVerificationFactory(
            user=self.user,
            status=enums.VerificationStatus.ESCALATED,
            country="EE",
            legal_person_identifier="12345678",
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

    def test_create_justification_for_verified_verification_fails(self):
        verified_verification = factories.OnboardingVerificationFactory(
            user=self.user,
            status=enums.VerificationStatus.VERIFIED,
        )

        response = self._create_justification(
            verified_verification.uuid,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("verification_uuid", response.data)

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
