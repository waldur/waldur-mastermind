from __future__ import unicode_literals

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status

from waldur_core.structure.tests import fixtures, factories as structure_factories

from . import factories, utils
from .. import models, base


@ddt
class OrderGetTest(utils.PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)

    @data('staff', 'owner', 'admin', 'manager')
    def test_orders_should_be_visible_to_colleagues_and_staff(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data('user')
    def test_orders_should_be_invisible_to_other_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_items_should_be_invisible_to_unauthenticated_users(self):
        url = factories.OrderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class OrderCreateTest(utils.PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_can_create_order_with_relation_project(self, user):
        user = getattr(self.fixture, user)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    @data('user')
    def test_user_can_not_create_order_with_not_relation_project(self, user):
        user = getattr(self.fixture, user)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthorized_user_can_not_create_order(self):
        url = factories.OrderFactory.get_list_url()
        payload = {
            'project': structure_factories.ProjectFactory.get_url(self.project),
            'items': [
                factories.ItemFactory.get_url()
            ]
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def create_order(self, user):
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        payload = {
            'project': structure_factories.ProjectFactory.get_url(self.project),
            'items': [
                factories.ItemFactory.get_url()
            ]
        }
        return self.client.post(url, payload)


@ddt
class OrderUpdateTest(utils.PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)

    @data('staff', 'owner', 'admin', 'manager')
    def test_can_set_requested_for_approval_state(self, user):
        order_state = models.Order.States.REQUESTED_FOR_APPROVAL
        response = self.update_offering(user, 'set_state_requested_for_approval')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.state, order_state)
        self.assertTrue(models.Order.objects.filter(state=order_state).exists())

    @data('user')
    def test_not_can_set_requested_for_approval_state(self, user):
        response = self.update_offering(user, 'set_state_requested_for_approval')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data('staff', 'owner')
    def test_can_set_done_state(self, user):
        self.order.state = models.Order.States.EXECUTING
        self.order.save()
        order_state = models.Order.States.DONE
        response = self.update_offering(user, 'set_state_done')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.state, order_state)
        self.assertTrue(models.Order.objects.filter(state=order_state).exists())

    @freeze_time('2017-01-10 00:00:00')
    def test_approved_fields(self):
        self.order.state = models.Order.States.EXECUTING
        self.order.save()
        response = self.update_offering('staff', 'set_state_done')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.approved_at.strftime('%Y-%m-%d %H:%M:%S'), '2017-01-10 00:00:00')
        self.assertEqual(self.order.approved_by, self.fixture.staff)

    @data('staff', 'owner')
    def test_not_can_set_done_state_if_current_state_is_wrong(self, user):
        self.order.state = models.Order.States.REQUESTED_FOR_APPROVAL
        self.order.save()
        response = self.update_offering(user, 'set_state_done')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data('admin', 'manager')
    def test_not_can_set_done_state(self, user):
        response = self.update_offering(user, 'set_state_done')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @base.override_marketplace_settings(MANAGER_CAN_APPROVE_ORDER=True)
    @data('manager')
    def test_can_set_done_state_if_this_is_enabled_by_settings(self, user):
        self.order.state = models.Order.States.EXECUTING
        self.order.save()
        order_state = models.Order.States.DONE
        response = self.update_offering(user, 'set_state_done')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.state, order_state)
        self.assertTrue(models.Order.objects.filter(state=order_state).exists())

    @data('staff', 'owner', 'admin', 'manager')
    def test_can_set_terminated_state(self, user):
        order_state = models.Order.States.TERMINATED
        response = self.update_offering(user, 'set_state_terminated')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.state, order_state)
        self.assertTrue(models.Order.objects.filter(state=order_state).exists())

    @data('user')
    def test_not_can_set_terminated_state(self, user):
        response = self.update_offering(user, 'set_state_terminated')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def update_offering(self, user, action):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, action=action)
        response = self.client.post(url)
        self.order.refresh_from_db()
        return response


@ddt
class OrderDeleteTest(utils.PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)

    @data('staff', 'owner')
    def test_owner_and_staff_can_delete_order(self, user):
        response = self.delete_order(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT, response.data)
        self.assertFalse(models.Order.objects.filter(created_by=self.manager).exists())

    @data('admin', 'manager')
    def test_other_colleagues_can_not_delete_order(self, user):
        response = self.delete_order(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Order.objects.filter(created_by=self.manager).exists())

    @data('user')
    def test_other_user_can_not_delete_order(self, user):
        response = self.delete_order(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(models.Order.objects.filter(created_by=self.manager).exists())

    def delete_order(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order)
        response = self.client.delete(url)
        return response
