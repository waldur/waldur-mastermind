from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests.helpers import override_marketplace_settings

from . import factories


@ddt
class CategoryGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.category = factories.CategoryFactory()

    @data('staff', 'owner', 'user', 'customer_support', 'admin', 'manager')
    def test_category_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_category_should_be_invisible_to_unauthenticated_users(self):
        url = factories.CategoryFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_anonymous_user_can_see_category_list(self):
        url = factories.CategoryFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_anonymous_user_can_see_category_item(self):
        url = factories.CategoryFactory.get_url(self.category)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@ddt
class CategoryCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    @data('staff',)
    def test_authorized_user_can_create_category(self, user):
        response = self.create_category(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Category.objects.filter(title='category').exists())

    @data('owner', 'user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_create_category(self, user):
        response = self.create_category(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_category(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryFactory.get_list_url()

        payload = {
            'title': 'category',
        }

        return self.client.post(url, payload)


@ddt
class CategoryUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    @data('staff',)
    def test_authorized_user_can_update_category(self, user):
        response, category = self.update_category(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(category.title, 'new_category')
        self.assertTrue(models.Category.objects.filter(title='new_category').exists())

    @data('owner', 'user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_category(self, user):
        response, category = self.update_category(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_category(self, user):
        category = factories.CategoryFactory()
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryFactory.get_url(category)

        response = self.client.patch(url, {'title': 'new_category'})
        category.refresh_from_db()

        return response, category


@ddt
class CategoryDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.category = factories.CategoryFactory(title='category')

    @data('staff',)
    def test_authorized_user_can_delete_category(self, user):
        response = self.delete_category(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(models.Category.objects.filter(title='category').exists())

    @data('owner', 'user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_delete_category(self, user):
        response = self.delete_category(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Category.objects.filter(title='category').exists())

    def delete_category(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryFactory.get_url(self.category)
        response = self.client.delete(url)
        return response
