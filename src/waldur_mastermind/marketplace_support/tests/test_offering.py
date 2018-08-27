from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models as support_models

from waldur_mastermind.marketplace_support import PLUGIN_NAME


class TemplateOfferingTest(test.APITransactionTestCase):

    def test_create_template_offering(self):
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        offering.refresh_from_db()
        template = support_models.OfferingTemplate.objects.get(name=offering.name)
        self.assertTrue(offering.scope, template)

    def test_not_create_template_offering(self):
        offering = marketplace_factories.OfferingFactory()
        offering.refresh_from_db()
        self.assertIsNone(offering.scope)
