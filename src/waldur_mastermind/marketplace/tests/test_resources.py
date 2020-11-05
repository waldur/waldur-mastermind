from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace.processors import process_order_item
from waldur_mastermind.support.tests.base import override_support_settings

from .. import callbacks, log, models, plugins, tasks
from . import factories
from . import utils as test_utils


class ResourceGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = models.Resource.objects.create(
            project=self.project, offering=self.offering, plan=self.plan,
        )

    def get_resource(self, user=None):
        if not user:
            user = self.fixture.owner
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(self.resource)
        return self.client.get(url)

    def test_resource_is_usage_based(self):
        factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
        )

        self.assertTrue(self.get_resource().data['is_usage_based'])

    def test_resource_is_not_usage_based(self):
        self.assertFalse(self.get_resource().data['is_usage_based'])

    def test_project_manager_can_get_resource_data(self):
        response = self.get_resource(self.fixture.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_owner_can_get_resource_data(self):
        response = self.get_resource(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_service_provider_can_get_resource_data(self):
        owner = UserFactory()
        self.offering.customer.add_user(owner, structure_models.CustomerRole.OWNER)

        response = self.get_resource()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_other_user_can_not_get_resource_data(self):
        response = self.get_resource(UserFactory())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ResourceSwitchPlanTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan1 = factories.PlanFactory()
        self.offering = self.plan1.offering
        self.plan2 = factories.PlanFactory(offering=self.offering)
        self.resource1 = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan1,
            state=models.Resource.States.OK,
        )
        self.resource2 = models.Resource.objects.create(
            project=self.project, offering=self.offering, plan=self.plan2,
        )

    def switch_plan(self, user, resource, plan):
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(resource, 'switch_plan')
        payload = {'plan': factories.PlanFactory.get_url(plan)}
        return self.client.post(url, payload)

    def test_plan_switch_is_available_if_plan_limit_is_not_reached(self):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_plan_switch_is_available_if_resource_is_terminated(self):
        # Arrange
        self.resource2.state = models.Resource.States.TERMINATED
        self.resource2.save()

        self.plan2.max_amount = 1
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_plan_switch_is_not_available_if_plan_limit_has_been_reached(self):
        # Arrange
        self.plan2.max_amount = 1
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_switch_is_not_available_if_plan_is_related_to_another_offering(self):
        # Act
        response = self.switch_plan(
            self.fixture.owner, self.resource1, factories.PlanFactory()
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_switch_is_not_available_if_resource_is_not_OK(self):
        # Arrange
        self.resource1.state = models.Resource.States.UPDATING
        self.resource1.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_order_item_is_created(self):
        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            models.OrderItem.objects.filter(
                type=models.OrderItem.Types.UPDATE,
                plan=self.plan2,
                resource=self.resource1,
            ).exists()
        )

    def test_order_is_created(self):
        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            models.Order.objects.filter(
                project=self.project, created_by=self.fixture.owner
            ).exists()
        )

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.switch_plan(self.fixture.staff, self.resource1, self.plan2)

        # Assert
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, models.Order.States.EXECUTING)
        self.assertEqual(order.approved_by, self.fixture.staff)

    def test_plan_switch_is_not_allowed_if_pending_order_item_for_resource_already_exists(
        self,
    ):
        # Arrange
        factories.OrderItemFactory(
            resource=self.resource1, state=models.OrderItem.States.PENDING
        )

        # Act
        response = self.switch_plan(self.fixture.staff, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_switching_is_not_available_for_blocked_organization(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_mastermind.marketplace.tasks.process_order')
    def test_order_has_been_approved_if_user_has_got_permissions(self, mock_task):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.delay.assert_called_once_with(
            'marketplace.order:%s' % order.id, 'core.user:%s' % self.fixture.owner.id
        )

    @mock.patch('waldur_mastermind.marketplace.views.tasks')
    def test_order_has_not_been_approved_if_user_has_not_got_permissions(
        self, mock_tasks
    ):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.admin, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_tasks.process_order.delay.assert_not_called()


@ddt
class ResourceTerminateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            state=models.Resource.States.OK,
        )

    def terminate(self, user, attributes=None):
        attributes = attributes or {}
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(self.resource, 'terminate')
        if attributes:
            return self.client.post(url, {'attributes': attributes})
        else:
            return self.client.post(url)

    @mock.patch('waldur_mastermind.marketplace.tasks.notify_order_approvers.delay')
    def test_service_provider_can_terminate_resource(self, mocked_approve):
        # Arrange
        owner = UserFactory()
        self.offering.customer.add_user(owner, structure_models.CustomerRole.OWNER)

        # Act
        response = self.terminate(owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_approve.assert_not_called()

    def test_order_item_is_created_when_user_submits_termination_request(self):
        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.project, self.project)

    @data(
        models.Resource.States.CREATING,
        models.Resource.States.UPDATING,
        models.Resource.States.TERMINATING,
    )
    def test_termination_request_is_not_accepted_if_resource_is_not_ok_or_erred(
        self, state
    ):
        # Arrange
        self.resource.state = state
        self.resource.save()

        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data(models.Resource.States.OK, models.Resource.States.ERRED)
    def test_termination_request_is_accepted_if_resource_is_ok_or_erred(self, state):
        # Arrange
        self.resource.state = state
        self.resource.save()

        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.terminate(self.fixture.staff)

        # Assert
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, models.Order.States.EXECUTING)
        self.assertEqual(order.approved_by, self.fixture.staff)

    def test_plan_switch_is_not_allowed_if_pending_order_item_for_resource_already_exists(
        self,
    ):
        # Arrange
        factories.OrderItemFactory(
            resource=self.resource, state=models.OrderItem.States.PENDING
        )

        # Act
        response = self.terminate(self.fixture.staff)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resource_terminating_is_not_available_for_blocked_organization(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.terminate(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_options_can_be_passed_if_resource_is_terminated(self):
        # Act
        response = self.terminate(self.fixture.staff, {'param': True})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.project, self.project)
        item = order.items.first()
        self.assertTrue(item.attributes.get('param'))


class PlanUsageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan1 = factories.PlanFactory()
        self.offering = self.plan1.offering
        self.plan2 = factories.PlanFactory(offering=self.offering)

        factories.ResourceFactory.create_batch(
            3,
            project=self.project,
            offering=self.offering,
            plan=self.plan1,
            state=models.Resource.States.OK,
        )

        factories.ResourceFactory.create_batch(
            2,
            project=self.project,
            offering=self.offering,
            plan=self.plan2,
            state=models.Resource.States.OK,
        )

        factories.ResourceFactory.create_batch(
            2,
            project=self.project,
            offering=self.offering,
            plan=self.plan2,
            state=models.Resource.States.TERMINATED,
        )

    def get_stats(self, data=None):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.PlanFactory.get_list_url('usage_stats')
        response = self.client.get(url, data)
        return response

    def test_count_plans_for_ok_resources(self):
        response = self.get_stats()
        self.assertEqual(response.data[0]['offering_uuid'], self.offering.uuid)
        self.assertEqual(
            response.data[0]['customer_provider_uuid'], self.offering.customer.uuid
        )
        self.assertEqual(response.data[0]['plan_uuid'], self.plan1.uuid)
        self.assertEqual(response.data[0]['usage'], 3)

    def test_count_plans_for_terminated_resources(self):
        response = self.get_stats()
        self.assertEqual(response.data[1]['usage'], 2)

    def test_order_by_remaining_ascending(self):
        self.plan1.max_amount = 100
        self.plan1.save()

        self.plan2.max_amount = 10
        self.plan2.save()

        response = self.get_stats({'o': 'remaining'})
        data = response.data

        self.assertEqual(data[0]['remaining'], 10 - 2)
        self.assertEqual(data[1]['remaining'], 100 - 3)

    def test_order_by_remaining_descending(self):
        self.plan1.max_amount = 100
        self.plan1.save()

        self.plan2.max_amount = 10
        self.plan2.save()

        response = self.get_stats({'o': '-remaining'})
        data = response.data

        self.assertEqual(data[0]['remaining'], 100 - 3)
        self.assertEqual(data[1]['remaining'], 10 - 2)

    def test_filter_plans_by_offering_uuid(self):
        plan = factories.PlanFactory()

        factories.ResourceFactory.create_batch(
            4,
            project=self.project,
            offering=plan.offering,
            plan=plan,
            state=models.Resource.States.OK,
        )

        response = self.get_stats({'offering_uuid': plan.offering.uuid.hex})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['usage'], 4)
        self.assertEqual(response.data[0]['offering_uuid'], plan.offering.uuid)

    def test_filter_plans_by_customer_provider_uuid(self):
        plan = factories.PlanFactory()

        factories.ResourceFactory.create_batch(
            4,
            project=self.project,
            offering=plan.offering,
            plan=plan,
            state=models.Resource.States.OK,
        )

        response = self.get_stats(
            {'customer_provider_uuid': plan.offering.customer.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['usage'], 4)
        self.assertEqual(
            response.data[0]['customer_provider_uuid'], plan.offering.customer.uuid
        )


class ResourceCostEstimateTest(test.APITransactionTestCase):
    @override_support_settings(
        ENABLED=True,
        ACTIVE_BACKEND='waldur_mastermind.support.backend.basic:BasicBackend',
    )
    def test_when_order_item_is_processed_cost_estimate_is_initialized(self):
        # Arrange
        fixture = fixtures.ProjectFixture()
        offering = factories.OfferingFactory(type='Support.OfferingTemplate')
        plan = factories.PlanFactory(unit_price=10)

        order_item = factories.OrderItemFactory(
            offering=offering,
            plan=plan,
            attributes={'name': 'item_name', 'description': 'Description'},
        )

        # Act
        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        tasks.process_order(serialized_order, serialized_user)

        # Assert
        order_item.refresh_from_db()
        self.assertEqual(order_item.resource.cost, plan.unit_price)

    def test_initialization_cost_is_added_to_cost_estimate_for_creation_request(self):
        # Arrange
        offering = factories.OfferingFactory(type='Support.OfferingTemplate')
        one_time_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=models.OfferingComponent.BillingTypes.ONE_TIME,
            type='signup',
        )
        usage_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
            type='cpu',
        )

        plan = factories.PlanFactory()
        factories.PlanComponentFactory(
            plan=plan, component=one_time_offering_component, price=100
        )
        factories.PlanComponentFactory(
            plan=plan, component=usage_offering_component, price=10
        )

        order_item = factories.OrderItemFactory(offering=offering, plan=plan,)
        order_item.init_cost()
        self.assertEqual(order_item.cost, 100)

    def test_when_plan_is_switched_cost_estimate_is_updated(self):
        # Arrange
        old_plan = factories.PlanFactory(unit_price=10)
        new_plan = factories.PlanFactory(unit_price=100)
        resource = factories.ResourceFactory(plan=old_plan)

        factories.OrderItemFactory(
            state=models.OrderItem.States.EXECUTING,
            type=models.OrderItem.Types.UPDATE,
            resource=resource,
            plan=new_plan,
        )

        # Act
        callbacks.resource_update_succeeded(resource)
        resource.refresh_from_db()

        # Assert
        self.assertEqual(resource.cost, new_plan.unit_price)

    def test_plan_switch_cost_is_added_to_cost_estimate_for_order_item(self):
        # Arrange
        offering = factories.OfferingFactory(type='Support.OfferingTemplate')
        switch_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH,
            type='plan_switch',
        )
        usage_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
            type='cpu',
        )

        plan = factories.PlanFactory()
        factories.PlanComponentFactory(
            plan=plan, component=switch_offering_component, price=50
        )
        factories.PlanComponentFactory(
            plan=plan, component=usage_offering_component, price=10
        )

        order_item = factories.OrderItemFactory(
            offering=offering, plan=plan, type=models.OrderItem.Types.UPDATE,
        )
        order_item.init_cost()
        self.assertEqual(order_item.cost, 50)


