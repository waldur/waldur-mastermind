from __future__ import unicode_literals

import mock
from rest_framework import status, test

from waldur_core.structure.tests import fixtures

from . import factories
from .. import models


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
            project=self.project,
            offering=self.offering,
            plan=self.plan2,
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
        response = self.switch_plan(self.fixture.owner, self.resource1, factories.PlanFactory())

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

    def test_plan_switch_is_not_available_if_user_is_not_authorized(self):
        # Act
        response = self.switch_plan(self.fixture.global_support, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_order_item_is_created(self):
        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(models.OrderItem.objects.filter(
            type=models.OrderItem.Types.UPDATE,
            plan=self.plan2,
            resource=self.resource1,
        ).exists())

    def test_order_is_created(self):
        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(models.Order.objects.filter(
            project=self.project, created_by=self.fixture.owner
        ).exists())

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.switch_plan(self.fixture.staff, self.resource1, self.plan2)

        # Assert
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, models.Order.States.EXECUTING)
        self.assertEqual(order.approved_by, self.fixture.staff)

    def test_plan_switch_is_not_allowed_if_pending_order_item_for_resource_already_exists(self):
        # Arrange
        factories.OrderItemFactory(resource=self.resource1, state=models.OrderItem.States.PENDING)

        # Act
        response = self.switch_plan(self.fixture.staff, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_switching_is_not_available_for_blocked_organization(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_mastermind.marketplace.views.tasks')
    def test_order_has_been_approved_if_user_has_got_permissions(self, mock_tasks):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_tasks.process_order.delay.assert_called_once_with('marketplace.order:%s' % order.id,
                                                               'core.user:%s' % self.fixture.owner.id)

    @mock.patch('waldur_mastermind.marketplace.views.tasks')
    def test_order_has_not_been_approved_if_user_has_not_got_permissions(self, mock_tasks):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.admin, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_tasks.process_order.delay.assert_not_called()


class ResourceTerminateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.plan.offering,
            plan=self.plan,
            state=models.Resource.States.OK,
        )

    def terminate(self, user):
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(self.resource, 'terminate')
        return self.client.post(url)

    def test_order_item_is_created_when_user_submits_termination_request(self):
        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.project, self.project)

    def test_termination_request_is_not_accepted_if_resource_is_not_OK(self):
        # Arrange
        self.resource.state = models.Resource.States.UPDATING
        self.resource.save()

        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_termination_request_is_not_accepted_if_user_is_not_authorized(self):
        # Act
        response = self.terminate(self.fixture.global_support)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.terminate(self.fixture.staff)

        # Assert
        order = models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, models.Order.States.EXECUTING)
        self.assertEqual(order.approved_by, self.fixture.staff)

    def test_plan_switch_is_not_allowed_if_pending_order_item_for_resource_already_exists(self):
        # Arrange
        factories.OrderItemFactory(resource=self.resource, state=models.OrderItem.States.PENDING)

        # Act
        response = self.terminate(self.fixture.staff)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resource_terminating_is_not_available_for_blocked_organization(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.terminate(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


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
        self.assertEqual(response.data[0]['customer_provider_uuid'], self.offering.customer.uuid)
        self.assertEqual(response.data[0]['plan_uuid'], self.plan1.uuid)
        self.assertEqual(response.data[0]['usage'], 3)

    def test_count_plans_for_terminated_resources(self):
        response = self.get_stats()
        self.assertEqual(response.data[1]['usage'], 2)

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

        response = self.get_stats({'customer_provider_uuid': plan.offering.customer.uuid.hex})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['usage'], 4)
        self.assertEqual(response.data[0]['customer_provider_uuid'], plan.offering.customer.uuid)
