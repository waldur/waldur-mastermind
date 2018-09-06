from __future__ import unicode_literals

import mock
from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status

from waldur_core.core.tests.utils import PostgreSQLTest
from waldur_core.structure.tests import fixtures, factories as structure_factories
from waldur_mastermind.marketplace.tests.factories import OFFERING_OPTIONS

from . import factories
from .. import models, base


@ddt
class OrderGetTest(PostgreSQLTest):

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
class OrderCreateTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_can_create_order_in_valid_project(self, user):
        user = getattr(self.fixture, user)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())
        self.assertEqual(1, len(response.data['items']))

    @data('user')
    def test_user_can_not_create_order_in_invalid_project(self, user):
        user = getattr(self.fixture, user)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_not_create_item_if_offering_is_not_available(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ARCHIVED)
        response = self.create_order(self.fixture.staff, offering)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_order_with_plan(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)
        add_payload = {'items': [
            {
                'offering': factories.OfferingFactory.get_url(offering),
                'plan': factories.PlanFactory.get_url(plan),
                'attributes': {}
            },
        ]}
        response = self.create_order(self.fixture.staff, offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_can_not_create_order_with_invalid_plan(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)
        add_payload = {'items': [
            {
                'offering': factories.OfferingFactory.get_url(),
                'plan': factories.PlanFactory.get_url(plan),
                'attributes': {}
            },
        ]}
        response = self.create_order(self.fixture.staff, offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_create_order_with_valid_attributes_specified_by_options(self):
        attributes = {
            'storage': 1000,
            'ram': 30,
            'cpu_count': 5,
        }
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE, options=OFFERING_OPTIONS)
        plan = factories.PlanFactory(offering=offering)
        add_payload = {'items': [
            {
                'offering': factories.OfferingFactory.get_url(offering),
                'plan': factories.PlanFactory.get_url(plan),
                'attributes': attributes,
            },
        ]}
        response = self.create_order(self.fixture.staff, offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['items'][0]['attributes'], attributes)

    def test_user_can_not_create_order_with_invalid_attributes(self):
        attributes = {
            'storage': 'invalid value',
        }
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE, options=OFFERING_OPTIONS)
        plan = factories.PlanFactory(offering=offering)
        add_payload = {'items': [
            {
                'offering': factories.OfferingFactory.get_url(offering),
                'plan': factories.PlanFactory.get_url(plan),
                'attributes': attributes,
            },
        ]}
        response = self.create_order(self.fixture.staff, offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def create_order(self, user, offering=None, add_payload=None):
        if offering is None:
            offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        payload = {
            'project': structure_factories.ProjectFactory.get_url(self.project),
            'items': [
                {
                    'offering': factories.OfferingFactory.get_url(offering),
                    'attributes': {}
                },
            ]
        }

        if add_payload:
            payload.update(add_payload)

        return self.client.post(url, payload)


@ddt
class OrderUpdateTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)

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
        response = self.update_offering('owner', 'set_state_executing')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.approved_at.strftime('%Y-%m-%d %H:%M:%S'), '2017-01-10 00:00:00')
        self.assertEqual(self.order.approved_by, self.fixture.owner)

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

    @mock.patch('waldur_mastermind.marketplace.handlers.tasks')
    def test_notifications_when_order_approval_is_requested(self, mock_tasks):
        order = factories.OrderFactory(project=self.project, created_by=self.manager)
        self.assertEqual(mock_tasks.notify_order_approvers.delay.call_count, 1)
        self.assertEqual(mock_tasks.notify_order_approvers.delay.call_args[0][0], order.uuid)

    @mock.patch('waldur_mastermind.marketplace.handlers.tasks')
    def test_not_send_notification_if_state_is_not_requested_for_approval(self, mock_tasks):
        self.order.set_state_terminated()
        self.order.save()
        self.assertEqual(mock_tasks.notify_order_approvers.delay.call_count, 0)

    def update_offering(self, user, action):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, action=action)
        response = self.client.post(url)
        self.order.refresh_from_db()
        return response


@ddt
class OrderDeleteTest(PostgreSQLTest):

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


class OrderStateTest(PostgreSQLTest):
    def test_switch_order_state_to_done_when_all_order_items_are_processed(self):
        order_item = factories.OrderItemFactory(state=models.OrderItem.States.EXECUTING)
        order = order_item.order
        order.state = models.Order.States.EXECUTING
        order.save()
        order_item.state = models.OrderItem.States.DONE
        order_item.save()
        order.refresh_from_db()
        self.assertEqual(order.state, models.Order.States.DONE)

    def test_not_switch_order_state_to_done_when_not_all_order_items_are_processed(self):
        order_item = factories.OrderItemFactory(state=models.OrderItem.States.EXECUTING)
        order = order_item.order
        factories.OrderItemFactory(state=models.OrderItem.States.EXECUTING, order=order)
        order.state = models.Order.States.EXECUTING
        order.save()
        order_item.state = models.OrderItem.States.DONE
        order_item.save()
        order.refresh_from_db()
        self.assertEqual(order.state, models.Order.States.EXECUTING)