@ddt
class ResourceNotificationTest(test.APITransactionTestCase):
    @data(
        'log_resource_creation_succeeded',
        'log_resource_creation_failed',
        'log_resource_update_succeeded',
        'log_resource_update_failed',
        'log_resource_terminate_succeeded',
        'log_resource_terminate_failed',
    )
    @mock.patch('waldur_mastermind.marketplace.log.tasks')
    def test_notify_about_resource_change(self, log_func_name, mock_tasks):
        resource = factories.ResourceFactory()
        log_func = getattr(log, log_func_name)
        log_func(resource)
        if log_func_name != 'log_resource_update_succeeded':
            mock_tasks.notify_about_resource_change.delay.assert_called_once()
        else:
            mock_tasks.notify_about_resource_change.delay.assert_not_called()


class ResourceUpdateLimitsTest(test.APITransactionTestCase):
    def setUp(self):
        plugins.manager.register(
            offering_type='TEST_TYPE',
            create_resource_processor=test_utils.TestCreateProcessor,
            update_resource_processor=test_utils.TestUpdateProcessor,
        )

        self.fixture = fixtures.ServiceFixture()
        self.resource = factories.ResourceFactory()
        self.resource.state = models.Resource.States.OK
        self.resource.project.customer = self.fixture.customer
        self.resource.project.save()
        self.resource.limits = {'vcpu': 1}
        self.resource.save()
        self.resource.offering.type = 'TEST_TYPE'
        self.resource.offering.save()

    def update_limits(self, user, resource, limits=None):
        limits = limits or {'vcpu': 10}
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(resource, 'update_limits')
        payload = {'limits': limits}
        return self.client.post(url, payload)

    def test_create_update_limits_order(self):
        response = self.update_limits(self.fixture.owner, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_update_limits_is_not_available_if_resource_is_not_OK(self):
        # Arrange
        self.resource.state = models.Resource.States.UPDATING
        self.resource.save()

        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_order_item_is_created(self):
        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            models.OrderItem.objects.filter(
                type=models.OrderItem.Types.UPDATE, resource=self.resource,
            ).exists()
        )

    def test_order_is_created(self):
        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            models.Order.objects.filter(
                project=self.resource.project, created_by=self.fixture.owner
            ).exists()
        )

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.update_limits(self.fixture.staff, self.resource)

        # Assert
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, models.Order.States.EXECUTING)
        self.assertEqual(order.approved_by, self.fixture.staff)

    def test_update_limits_is_not_allowed_if_pending_order_item_for_resource_already_exists(
        self,
    ):
        # Arrange
        factories.OrderItemFactory(
            resource=self.resource, state=models.OrderItem.States.PENDING
        )

        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_limits_is_not_available_for_blocked_organization(self):
        customer = self.resource.project.customer
        customer.blocked = True
        customer.save()
        response = self.update_limits(self.fixture.owner, self.resource)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_mastermind.marketplace.tasks.process_order')
    def test_order_has_been_approved_if_user_has_got_permissions(self, mock_task):
        # Act
        response = self.update_limits(self.fixture.staff, self.resource)

        # Assert
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.delay.assert_called_once_with(
            'marketplace.order:%s' % order.id, 'core.user:%s' % self.fixture.staff.id
        )

    @mock.patch('waldur_mastermind.marketplace.views.tasks')
    def test_order_has_not_been_approved_if_user_has_not_got_permissions(
        self, mock_tasks
    ):
        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_tasks.process_order.delay.assert_not_called()

    def test_update_limit_process(self):
        response = self.update_limits(self.fixture.staff, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order_item = models.OrderItem.objects.get(
            type=models.OrderItem.Types.UPDATE, resource=self.resource,
        )
        process_order_item(order_item, self.fixture.staff)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits['vcpu'], 10)
