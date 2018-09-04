from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
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


class PlanComponentsTest(test.APITransactionTestCase):
    components = [
        {
            'type': 'cores',
            'amount': 10,
            'price': 10,
        },
        {
            'type': 'ram',
            'amount': 100,
            'price': 100,
        },
        {
            'type': 'storage',
            'amount': 1000,
            'price': 1000,
        }
    ]

    def test_plan_components_are_validated(self):
        response = self.create_offering(self.components)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        offering = marketplace_models.Offering.objects.get(uuid=response.data['uuid'])
        self.assertEqual(offering.plans.first().components.count(), 3)

    def test_plan_without_components_is_invalid(self):
        response = self.create_offering()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('plans' in response.data)

    def test_total_price_is_calculated_from_components(self):
        response = self.create_offering(self.components)
        offering = marketplace_models.Offering.objects.get(uuid=response.data['uuid'])
        self.assertEqual(offering.plans.first().unit_price, 10 * 10 + 100 * 100 + 1000 * 1000)

    def create_offering(self, components=None):
        fixture = structure_fixtures.ProjectFixture()
        url = marketplace_factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(fixture.owner)
        payload = {
            'name': 'offering',
            'category': marketplace_factories.CategoryFactory.get_url(),
            'customer': structure_factories.CustomerFactory.get_url(fixture.customer),
            'type': PLUGIN_NAME,
            'plans': [
                {
                    'name': 'small',
                    'description': 'CPU 1',
                    'unit': UnitPriceMixin.Units.PER_DAY,
                    'unit_price': 100,
                }
            ]
        }
        if components:
            payload['plans'][0]['components'] = components
        return self.client.post(url, payload)
