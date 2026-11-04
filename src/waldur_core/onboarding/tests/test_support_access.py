from rest_framework import status, test

from waldur_core.onboarding import models
from waldur_core.structure.tests import factories as structure_factories

from . import factories


class SupportAccessTest(test.APITestCase):
    """Support users may read every onboarding record but change only their own."""

    def setUp(self):
        self.support = structure_factories.UserFactory(is_support=True)
        self.verification = factories.OnboardingVerificationFactory()
        self.justification = factories.OnboardingJustificationFactory(
            verification=self.verification
        )
        self.client.force_authenticate(user=self.support)

    def test_support_can_view_other_users_verification(self):
        response = self.client.get(
            factories.OnboardingVerificationFactory.get_url(self.verification)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_cannot_update_other_users_verification(self):
        response = self.client.patch(
            factories.OnboardingVerificationFactory.get_url(self.verification),
            {"legal_name": "Changed by support"},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.verification.refresh_from_db()
        self.assertNotEqual(self.verification.legal_name, "Changed by support")

    def test_support_cannot_delete_other_users_verification(self):
        response = self.client.delete(
            factories.OnboardingVerificationFactory.get_url(self.verification)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(
            models.OnboardingVerification.objects.filter(
                pk=self.verification.pk
            ).exists()
        )

    def test_support_cannot_delete_other_users_justification(self):
        response = self.client.delete(
            factories.OnboardingJustificationFactory.get_url(self.justification)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(
            models.OnboardingJustification.objects.filter(
                pk=self.justification.pk
            ).exists()
        )

    def test_support_cannot_act_on_other_users_verification(self):
        for action in ("submit_answers", "run_validation", "create_customer"):
            with self.subTest(action=action):
                response = self.client.post(
                    factories.OnboardingVerificationFactory.get_url(
                        self.verification, action
                    ),
                    {},
                )
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_support_cannot_attach_document_to_other_users_justification(self):
        response = self.client.post(
            factories.OnboardingJustificationFactory.get_url(
                self.justification, "attach_document"
            ),
            {},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_support_can_update_own_verification(self):
        own = factories.OnboardingVerificationFactory(user=self.support)
        response = self.client.patch(
            factories.OnboardingVerificationFactory.get_url(own),
            {"legal_name": "Changed by its owner"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        own.refresh_from_db()
        self.assertEqual(own.legal_name, "Changed by its owner")


class DisabledCreateRoutesTest(test.APITestCase):
    """Records are created by start_verification and create_justification.

    The generic creates set no user and failed for every caller, as did the
    checklist template, which this view has no parent object for.
    """

    def setUp(self):
        self.client.force_authenticate(user=structure_factories.UserFactory())

    def test_generic_verification_create_is_not_allowed(self):
        response = self.client.post(
            factories.OnboardingVerificationFactory.get_list_url(),
            {"country": "EE", "legal_person_identifier": "1", "legal_name": "X"},
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertFalse(models.OnboardingVerification.objects.exists())

    def test_generic_justification_create_is_not_allowed(self):
        verification = factories.OnboardingVerificationFactory()
        response = self.client.post(
            factories.OnboardingJustificationFactory.get_list_url(),
            {
                "verification": factories.OnboardingVerificationFactory.get_url(
                    verification
                ),
                "user_justification": "X",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertFalse(models.OnboardingJustification.objects.exists())

    def test_checklist_template_is_not_allowed(self):
        response = self.client.get(
            factories.OnboardingVerificationFactory.get_list_url("checklist-template")
            + "?parent_uuid=abc"
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
