from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.onboarding import enums
from waldur_core.structure.tests import factories as structure_factories

from . import factories


class HiddenChecklistQuestionsTest(APITestCase):
    """A required question hidden by its dependencies does not block creating
    the organization."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA,
        )
        self.gate = checklist_factories.QuestionFactory(
            checklist=checklist,
            required=True,
            question_type=checklist_enums.QuestionTypes.BOOLEAN,
        )
        hidden = checklist_factories.QuestionFactory(checklist=checklist, required=True)
        checklist_factories.QuestionDependencyFactory(
            question=hidden,
            depends_on_question=self.gate,
            operator="equals",
            required_answer_value=True,
        )
        self.verification = factories.OnboardingVerificationFactory(
            user=self.user,
            status=enums.VerificationStatus.VERIFIED,
            country="EE",
            legal_person_identifier="12345678",
            validation_method=enums.ValidationMethod.ARIREGISTER,
        )
        self.completion = self.verification.get_or_create_checklist_completion(
            checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
        )

    def create_customer(self, gate_answer):
        checklist_models.Answer.objects.create(
            completion=self.completion,
            question=self.gate,
            user=self.user,
            answer_data=gate_answer,
        )
        self.client.force_authenticate(self.user)
        return self.client.post(
            factories.OnboardingVerificationFactory.get_url(
                self.verification, action="create_customer"
            )
        )

    def test_created_when_only_hidden_questions_are_unanswered(self):
        response = self.create_customer(False)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_refused_when_a_shown_question_is_unanswered(self):
        response = self.create_customer(True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
