from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests.helpers import override_marketplace_settings

from . import factories


@ddt
class CategoryGroupGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.group = factories.CategoryGroupFactory()
        self.group_url = factories.CategoryGroupFactory.get_url(self.group)
        self.list_url = factories.CategoryGroupFactory.get_list_url()

    @data('staff', 'owner', 'user', 'customer_support', 'admin', 'manager')
    def test_group_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_group_should_be_invisible_to_unauthenticated_users(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_anonymous_user_can_see_group_list(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


@ddt
class CategoryGroupCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    @data(
        'staff',
    )
    def test_authorized_user_can_create_group(self, user):
        response = self.create_group(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.CategoryGroup.objects.filter(title='group').exists())

    @data('owner', 'user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_create_group(self, user):
        response = self.create_group(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_group(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryGroupFactory.get_list_url()

        payload = {
            'title': 'group',
        }

        return self.client.post(url, payload)


@ddt
class CategoryGroupUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    @data(
        'staff',
    )
    def test_authorized_user_can_update_group(self, user):
        response, group = self.update_group(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(group.title, 'new_group')
        self.assertTrue(models.CategoryGroup.objects.filter(title='new_group').exists())

    @data('owner', 'user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_category(self, user):
        response, group = self.update_group(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_group(self, user):
        group = factories.CategoryGroupFactory()
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryGroupFactory.get_url(group)

        response = self.client.patch(url, {'title': 'new_group'})
        group.refresh_from_db()

        return response, group


@ddt
class CategoryGroupDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.group = factories.CategoryGroupFactory(title='group')

    @data(
        'staff',
    )
    def test_authorized_user_can_delete_group(self, user):
        response = self.delete_group(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(models.CategoryGroup.objects.filter(title='group').exists())

    @data('owner', 'user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_delete_group(self, user):
        response = self.delete_group(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.CategoryGroup.objects.filter(title='group').exists())

    def delete_group(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryGroupFactory.get_url(self.group)
        response = self.client.delete(url)
        return response
