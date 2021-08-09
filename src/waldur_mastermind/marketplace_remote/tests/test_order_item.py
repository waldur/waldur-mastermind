from unittest import mock

from rest_framework import test

from waldur_core.core.utils import serialize_instance
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.marketplace.models import Order, OrderItem, Resource
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    OrderFactory,
    OrderItemFactory,
    ResourceFactory,
)
from waldur_mastermind.marketplace_remote import PLUGIN_NAME
from waldur_mastermind.marketplace_remote.tasks import OrderItemPullTask


class OrderItemPullTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch('waldur_mastermind.marketplace_remote.utils.WaldurClient')
        self.client_mock = patcher.start()
        fixture = ProjectFixture()
        offering = OfferingFactory(
            type=PLUGIN_NAME,
            secret_options={
                'api_url': 'https://remote-waldur.com/',
                'token': 'valid_token',
            },
        )
        order = OrderFactory(project=fixture.project, state=Order.States.EXECUTING)
        self.resource = ResourceFactory(project=fixture.project, offering=offering)
        self.order_item = OrderItemFactory(
            order=order,
            offering=offering,
            resource=self.resource,
            state=OrderItem.States.EXECUTING,
        )

    def tearDown(self):
        super(OrderItemPullTest, self).tearDown()
        mock.patch.stopall()

    def test_when_order_item_succeeds_resource_is_updated(self):
        # Arrange
        self.client_mock().get_order.return_value = {
            'items': [{'state': 'done', 'error_message': '',}]
        }

        # Act
        OrderItemPullTask().run(serialize_instance(self.order_item))

        # Assert
        self.order_item.refresh_from_db()
        self.assertEqual(self.order_item.state, OrderItem.States.DONE)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, Resource.States.OK)

    def test_when_order_item_fails_resource_is_updated(self):
        # Arrange
        self.client_mock().get_order.return_value = {
            'items': [{'state': 'erred', 'error_message': 'Invalid credentials',}]
        }

        # Act
        OrderItemPullTask().run(serialize_instance(self.order_item))

        # Assert
        self.order_item.refresh_from_db()
        self.assertEqual(self.order_item.state, OrderItem.States.ERRED)
        self.assertEqual(self.order_item.error_message, 'Invalid credentials')

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, Resource.States.ERRED)

    def test_when_creation_order_succeeds_resource_is_created(self):
        # Arrange
        self.client_mock().get_order.return_value = {
            'items': [
                {
                    'state': 'done',
                    'marketplace_resource_uuid': 'marketplace_resource_uuid',
                    'error_message': '',
                }
            ]
        }
        self.order_item.resource = None
        self.order_item.save()

        # Act
        OrderItemPullTask().run(serialize_instance(self.order_item))

        # Assert
        self.order_item.refresh_from_db()
        self.assertIsNotNone(self.order_item.resource)
        self.assertEqual(Resource.States.OK, self.order_item.resource.state)
