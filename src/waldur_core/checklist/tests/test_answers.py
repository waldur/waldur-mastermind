from rest_framework import status, test

from waldur_core.checklist import enums, models
from waldur_core.checklist.tests import factories, fixtures


class AnswerSubmitTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.user = self.fixture.user
        self.checklist = self.fixture.checklist
        self.question = self.fixture.question
        self.url = (
            f"/api/marketplace-checklists/{self.checklist.uuid.hex}/answers/submit/"
        )

    def _get_payload(self, answer_data):
        return [
            {
                "question_uuid": str(self.question.uuid),
                "answer_data": answer_data,
            }
        ]

    def test_user_can_submit_valid_answer(self):
        user = self.fixture.user
        self.client.force_authenticate(user)

        payload = self._get_payload("valid text answer")
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.assertTrue(
            models.Answer.objects.filter(
                question=self.question, user=user, answer_data="valid text answer"
            ).exists()
        )

    def test_user_cannot_submit_invalid_answer(self):
        user = self.fixture.user
        self.client.force_authenticate(user)

        select_question = factories.QuestionFactory(
            checklist=self.checklist, question_type=enums.QuestionTypes.SINGLE_SELECT
        )

        payload = [
            {
                "question_uuid": str(select_question.uuid),
                "answer_data": "invalid string answer",
            }
        ]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        error_data = response.json()
        self.assertIn("Answer value", error_data[0]["non_field_errors"][0])
        self.assertIn("not valid", error_data[0]["non_field_errors"][0])

    def test_answer_requires_review_when_condition_met(self):
        user = self.fixture.user
        self.client.force_authenticate(user)

        question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.TEXT_INPUT,
            review_answer_value="trigger_review",
            operator="equals",
        )

        payload = [
            {
                "question_uuid": str(question.uuid),
                "answer_data": "trigger_review",
            }
        ]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        answer = models.Answer.objects.get(question=question, user=user)
        self.assertTrue(answer.requires_review)
