import traceback
import unittest

from ddt import data, ddt
from django.core.exceptions import ValidationError
from rest_framework import status, test

from waldur_core.quotas import signals as quota_signals
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
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

    def test_service_provider_can_see_order(self):
        # Arrange
        user = structure_factories.UserFactory()
        self.order_item.offering.customer.add_user(
            user, structure_models.CustomerRole.OWNER
        )

        # Act
        self.client.force_authenticate(user)
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.order_item.uuid.hex)


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
            'offering': factories.OfferingFactory.get_public_url(self.offering),
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
            'offering': factories.OfferingFactory.get_public_url(self.offering),
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
            'offering': factories.OfferingFactory.get_public_url(
                self.order_item.offering
            ),
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
        self.offering.type = 'OpenStack.Admin'
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
            project=self.project,
            offering=self.plan.offering,
            plan=self.plan,
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


@ddt
class ItemRejectTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.offering = factories.OfferingFactory(
            type='Support.OfferingTemplate', customer=self.fixture.customer
        )
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        resource = factories.ResourceFactory(offering=self.offering)
        self.order_item = factories.OrderItemFactory(
            resource=resource,
            order=self.order,
            offering=self.offering,
            state=models.OrderItem.States.EXECUTING,
        )

    @data(
        'staff',
        'owner',
    )
    def test_authorized_user_can_reject_item(self, user):
        response = self.reject_item(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order_item.refresh_from_db()
        self.assertEqual(self.order_item.state, models.OrderItem.States.TERMINATED)

    @data(
        'admin',
        'manager',
    )
    def test_user_cannot_reject_item(self, user):
        response = self.reject_item(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        models.OrderItem.States.TERMINATED,
    )
    def test_order_item_cannot_be_rejected_if_it_is_in_terminated_state(self, state):
        self.order_item.state = state
        self.order_item.save()
        response = self.reject_item('staff')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_order_item_can_be_rejected_if_it_is_in_pending_state(self):
        self.order_item.state = models.OrderItem.States.PENDING
        self.order_item.save()
        response = self.reject_item('staff')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_when_create_order_item_with_basic_offering_is_rejected_resource_is_marked_as_terminated(
        self,
    ):
        self.offering.type = 'Marketplace.Basic'
        self.offering.save()

        self.reject_item('owner')
        self.order_item.refresh_from_db()
        self.assertEqual(
            models.Resource.States.TERMINATED, self.order_item.resource.state
        )

    def test_when_update_order_item_with_basic_offering_is_rejected_resource_is_marked_as_erred(
        self,
    ):
        self.offering.type = 'Marketplace.Basic'
        self.offering.save()
        self.order_item.type = models.OrderItem.Types.UPDATE
        self.order_item.save()

        plan_period = factories.ResourcePlanPeriodFactory()
        old_plan = plan_period.plan
        old_plan.offering = self.offering
        old_plan.save()

        old_limits = {'unit': 50}
        resource = self.order_item.resource
        resource.plan = old_plan
        resource.limits = old_limits
        resource.save()

        plan_period.resource = resource
        plan_period.save()

        self.reject_item('owner')
        self.order_item.refresh_from_db()
        self.assertEqual(models.Resource.States.ERRED, self.order_item.resource.state)
        self.assertEqual(old_plan, self.order_item.resource.plan)
        self.assertEqual(old_limits, self.order_item.resource.limits)

    def test_when_terminate_order_item_with_basic_offering_is_rejected_resource_is_marked_as_ok(
        self,
    ):
        self.offering.type = 'Marketplace.Basic'
        self.offering.save()
        self.order_item.type = models.OrderItem.Types.TERMINATE
        self.order_item.save()

        self.reject_item('owner')
        self.order_item.refresh_from_db()
        self.assertEqual(models.Resource.States.OK, self.order_item.resource.state)

    def reject_item(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderItemFactory.get_url(self.order_item, 'reject')
        return self.client.post(url)


class BaseItemSetStateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager

        self.offering = self.fixture.offering
        self.offering.type = 'Marketplace.Slurm'
        self.offering.save()

        self.order_item = self.fixture.order_item


@ddt
class ItemSetStateExecutingTest(BaseItemSetStateTest):
    @data(
        ('staff', models.OrderItem.States.PENDING),
        ('staff', models.OrderItem.States.ERRED),
        ('offering_owner', models.OrderItem.States.PENDING),
        ('offering_owner', models.OrderItem.States.ERRED),
    )
    def test_authorized_user_can_set_executing_state(self, user_and_state):
        user, state = user_and_state
        self.order_item.state = state
        self.order_item.save()

        response = self.item_set_state_executing(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order_item.refresh_from_db()
        self.assertEqual(self.order_item.state, models.OrderItem.States.EXECUTING)

    @data('admin', 'manager', 'owner')
    def test_user_cannot_set_executing_state(self, user):
        response = self.item_set_state_executing(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def item_set_state_executing(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderItemFactory.get_url(self.order_item, 'set_state_executing')
        return self.client.post(url)


@ddt
class ItemSetStateDoneTest(BaseItemSetStateTest):
    @data('staff', 'offering_owner')
    def test_authorized_user_can_set_done_state(self, user):
        self.order_item.state = models.OrderItem.States.EXECUTING
        self.order_item.save()

        response = self.item_set_state_done(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order_item.refresh_from_db()
        self.assertEqual(self.order_item.state, models.OrderItem.States.DONE)

    @data('admin', 'manager', 'owner')
    def test_user_cannot_set_done_state(self, user):
        response = self.item_set_state_done(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def item_set_state_done(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderItemFactory.get_url(self.order_item, 'set_state_done')
        return self.client.post(url)


@ddt
class ItemSetStateErredTest(BaseItemSetStateTest):
    @data('staff', 'offering_owner')
    def test_authorized_user_can_set_erred_state(self, user):
        self.order_item.state = models.OrderItem.States.EXECUTING
        self.order_item.save()

        error_message = 'Resource creation has been failed'
        error_traceback = traceback.format_exc()
        user = 'staff'
        response = self.item_set_state_erred(user, error_message, error_traceback)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order_item.refresh_from_db()
        self.assertEqual(self.order_item.state, models.OrderItem.States.ERRED)
        self.assertEqual(self.order_item.error_message, error_message)
        self.assertEqual(self.order_item.error_traceback, error_traceback.strip())

    @data('admin', 'manager', 'owner')
    def test_user_cannot_set_erred_state(self, user):
        response = self.item_set_state_erred(
            user, 'Resource creation has been failed', traceback.format_exc()
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def item_set_state_erred(self, user, error_message, error_traceback):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderItemFactory.get_url(self.order_item, 'set_state_erred')
        return self.client.post(
            url, {'error_message': error_message, 'error_traceback': error_traceback}
        )
