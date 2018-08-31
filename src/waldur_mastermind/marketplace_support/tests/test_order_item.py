from rest_framework import status

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests import factories as support_factories
from waldur_mastermind.support.tests.base import BaseTest
from waldur_mastermind.marketplace_support import PLUGIN_NAME


class SupportOrderTest(BaseTest):

    def test_create_offering_if_order_item_is_approved(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.user = self.fixture.staff
        self.offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME,
                                                              options={'order': []})

        order_item = marketplace_factories.OrderItemFactory(offering=self.offering,
                                                            attributes={'name': 'item_name', 'description': '{}'})
        url = marketplace_factories.OrderFactory.get_url(order_item.order, 'set_state_executing')

        self.client.force_login(self.user)
        response = self.client.post(url)
        self.assertTrue(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(support_models.Offering.objects.filter(name='item_name').exists())

    def test_not_create_offering_if_marketplace_offering_is_not_support_type(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.user = self.fixture.staff
        self.offering = marketplace_factories.OfferingFactory()

        order_item = marketplace_factories.OrderItemFactory(offering=self.offering,
                                                            attributes={'name': 'item_name', 'description': '{}'})
        url = marketplace_factories.OrderFactory.get_url(order_item.order, 'set_state_executing')

        self.client.force_login(self.user)
        response = self.client.post(url)
        self.assertTrue(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_order_item_set_state_done(self):
        offering = support_factories.OfferingFactory()
        order_item = marketplace_factories.OrderItemFactory(scope=offering)
        order_item.set_state('executing')
        order_item.order.state = marketplace_models.Order.States.EXECUTING
        order_item.order.save()
        offering.state = support_models.Offering.States.OK
        offering.save()
        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)
