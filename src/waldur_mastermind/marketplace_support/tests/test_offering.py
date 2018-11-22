from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests import factories as support_factories
from waldur_mastermind.support.tests.base import BaseTest


class OfferingTemplateCreateTest(test.APITransactionTestCase):

    def test_offering_template_is_created_for_valid_type(self):
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        offering.refresh_from_db()
        template = support_models.OfferingTemplate.objects.get(name=offering.name)
        self.assertTrue(offering.scope, template)

    def test_offering_template_is_not_created_for_invalid_type(self):
        offering = marketplace_factories.OfferingFactory()
        offering.refresh_from_db()
        self.assertIsNone(offering.scope)


class SupportOfferingTest(BaseTest):
    def test_offering_set_state_done(self):
        offering = support_factories.OfferingFactory()
        resource = marketplace_factories.ResourceFactory(scope=offering)
        order_item = marketplace_factories.OrderItemFactory(resource=resource)
        order_item.set_state_executing()
        order_item.save()

        order_item.order.state = marketplace_models.Order.States.EXECUTING
        order_item.order.save()
        offering.issue.status = 'Completed'
        offering.issue.resolution = 'Done'
        offering.issue.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)
        offering.refresh_from_db()
        self.assertEqual(offering.state, offering.States.OK)
