from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_packages.tests.utils import override_marketplace_packages_settings
from waldur_mastermind.packages.tests import factories as package_factories

from .. import utils


class TemplateOfferingTest(test.APITransactionTestCase):
    def setUp(self):
        super(TemplateOfferingTest, self).setUp()
        self.customer = structure_factories.CustomerFactory()
        self.category = marketplace_models.Category.objects.create(title='VPC')
        self.decorator = override_marketplace_packages_settings(
            CUSTOMER_ID=self.customer.id,
            CATEGORY_ID=self.category.id
        )
        self.decorator.enable()

    def tearDown(self):
        super(TemplateOfferingTest, self).tearDown()
        self.decorator.disable()

    def test_offering_for_template_is_created(self):
        template = package_factories.PackageTemplateFactory()
        offering = marketplace_models.Offering.objects.get(scope=template.service_settings)
        self.assertEqual(template.service_settings.name, offering.name)

    def test_offering_is_updated(self):
        template = package_factories.PackageTemplateFactory()
        template.name = 'New VPC name'
        template.save()
        offering = marketplace_models.Offering.objects.get(scope=template.service_settings)
        self.assertEqual(template.service_settings.name, offering.name)

    def test_plan_is_updated(self):
        template = package_factories.PackageTemplateFactory()
        template.archived = True
        template.save()
        plan = marketplace_models.Plan.objects.get(scope=template)
        self.assertTrue(plan.archived)

    def test_missing_offerings_are_created(self):
        template = package_factories.PackageTemplateFactory()
        marketplace_models.Offering.objects.all().delete()
        utils.create_missing_offerings()
        offering = marketplace_models.Offering.objects.get(scope=template.service_settings)
        self.assertEqual(template.service_settings.name, offering.name)
