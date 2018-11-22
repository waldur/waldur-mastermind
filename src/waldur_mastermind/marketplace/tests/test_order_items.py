from __future__ import unicode_literals

import unittest

from ddt import data, ddt
from rest_framework import status

from waldur_core.structure.tests import fixtures
from waldur_core.core.tests.utils import PostgreSQLTest
from waldur_core.quotas import signals as quota_signals

from . import factories
from .. import models


@ddt
class ItemGetTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)
        self.order_item = factories.OrderItemFactory(order=self.order)

    @data('staff', 'owner', 'admin', 'manager')
    def test_items_should_be_visible_to_colleagues_and_staff(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderItemFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data('user')
    def test_items_should_be_invisible_to_other_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderItemFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_items_should_be_invisible_to_unauthenticated_users(self):
        url = factories.OrderItemFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@unittest.skip('OrderItem creation is irrelevant now.')
@ddt
class ItemCreateTest(PostgreSQLTest):

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
        self.assertTrue(models.OrderItem.objects.filter(order=self.order).exists())

    @data('user')
    def test_user_can_not_create_item_with_not_relation_project(self, user):
        response = self.create_item(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthorized_user_can_not_create_item(self):
        url = factories.OrderItemFactory.get_list_url()
        payload = {
            'offering': factories.OfferingFactory.get_url(self.offering),
            'order': factories.OrderFactory.get_url(self.order)
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_not_create_item_if_order_state_is_not_draft(self):
        self.order.state = models.Order.States.DONE
        self.order.save()
        response = self.create_item('staff')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.OrderItem.objects.filter(order=self.order).exists())

    def test_user_can_not_create_item_if_offering_is_not_available(self):
        self.offering.is_active = False
        self.offering.save()
        response = self.create_item('staff')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.OrderItem.objects.filter(order=self.order).exists())

    def create_item(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderItemFactory.get_list_url()
        payload = {
            'offering': factories.OfferingFactory.get_url(self.offering),
            'order': factories.OrderFactory.get_url(self.order)
        }
        return self.client.post(url, payload)


@unittest.skip('OrderItem update is irrelevant now.')
@ddt
class ItemUpdateTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)
        self.order_item = factories.OrderItemFactory(order=self.order)

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
        url = factories.OrderItemFactory.get_url(self.order_item)
        payload = {
            'offering': factories.OfferingFactory.get_url(self.order_item.offering),
            'plan': factories.PlanFactory.get_url(self.order_item.plan)
        }
        response = self.client.patch(url, payload)
        self.order_item.refresh_from_db()
        return response


@ddt
class ItemDeleteTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(project=self.project, created_by=self.manager)
        self.order_item = factories.OrderItemFactory(order=self.order)

    @data('staff', 'owner', 'admin', 'manager')
    def test_authorized_user_can_delete_item(self, user):
        response = self.delete_item(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT, response.data)
        self.assertFalse(models.OrderItem.objects.filter(order=self.order).exists())

    @data('user')
    def test_other_user_can_not_delete_item(self, user):
        response = self.delete_item(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(models.OrderItem.objects.filter(order=self.order).exists())

    def test_unauthorized_user_can_not_delete_item(self):
        url = factories.OrderItemFactory.get_url(self.order_item)
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
        url = factories.OrderItemFactory.get_url(self.order_item)
        response = self.client.delete(url)
        return response


class ProjectResourceCountTest(PostgreSQLTest):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.plan.offering,
            plan=self.plan,
        )
        self.category = self.plan.offering.category

    def test_when_resource_scope_is_updated_resource_count_is_increased(self):
        self.resource.scope = self.fixture.resource
        self.resource.save()
        self.assertEqual(models.ProjectResourceCount.objects.get(
            project=self.project, category=self.category).count, 1)

    def test_when_resource_scope_is_updated_resource_count_is_decreased(self):
        self.resource.scope = self.fixture.resource
        self.resource.save()
        self.resource.scope = None
        self.resource.save()

        self.assertEqual(models.ProjectResourceCount.objects.get(
            project=self.project, category=self.category).count, 0)

    def test_recalculate_count(self):
        self.resource.scope = self.fixture.resource
        self.resource.save()
        models.ProjectResourceCount.objects.all().delete()
        quota_signals.recalculate_quotas.send(sender=self)

        self.assertEqual(models.ProjectResourceCount.objects.get(
            project=self.project, category=self.category).count, 1)
