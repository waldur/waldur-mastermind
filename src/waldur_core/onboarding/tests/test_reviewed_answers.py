from ddt import data, ddt
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.onboarding import enums
from waldur_core.structure.tests import factories as structure_factories

from . import factories


@ddt
class ReviewedVerificationAnswersTest(APITestCase):
    """Once staff approve or reject a justification, the verification's owner
    can no longer submit its checklist answers."""

    def setUp(self):
        self.owner = structure_factories.UserFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA,
        )
        self.question = checklist_factories.QuestionFactory(
            checklist=checklist,
            description="VAT code",
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
            order=1,
        )
        self.verification = factories.OnboardingVerificationFactory(
            user=self.owner,
            status=enums.VerificationStatus.ESCALATED,
            validation_method="",
        )
        self.url = factories.OnboardingVerificationFactory.get_url(
            self.verification, action="submit_answers"
        )

    def justify(self, decision=enums.ReviewDecision.PENDING):
        return factories.OnboardingJustificationFactory(
            verification=self.verification,
            user=self.owner,
            validation_decision=decision,
        )

    def submit(self, user, answer):
        self.client.force_authenticate(user)
        return self.client.post(
            self.url,
            [{"question_uuid": str(self.question.uuid), "answer_data": answer}],
            format="json",
        )

    def stored_answer(self):
        answer = checklist_models.Answer.objects.filter(question=self.question).first()
        return answer and answer.answer_data

    @data(enums.ReviewDecision.APPROVED, enums.ReviewDecision.REJECTED)
    def test_owner_can_not_change_answers_after_review(self, decision):
        self.assertEqual(
            self.submit(self.owner, "EE111").status_code, status.HTTP_200_OK
        )
        self.justify(decision)

        response = self.submit(self.owner, "EE222")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.stored_answer(), "EE111")

    def test_owner_can_change_answers_without_a_justification(self):
        self.assertEqual(
            self.submit(self.owner, "EE111").status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self.submit(self.owner, "EE112").status_code, status.HTTP_200_OK
        )
        self.assertEqual(self.stored_answer(), "EE112")

    def test_answers_stay_locked_after_rejection_and_a_new_justification(self):
        self.submit(self.owner, "EE111")
        self.justify(enums.ReviewDecision.REJECTED)
        self.justify()

        response = self.submit(self.owner, "EE222")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.stored_answer(), "EE111")

    def test_owner_can_not_clear_answers_after_review(self):
        self.submit(self.owner, "EE111")
        self.justify(enums.ReviewDecision.APPROVED)

        response = self.submit(self.owner, None)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.stored_answer(), "EE111")

    def test_owner_can_change_answers_while_the_justification_awaits_review(self):
        self.justify()

        response = self.submit(self.owner, "EE333")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.stored_answer(), "EE333")

    def test_staff_are_not_refused_after_review(self):
        self.justify(enums.ReviewDecision.APPROVED)

        response = self.submit(self.staff, "EE444")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
