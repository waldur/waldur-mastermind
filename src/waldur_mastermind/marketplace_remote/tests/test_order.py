from unittest import mock

from django.core import mail
from django.test import override_settings
from rest_framework import test

import waldur_core.structure.models as structure_models
import waldur_core.structure.tests.factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tasks import approve_order
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_remote import PLUGIN_NAME


class OrderCreateTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = PLUGIN_NAME
        self.offering.save()
        self.order_item = self.fixture.order_item
        self.order_item.state = marketplace_models.OrderItem.States.PENDING
        self.order_item.save()

        self.order = self.order_item.order

        self.fixture.offering_owner

        event_type = 'notification_service_provider_approval'
        structure_factories.NotificationFactory(key=f"marketplace.{event_type}")

    @override_settings(
        task_always_eager=True,
    )
    @mock.patch('waldur_mastermind.marketplace.utils.process_order_item')
    def test_order_is_processed_when_plugin_option_is_enabled(self, process_order_item):
        self.offering.plugin_options = {'auto_approve_remote_orders': True}
        self.offering.save()

        approve_order(self.order, self.fixture.service_manager)

        self.order.refresh_from_db()
        self.assertEqual(marketplace_models.Order.States.EXECUTING, self.order.state)

        self.order_item.refresh_from_db()
        self.assertEqual(
            marketplace_models.OrderItem.States.EXECUTING, self.order_item.state
        )

        process_order_item.assert_called_once()

        self.assertEqual(len(mail.outbox), 0)

    @override_settings(
        task_always_eager=True,
    )
    @mock.patch('waldur_mastermind.marketplace.utils.process_order_item')
    def test_order_is_processed_when_plugin_option_is_disabled(
        self, process_order_item
    ):
        self.offering.plugin_options = {'auto_approve_remote_orders': False}
        self.offering.save()

        approve_order(self.order, self.fixture.service_manager)

        self.order.refresh_from_db()
        self.assertEqual(marketplace_models.Order.States.EXECUTING, self.order.state)

        self.order_item.refresh_from_db()
        self.assertEqual(
            marketplace_models.OrderItem.States.PENDING, self.order_item.state
        )

        process_order_item.assert_not_called()

        self.assertEqual(len(mail.outbox), 1)

    @override_settings(
        task_always_eager=True,
    )
    @mock.patch('waldur_mastermind.marketplace.utils.process_order_item')
    def test_order_is_processed_when_plugin_option_is_absent(self, process_order_item):
        approve_order(self.order, self.fixture.service_manager)

        self.order.refresh_from_db()
        self.assertEqual(marketplace_models.Order.States.EXECUTING, self.order.state)

        self.order_item.refresh_from_db()
        self.assertEqual(
            marketplace_models.OrderItem.States.PENDING, self.order_item.state
        )

        process_order_item.assert_not_called()

        self.assertEqual(len(mail.outbox), 1)


class LimitsUpdateTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = PLUGIN_NAME
        self.offering.save()

        self.resource = self.fixture.resource
        self.resource.set_state_ok()
        self.resource.save()

        self.plan_component = self.fixture.plan_component
        self.offering_component = self.fixture.offering_component
        self.offering_component.billing_type = (
            marketplace_models.OfferingComponent.BillingTypes.LIMIT
        )
        self.offering_component.save()

    def update_limits(self, user, resource):
        limits = {'cpu': 10}
        customer = self.fixture.customer
        customer.add_user(user, structure_models.CustomerRole.OWNER)

        self.client.force_authenticate(user)
        url = marketplace_factories.ResourceFactory.get_url(resource, 'update_limits')
        payload = {'limits': limits}
        return self.client.post(url, payload)

    @override_settings(task_always_eager=True)
    @mock.patch('waldur_mastermind.marketplace.utils.process_order_item')
    def test_order_item_is_approved_implicitly_for_SP_owner(self, process_order_item):
        # Act
        user = self.fixture.offering_owner
        response = self.update_limits(user, self.resource)

        # Assert
        self.assertEqual(response.status_code, 200, response.data)
        order = marketplace_models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, marketplace_models.Order.States.EXECUTING)
        self.assertEqual(order.approved_by, user)
        process_order_item.assert_called_once()
        order_item = order.items.first()
        self.assertEqual(
            order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )

    @override_settings(task_always_eager=True)
    @mock.patch('waldur_mastermind.marketplace.utils.process_order_item')
    def test_order_item_is_approved_implicitly_for_SP_service_manager(
        self, process_order_item
    ):
        # Act
        user = self.fixture.service_manager
        self.offering.add_user(user)
        response = self.update_limits(user, self.resource)

        # Assert
        self.assertEqual(response.status_code, 200, response.data)
        order = marketplace_models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, marketplace_models.Order.States.EXECUTING)
        self.assertEqual(order.approved_by, user)
        process_order_item.assert_called_once()
        order_item = order.items.first()
        self.assertEqual(
            order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )

    @override_settings(task_always_eager=True)
    @mock.patch('waldur_mastermind.marketplace.utils.process_order_item')
    def test_order_item_is_not_approved_for_SP_service_manager_of_another_offering(
        self, process_order_item
    ):
        # Act
        user = self.fixture.service_manager
        response = self.update_limits(user, self.resource)

        # Assert
        self.assertEqual(response.status_code, 200, response.data)
        order = marketplace_models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, marketplace_models.Order.States.EXECUTING)
        process_order_item.assert_not_called()
        order_item = order.items.first()
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.PENDING)
