from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.structure.tests import factories as structure_factories


class QuestionOnboardingMappingFilterTest(APITestCase):
    """Test has_onboarding_mapping filter for Question queryset."""

    def setUp(self):
        from waldur_core.onboarding.models import OnboardingQuestionMetadata

        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff_user)

        self.checklist = checklist_factories.ChecklistFactory(
            name="Test Checklist",
            checklist_type=checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA,
        )

        self.question_with_mapping = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="Question with mapping",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
        )
        OnboardingQuestionMetadata.objects.create(
            question=self.question_with_mapping,
            maps_to_customer_field="email",
        )

        self.question_without_mapping = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            description="Question without mapping",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
        )

    def test_filter_questions_with_onboarding_mapping(self):
        """Test filtering questions that have onboarding metadata."""
        url = reverse("checklists-admin-questions-list")
        response = self.client.get(url, {"has_onboarding_mapping": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]

        # Should include question with mapping
        self.assertIn(str(self.question_with_mapping.uuid), uuids)
        # Should NOT include question without mapping
        self.assertNotIn(str(self.question_without_mapping.uuid), uuids)

    def test_filter_questions_without_onboarding_mapping(self):
        """Test filtering questions that don't have onboarding metadata."""
        url = reverse("checklists-admin-questions-list")
        response = self.client.get(url, {"has_onboarding_mapping": "false"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]

        # Should include question without mapping
        self.assertIn(str(self.question_without_mapping.uuid), uuids)
        # Should NOT include question with mapping
        self.assertNotIn(str(self.question_with_mapping.uuid), uuids)

    def test_no_filter_returns_all_questions(self):
        """Test that without filter, all questions are returned."""
        url = reverse("checklists-admin-questions-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]

        # Should include both questions
        self.assertIn(str(self.question_with_mapping.uuid), uuids)
        self.assertIn(str(self.question_without_mapping.uuid), uuids)
