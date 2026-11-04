from ddt import data, ddt
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.onboarding import enums
from waldur_core.onboarding.models import (
    OnboardingJustification,
    OnboardingJustificationDocumentation,
    OnboardingVerification,
)
from waldur_core.structure.tests import factories as structure_factories

from . import factories


@ddt
class ReviewedJustificationTest(APITestCase):
    """Once staff approve or reject a justification, its owner can not change it."""

    def setUp(self):
        self.owner = structure_factories.UserFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.verification = factories.OnboardingVerificationFactory(
            user=self.owner, status=enums.VerificationStatus.ESCALATED
        )
        self.justification = factories.OnboardingJustificationFactory(
            verification=self.verification,
            user=self.owner,
            user_justification="Original justification.",
        )

    def review(self, decision):
        self.justification.validation_decision = decision
        self.justification.save(update_fields=["validation_decision"])

    def url(self, action=None):
        return factories.OnboardingJustificationFactory.get_url(
            self.justification, action=action
        )

    def patch(self):
        return self.client.patch(
            self.url(), {"user_justification": "Changed."}, format="json"
        )

    def put(self):
        return self.client.put(
            self.url(), {"user_justification": "Changed."}, format="json"
        )

    def attach(self):
        return self.client.post(
            self.url("attach_document"),
            {"file": SimpleUploadedFile("proof.pdf", b"%PDF-1.4", "application/pdf")},
            format="multipart",
        )

    def delete(self):
        return self.client.delete(self.url())

    @data(enums.ReviewDecision.APPROVED, enums.ReviewDecision.REJECTED)
    def test_owner_can_not_change_a_reviewed_justification(self, decision):
        self.review(decision)
        self.client.force_authenticate(self.owner)

        for request in (self.patch, self.put, self.attach, self.delete):
            with self.subTest(request=request.__name__):
                response = request()
                self.assertEqual(
                    response.status_code, status.HTTP_403_FORBIDDEN, response.data
                )

        self.justification.refresh_from_db()
        self.assertEqual(
            self.justification.user_justification, "Original justification."
        )
        self.assertFalse(
            OnboardingJustificationDocumentation.objects.filter(
                justification=self.justification
            ).exists()
        )

    def test_owner_can_change_a_justification_awaiting_review(self):
        self.client.force_authenticate(self.owner)

        self.assertEqual(self.patch().status_code, status.HTTP_200_OK)
        self.assertEqual(self.put().status_code, status.HTTP_200_OK)
        self.assertEqual(self.attach().status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.delete().status_code, status.HTTP_204_NO_CONTENT)

    @data(enums.ReviewDecision.APPROVED, enums.ReviewDecision.REJECTED)
    def test_staff_can_still_change_a_reviewed_justification(self, decision):
        self.review(decision)
        self.client.force_authenticate(self.staff)

        self.assertEqual(self.patch().status_code, status.HTTP_200_OK)
        self.assertEqual(self.attach().status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.delete().status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            OnboardingJustification.objects.filter(pk=self.justification.pk).exists()
        )

    def test_owner_can_still_read_a_reviewed_justification(self):
        self.review(enums.ReviewDecision.APPROVED)
        self.client.force_authenticate(self.owner)

        response = self.client.get(self.url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def delete_verification(self):
        return self.client.delete(
            factories.OnboardingVerificationFactory.get_url(self.verification)
        )

    @data(enums.ReviewDecision.APPROVED, enums.ReviewDecision.REJECTED)
    def test_owner_can_not_delete_the_verification_of_a_reviewed_justification(
        self, decision
    ):
        # Deleting the verification would delete the justification with it.
        self.review(decision)
        self.client.force_authenticate(self.owner)

        response = self.delete_verification()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            OnboardingJustification.objects.filter(pk=self.justification.pk).exists()
        )

    def test_owner_can_delete_a_verification_awaiting_review(self):
        self.client.force_authenticate(self.owner)

        response = self.delete_verification()

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            OnboardingVerification.objects.filter(pk=self.verification.pk).exists()
        )

    def test_staff_can_delete_the_verification_of_a_reviewed_justification(self):
        self.review(enums.ReviewDecision.APPROVED)
        self.client.force_authenticate(self.staff)

        response = self.delete_verification()

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
