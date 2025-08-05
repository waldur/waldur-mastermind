from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist import models
from waldur_core.checklist.tests import factories, fixtures


@ddt
class QuestionOptionAdminGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.url = factories.QuestionOptionFactory.get_admin_list_url()

    @data("staff")
    def test_user_can_list_options(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner")
    def test_user_cannot_list_options(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class QuestionOptionAdminCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question = self.fixture.question
        self.url = factories.QuestionOptionFactory.get_admin_list_url()

    def _get_payload(self):
        return {
            "question": factories.QuestionFactory.get_admin_url(self.question),
            "label": "option label",
            "order": 1,
        }

    @data("staff")
    def test_user_can_create_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.QuestionOption.objects.filter(label="option label").exists()
        )

    @data("owner")
    def test_user_cannot_create_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class QuestionOptionAdminUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question_option = self.fixture.question_option
        self.url = factories.QuestionOptionFactory.get_admin_url(self.question_option)

    def _get_payload(self):
        return {
            "label": "updated option",
        }

    @data("staff")
    def test_user_can_update_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            models.QuestionOption.objects.filter(label="updated option").exists()
        )

    @data("owner")
    def test_user_cannot_update_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.QuestionOption.objects.filter(label="updated option").exists()
        )


@ddt
class QuestionOptionAdminDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question_option = self.fixture.question_option
        self.url = factories.QuestionOptionFactory.get_admin_url(self.question_option)

    @data("staff")
    def test_user_can_delete_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.QuestionOption.objects.filter(pk=self.question_option.pk).exists()
        )

    @data("owner")
    def test_user_cannot_delete_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.QuestionOption.objects.filter(pk=self.question_option.pk).exists()
        )
