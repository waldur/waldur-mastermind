from ddt import ddt, data
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status, test

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages.tests import factories as package_factories
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.tests import factories as openstack_factories
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps

from .. import INSTANCE_TYPE, PACKAGE_TYPE, VOLUME_TYPE, utils


class TemplateOfferingTest(test.APITransactionTestCase):
    def test_template_for_plan_is_created(self):
        fixture = package_fixtures.OpenStackFixture()
        offering = marketplace_factories.OfferingFactory(
            type=PACKAGE_TYPE,
            scope=fixture.openstack_service_settings
        )
        plan = marketplace_factories.PlanFactory(offering=offering)
        component = marketplace_models.OfferingComponent.objects.create(
            offering=offering,
            type='ram',
        )
        marketplace_models.PlanComponent.objects.create(
            plan=plan,
            component=component,
            amount=10240,
            price=10,
        )
        plan.refresh_from_db()

        template = plan.scope
        self.assertTrue(isinstance(template, package_models.PackageTemplate))
        self.assertEqual(template.components.get(type='ram').price, 10)

    def test_template_for_plan_is_not_created_if_type_is_invalid(self):
        offering = marketplace_factories.OfferingFactory(type='INVALID')
        plan = marketplace_factories.PlanFactory(offering=offering)
        plan.refresh_from_db()
        self.assertIsNone(plan.scope)

    def test_missing_offerings_are_created(self):
        customer = structure_factories.CustomerFactory()
        category = marketplace_models.Category.objects.create(title='VPC')
        template = package_factories.PackageTemplateFactory()
        marketplace_models.Offering.objects.all().delete()
        utils.create_package_missing_offerings(category, customer)
        offering = marketplace_models.Offering.objects.get(scope=template.service_settings)
        self.assertEqual(template.service_settings.name, offering.name)


class PlanComponentsTest(test.APITransactionTestCase):
    prices = {
        'cores': 10,
        'ram': 100,
        'storage': 1000,
    }
    quotas = prices

    def test_plan_components_are_validated(self):
        response = self.create_offering()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(uuid=response.data['uuid'])
        self.assertEqual(offering.plans.first().components.count(), 3)

    def test_plan_without_components_is_invalid(self):
        response = self.create_offering(False)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('plans' in response.data)

    def test_total_price_is_calculated_from_components(self):
        response = self.create_offering()
        offering = marketplace_models.Offering.objects.get(uuid=response.data['uuid'])
        self.assertEqual(offering.plans.first().unit_price, 10 * 10 + 100 * 100 + 1000 * 1000)

    def create_offering(self, components=True):
        fixture = structure_fixtures.ProjectFixture()
        url = marketplace_factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(fixture.owner)
        payload = {
            'name': 'offering',
            'category': marketplace_factories.CategoryFactory.get_url(),
            'customer': structure_factories.CustomerFactory.get_url(fixture.customer),
            'type': PACKAGE_TYPE,
            'plans': [
                {
                    'name': 'small',
                    'description': 'CPU 1',
                    'unit': UnitPriceMixin.Units.PER_DAY,
                    'unit_price': 1010100,
                }
            ]
        }
        if components:
            payload['plans'][0]['prices'] = self.prices
            payload['plans'][0]['quotas'] = self.quotas
        return self.client.post(url, payload)


@ddt
class OpenStackResourceOfferingTest(test.APITransactionTestCase):
    @data(INSTANCE_TYPE, VOLUME_TYPE)
    def test_offering_is_created_when_tenant_creation_is_completed(self, offering_type):
        tenant = self.trigger_offering_creation()

        offering = marketplace_models.Offering.objects.get(type=offering_type)
        service_settings = offering.scope

        self.assertTrue(isinstance(service_settings, structure_models.ServiceSettings))
        self.assertEqual(service_settings.scope, tenant)

    @data(INSTANCE_TYPE, VOLUME_TYPE)
    def test_offering_is_not_created_if_tenant_is_not_created_via_marketplace(self, offering_type):
        fixture = package_fixtures.OpenStackFixture()
        tenant = openstack_models.Tenant.objects.create(
            service_project_link=fixture.openstack_spl,
            state=openstack_models.Tenant.States.CREATING,
        )

        tenant.set_ok()
        tenant.save()

        self.assertRaises(ObjectDoesNotExist, marketplace_models.Offering.objects.get, type=offering_type)

    @data(INSTANCE_TYPE, VOLUME_TYPE)
    def test_offering_is_archived_when_tenant_is_deleted(self, offering_type):
        tenant = self.trigger_offering_creation()
        tenant.delete()
        offering = marketplace_models.Offering.objects.get(type=offering_type)
        self.assertEqual(offering.state, marketplace_models.Offering.States.ARCHIVED)

    def test_creating_missing_offerings_for_tenants(self):
        tenant = openstack_factories.TenantFactory()
        category = marketplace_factories.CategoryFactory()

        utils.create_missing_offerings(category)
        self.assertEqual(marketplace_models.Offering.objects.all().count(), 2)

        service_settings = self._get_service_settings(tenant)
        self.assertTrue(marketplace_models.Offering.objects.filter(scope=service_settings).exists())

    def test_creating_missing_offerings_for_selected_tenants(self):
        tenant1 = openstack_factories.TenantFactory()
        tenant2 = openstack_factories.TenantFactory()

        category = marketplace_factories.CategoryFactory()
        utils.create_missing_offerings(category, [tenant1.uuid])

        self.assertEqual(marketplace_models.Offering.objects.all().count(), 2)
        self.assertTrue(marketplace_models.Offering.objects.filter(scope=self._get_service_settings(tenant1)).exists())
        self.assertFalse(marketplace_models.Offering.objects.filter(scope=self._get_service_settings(tenant2)).exists())

    def _get_service_settings(self, tenant):
        return structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
        )

    def trigger_offering_creation(self):
        fixture = package_fixtures.OpenStackFixture()
        tenant = openstack_models.Tenant.objects.create(
            service_project_link=fixture.openstack_spl,
            state=openstack_models.Tenant.States.CREATING,
        )
        resource = marketplace_factories.ResourceFactory(scope=tenant)
        marketplace_factories.OrderItemFactory(resource=resource)

        tenant.set_ok()
        tenant.save()
        return tenant
