from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models as support_models

from waldur_mastermind.marketplace_support import PLUGIN_NAME


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
