from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_packages import PLUGIN_NAME
from waldur_mastermind.marketplace_packages.tests.utils import override_marketplace_packages_settings
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages.tests import factories as package_factories
from waldur_mastermind.packages.tests import fixtures as package_fixtures

from .. import utils


class TemplateOfferingTest(test.APITransactionTestCase):
    def test_template_for_plan_is_created(self):
        fixture = package_fixtures.OpenStackFixture()
        offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME,
            scope=fixture.openstack_service_settings
        )
        plan = marketplace_factories.PlanFactory(offering=offering)
        marketplace_models.PlanComponent.objects.create(
            plan=plan,
            type='ram',
            amount=10240,
            price=10,
        )
        plan.refresh_from_db()

        template = plan.scope
        self.assertTrue(isinstance(template, package_models.PackageTemplate))
        self.assertEqual(plan.components.get(type='ram').price, 10)

    def test_template_for_plan_is_not_created_if_type_is_invalid(self):
        offering = marketplace_factories.OfferingFactory(type='INVALID')
        plan = marketplace_factories.PlanFactory(offering=offering)
        plan.refresh_from_db()
        self.assertIsNone(plan.scope)

    def test_missing_offerings_are_created(self):
        customer = structure_factories.CustomerFactory()
        category = marketplace_models.Category.objects.create(title='VPC')
        with override_marketplace_packages_settings(CUSTOMER_ID=customer.id, CATEGORY_ID=category.id):
            template = package_factories.PackageTemplateFactory()
            marketplace_models.Offering.objects.all().delete()
            utils.create_missing_offerings()
            offering = marketplace_models.Offering.objects.get(scope=template.service_settings)
            self.assertEqual(template.service_settings.name, offering.name)
