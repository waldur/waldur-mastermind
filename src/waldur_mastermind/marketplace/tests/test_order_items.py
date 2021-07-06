import unittest

from ddt import data, ddt
from django.core.exceptions import ValidationError
from rest_framework import status, test

from waldur_core.quotas import signals as quota_signals
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class OrderItemFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        self.order_item = factories.OrderItemFactory(order=self.order)
        self.url = factories.OrderItemFactory.get_list_url()

    @data('staff', 'owner', 'admin', 'manager')
    def test_items_should_be_visible_to_colleagues_and_staff(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data('user')
    def test_items_should_be_invisible_to_other_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_items_should_be_invisible_to_unauthenticated_users(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filter_order_items_for_service_manager(self):
        # Arrange
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        offering.add_user(self.fixture.user)
        order_item = factories.OrderItemFactory(offering=offering, order=self.order)

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            self.url, {'service_manager_uuid': self.fixture.user.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], order_item.uuid.hex)


@unittest.skip('OrderItem creation is irrelevant now.')
@ddt
class ItemCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
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
            'order': factories.OrderFactory.get_url(self.order),
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
            'order': factories.OrderFactory.get_url(self.order),
        }
        return self.client.post(url, payload)


@unittest.skip('OrderItem update is irrelevant now.')
@ddt
class ItemUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
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
            'plan': factories.PlanFactory.get_url(self.order_item.plan),
        }
        response = self.client.patch(url, payload)
        self.order_item.refresh_from_db()
        return response


@ddt
class ItemDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        self.order_item = factories.OrderItemFactory(order=self.order)

    @data('staff', 'owner', 'admin', 'manager')
    def test_authorized_user_can_delete_item(self, user):
        response = self.delete_item(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
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


@ddt
class ItemTerminateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.offering = factories.OfferingFactory(type='Support.OfferingTemplate')
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        self.order_item = factories.OrderItemFactory(
            order=self.order, offering=self.offering
        )

    @data('staff', 'owner', 'admin', 'manager')
    def test_authorized_user_can_terminate_item(self, user):
        response = self.terminate_item(user)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.order_item.refresh_from_db()
        self.assertEqual(self.order_item.state, models.OrderItem.States.TERMINATING)

    @data(
        models.OrderItem.States.DONE,
        models.OrderItem.States.ERRED,
        models.OrderItem.States.TERMINATED,
    )
    def test_order_item_cannot_be_terminated_if_it_is_in_terminal_state(self, state):
        self.order_item.state = state
        self.order_item.save()
        response = self.terminate_item('staff')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_terminate_order_if_it_is_not_supported_by_offering(self):
        self.offering.type = 'Packages.Template'
        self.offering.save()
        response = self.terminate_item('staff')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def terminate_item(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderItemFactory.get_url(self.order_item, 'terminate')
        return self.client.post(url)


class AggregateResourceCountTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer
        self.plan = factories.PlanFactory()
        self.resource = models.Resource.objects.create(
            project=self.project, offering=self.plan.offering, plan=self.plan,
        )
        self.category = self.plan.offering.category

    def test_when_resource_scope_is_updated_resource_count_is_increased(self):
        self.resource.scope = self.fixture.resource
        self.resource.save()
        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.project, category=self.category
            ).count,
            1,
        )
        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.customer, category=self.category
            ).count,
            1,
        )

    def test_when_resource_scope_is_updated_resource_count_is_decreased(self):
        self.resource.scope = self.fixture.resource
        self.resource.save()
        self.resource.state = models.Resource.States.TERMINATED
        self.resource.save()

        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.project, category=self.category
            ).count,
            0,
        )
        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.customer, category=self.category
            ).count,
            0,
        )

    def test_recalculate_count(self):
        self.resource.scope = self.fixture.resource
        self.resource.save()
        models.AggregateResourceCount.objects.all().delete()
        quota_signals.recalculate_quotas.send(sender=self)

        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.project, category=self.category
            ).count,
            1,
        )
        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.customer, category=self.category
            ).count,
            1,
        )


class ItemValidateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    def test_types_of_items_in_one_order_must_be_the_same(self):
        new_item = factories.OrderItemFactory(
            order=self.fixture.order,
            offering=self.fixture.offering,
            type=models.RequestTypeMixin.Types.UPDATE,
        )
        self.assertRaises(ValidationError, new_item.clean)
