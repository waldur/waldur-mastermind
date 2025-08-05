from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist import models
from waldur_core.checklist.tests import factories, fixtures
from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import enums


@ddt
class QuestionAdminGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.url = factories.QuestionFactory.get_admin_list_url()

    @data("staff")
    def test_user_can_list_questions(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data("owner")
    def test_user_cannot_list_questions(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class QuestionAdminCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.url = factories.QuestionFactory.get_admin_list_url()
        self.checklist = factories.ChecklistFactory()

    def _get_payload(self):
        return {
            "description": "test question",
            "checklist": factories.ChecklistFactory.get_admin_url(self.checklist),
            "question_type": enums.QuestionTypes.TEXT_INPUT,
        }

    @data("staff")
    def test_user_can_create_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Question.objects.filter(description="test question").exists()
        )

    @data("owner")
    def test_user_cannot_create_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_operator_and_review_answer_value_are_required(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        payload = self._get_payload()
        payload["operator"] = enums.OPERATORS[2][0]
        payload["review_answer_value"] = ["answer"]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        payload = self._get_payload()
        payload["operator"] = enums.OPERATORS[2][0]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Both", response.json()["non_field_errors"][0])

    def test_validate_operator(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        payload = self._get_payload()
        payload["operator"] = enums.OPERATORS[3][0]
        payload["review_answer_value"] = ["answer"]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Operator", response.json()["non_field_errors"][0])

    def test_validate_review_answer_value(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        payload = self._get_payload()
        payload["operator"] = enums.OPERATORS[2][0]
        payload["review_answer_value"] = "answer"
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Review answer value", response.json()["non_field_errors"][0])


@ddt
class QuestionAdminUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question = factories.QuestionFactory()
        self.url = factories.QuestionFactory.get_admin_url(self.question)

    def _get_payload(self):
        return {
            "description": "updated question",
        }

    @data("staff")
    def test_user_can_update_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            models.Question.objects.filter(description="updated question").exists()
        )

    @data("owner")
    def test_user_cannot_update_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.Question.objects.filter(description="updated question").exists()
        )


@ddt
class QuestionAdminDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question = factories.QuestionFactory()
        self.url = factories.QuestionFactory.get_admin_url(self.question)

    @data("staff")
    def test_user_can_delete_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Question.objects.filter(pk=self.question.pk).exists())

    @data("owner")
    def test_user_cannot_delete_question(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Question.objects.filter(pk=self.question.pk).exists())
