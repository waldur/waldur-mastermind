from unittest import mock

from django.core import mail
from django.test import override_settings
from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tasks import approve_order
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

    @override_settings(
        task_always_eager=True,
    )
    @mock.patch('waldur_mastermind.marketplace.utils.process_order_item')
    def test_order_is_processed_when_plugin_option_is_enabled(self, process_order_item):
        self.offering.plugin_options = {'auto_approve_remote_orders': True}
        self.offering.save()

        approve_order(self.order, self.fixture.offering_owner)

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

        approve_order(self.order, self.fixture.offering_owner)

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
        approve_order(self.order, self.fixture.offering_owner)

        self.order.refresh_from_db()
        self.assertEqual(marketplace_models.Order.States.EXECUTING, self.order.state)

        self.order_item.refresh_from_db()
        self.assertEqual(
            marketplace_models.OrderItem.States.PENDING, self.order_item.state
        )

        process_order_item.assert_not_called()

        self.assertEqual(len(mail.outbox), 1)
