import mock
from django.conf import settings
from rest_framework import test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models as support_models

from waldur_mastermind.marketplace_support import PLUGIN_NAME


class SupportOrderTest(test.APITransactionTestCase):
    def setUp(self, **kwargs):
        super(SupportOrderTest, self).setUp(**kwargs)
        support_backend = 'waldur_mastermind.support.backend.atlassian:ServiceDeskBackend'
        settings.WALDUR_SUPPORT['ENABLED'] = True
        settings.WALDUR_SUPPORT['ACTIVE_BACKEND'] = support_backend
        mock_patch = mock.patch('waldur_mastermind.support.backend.get_active_backend')
        self.mock_get_active_backend = mock_patch.start()

    def tearDown(self):
        mock.patch.stopall()

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
        self.client.post(url)
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
        self.client.post(url)
        self.assertFalse(support_models.Offering.objects.filter(name='item_name').exists())
