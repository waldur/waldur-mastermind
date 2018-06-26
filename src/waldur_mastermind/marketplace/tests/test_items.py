from __future__ import unicode_literals

from ddt import data, ddt
from rest_framework import status

from waldur_core.structure.tests import fixtures

from . import factories, utils
from .. import models


@ddt
class ItemGetTest(utils.PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)
        self.item = factories.ItemFactory(order=self.order)

    @data('staff', 'owner', 'admin', 'manager')
    def test_items_should_be_visible_to_colleagues_and_staff(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ItemFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data('user')
    def test_items_should_be_invisible_to_other_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ItemFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_items_should_be_invisible_to_unauthenticated_users(self):
        url = factories.ItemFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class ItemCreateTest(utils.PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)
        self.offering = factories.OfferingFactory()

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_can_create_item_with_relation_project(self, user):
        response = self.create_item(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Item.objects.filter(order=self.order).exists())

    @data('user')
    def test_user_can_not_create_item_with_not_relation_project(self, user):
        response = self.create_item(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthorized_user_can_not_create_item(self):
        url = factories.ItemFactory.get_list_url()
        payload = {
            'offering': factories.OfferingFactory.get_url(self.offering),
            'order': factories.OrderFactory.get_url(self.order)
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_can_not_create_item_if_order_state_is_not_draft(self, user):
        self.order.state = models.Order.States.DONE
        self.order.save()
        response = self.create_item(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.Item.objects.filter(order=self.order).exists())

    def create_item(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ItemFactory.get_list_url()
        payload = {
            'offering': factories.OfferingFactory.get_url(self.offering),
            'order': factories.OrderFactory.get_url(self.order)
        }
        return self.client.post(url, payload)


@ddt
class ItemUpdateTest(utils.PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)
        self.item = factories.ItemFactory(order=self.order)

    @data('staff', 'owner')
    def test_staff_and_owner_can_update_item(self, user):
        response = self.update_item(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_other_user_can_not_update_item(self, user):
        response = self.update_item(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data('admin', 'manager')
    def test_admin_and_manager_can_not_update_item(self, user):
        response = self.update_item(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('staff', 'owner')
    def test_can_not_update_item_if_order_state_is_not_draft(self, user):
        self.order.state = models.Order.States.DONE
        self.order.save()
        response = self.update_item(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def update_item(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ItemFactory.get_url(self.item)
        response = self.client.patch(url, {
            'attributes': {'test': 1}
        })
        self.item.refresh_from_db()
        return response


@ddt
class ItemDeleteTest(utils.PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)
        self.item = factories.ItemFactory(order=self.order)

    @data('staff', 'owner', 'admin', 'manager')
    def test_authorized_user_can_delete_item(self, user):
        response = self.delete_item(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT, response.data)
        self.assertFalse(models.Item.objects.filter(order=self.order).exists())

    @data('user')
    def test_other_user_can_not_delete_item(self, user):
        response = self.delete_item(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(models.Item.objects.filter(order=self.order).exists())

    def test_unauthorized_user_can_not_delete_item(self):
        url = factories.ItemFactory.get_url(self.item)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data('staff', 'owner')
    def test_can_not_update_item_if_order_state_is_not_draft(self, user):
        self.order.state = models.Order.States.DONE
        self.order.save()
        response = self.delete_item(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def delete_item(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ItemFactory.get_url(self.item)
        response = self.client.delete(url)
        return response
