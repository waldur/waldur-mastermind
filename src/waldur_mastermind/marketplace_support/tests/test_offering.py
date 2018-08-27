from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models as support_models

from waldur_mastermind.marketplace_support import PLUGIN_NAME


class TemplateOfferingTest(test.APITransactionTestCase):

    def test_create_template_offering(self):
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        self.assertTrue(support_models.OfferingTemplate.objects.filter(name=offering.name).exists())

    def test_not_create_template_offering(self):
        offering = marketplace_factories.OfferingFactory()
        self.assertFalse(support_models.OfferingTemplate.objects.filter(name=offering.name).exists())
