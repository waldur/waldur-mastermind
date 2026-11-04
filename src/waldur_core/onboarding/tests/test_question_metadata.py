from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.onboarding.models import OnboardingQuestionMetadata
from waldur_core.structure.tests import factories as structure_factories


def list_url():
    return "http://testserver" + reverse("onboarding-question-metadata-list")


def detail_url(metadata):
    return "http://testserver" + reverse(
        "onboarding-question-metadata-detail", kwargs={"uuid": metadata.uuid.hex}
    )


class QuestionMetadataAccessTest(APITestCase):
    """Only staff may change how checklist answers map to organization fields."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.user = structure_factories.UserFactory()
        self.metadata = OnboardingQuestionMetadata.objects.create(
            question=checklist_factories.QuestionFactory(),
            maps_to_customer_field="email",
        )
        self.new_question = checklist_factories.QuestionFactory()

    def create_payload(self):
        return {
            "question": checklist_factories.QuestionFactory.get_admin_url(
                self.new_question
            ),
            "maps_to_customer_field": "vat_code",
        }

    def update_payload(self):
        return {
            "question": checklist_factories.QuestionFactory.get_admin_url(
                self.metadata.question
            ),
            "maps_to_customer_field": "vat_code",
        }

    def assert_metadata_unchanged(self):
        self.metadata.refresh_from_db()
        self.assertEqual(self.metadata.maps_to_customer_field, "email")

    def assert_cannot_write(self, caller):
        self.client.force_authenticate(user=caller)
        with self.subTest(action="create"):
            response = self.client.post(list_url(), self.create_payload())
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertFalse(
                OnboardingQuestionMetadata.objects.filter(
                    question=self.new_question
                ).exists()
            )

        with self.subTest(action="update"):
            response = self.client.put(detail_url(self.metadata), self.update_payload())
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assert_metadata_unchanged()

        with self.subTest(action="partial_update"):
            response = self.client.patch(
                detail_url(self.metadata), {"maps_to_customer_field": "vat_code"}
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assert_metadata_unchanged()

        with self.subTest(action="destroy"):
            response = self.client.delete(detail_url(self.metadata))
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertTrue(
                OnboardingQuestionMetadata.objects.filter(pk=self.metadata.pk).exists()
            )

    def test_regular_user_cannot_write(self):
        self.assert_cannot_write(self.user)

    def test_support_cannot_write(self):
        self.assert_cannot_write(self.support)

    def test_signed_in_users_can_read(self):
        for caller in (self.user, self.support, self.staff):
            with self.subTest(caller=caller.username):
                self.client.force_authenticate(user=caller)
                response = self.client.get(list_url())
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(len(response.data), 1)
                response = self.client.get(detail_url(self.metadata))
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_can_create(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(list_url(), self.create_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            OnboardingQuestionMetadata.objects.get(
                question=self.new_question
            ).maps_to_customer_field,
            "vat_code",
        )

    def test_staff_can_update(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.put(detail_url(self.metadata), self.update_payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.metadata.refresh_from_db()
        self.assertEqual(self.metadata.maps_to_customer_field, "vat_code")

    def test_staff_can_partially_update(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            detail_url(self.metadata), {"intent_field": "intent"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.metadata.refresh_from_db()
        self.assertEqual(self.metadata.intent_field, "intent")

    def test_staff_can_delete(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.delete(detail_url(self.metadata))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            OnboardingQuestionMetadata.objects.filter(pk=self.metadata.pk).exists()
        )
