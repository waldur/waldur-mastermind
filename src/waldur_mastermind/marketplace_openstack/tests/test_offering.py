import uuid

from ddt import ddt, data
from django.core.exceptions import ObjectDoesNotExist
import mock
from rest_framework import status, test

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import RAM_TYPE, CORES_TYPE, STORAGE_TYPE
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_openstack.openstack import models as openstack_models

from .. import INSTANCE_TYPE, PACKAGE_TYPE, VOLUME_TYPE
from .utils import BaseOpenStackTest


class TemplateOfferingTest(BaseOpenStackTest):
    def test_template_for_plan_is_created(self):
        fixture = package_fixtures.OpenStackFixture()
        offering = marketplace_factories.OfferingFactory(
            type=PACKAGE_TYPE,
            scope=fixture.openstack_service_settings
        )
        plan = marketplace_factories.PlanFactory(offering=offering)
        ram_component = marketplace_models.OfferingComponent.objects.create(
            offering=offering,
            type=RAM_TYPE,
        )
        marketplace_models.PlanComponent.objects.create(
            plan=plan,
            component=ram_component,
            amount=20,
            price=10,
        )

        cores_component = marketplace_models.OfferingComponent.objects.create(
            offering=offering,
            type=CORES_TYPE,
        )
        marketplace_models.PlanComponent.objects.create(
            plan=plan,
            component=cores_component,
            amount=10,
            price=3,
        )

        storage_component = marketplace_models.OfferingComponent.objects.create(
            offering=offering,
            type=STORAGE_TYPE,
        )
        marketplace_models.PlanComponent.objects.create(
            plan=plan,
            component=storage_component,
            amount=100,
            price=1,
        )
        plan.refresh_from_db()

        template = plan.scope
        self.assertTrue(isinstance(template, package_models.PackageTemplate))

        template_ram_component = template.components.get(type=RAM_TYPE)
        template_cores_component = template.components.get(type=CORES_TYPE)
        template_storage_component = template.components.get(type=STORAGE_TYPE)

        self.assertEqual(template_ram_component.amount, 20 * 1024)
        self.assertEqual(template_ram_component.price, 10.0 / 1024)

        self.assertEqual(template_cores_component.amount, 10)
        self.assertEqual(template_cores_component.price, 3)

        self.assertEqual(template_storage_component.amount, 100 * 1024)
        self.assertEqual(template_storage_component.price, 1.0 / 1024)

    def test_template_for_plan_is_not_created_if_type_is_invalid(self):
        offering = marketplace_factories.OfferingFactory(type='INVALID')
        plan = marketplace_factories.PlanFactory(offering=offering)
        plan.refresh_from_db()
        self.assertIsNone(plan.scope)


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
            'service_attributes': {
                'backend_url': 'http://example.com/',
                'username': 'root',
                'password': 'secret',
                'tenant_name': 'admin',
                'external_network_id': uuid.uuid4(),
            },
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
        with mock.patch('waldur_core.structure.models.ServiceSettings.get_backend'):
            return self.client.post(url, payload)


@ddt
class OpenStackResourceOfferingTest(BaseOpenStackTest):
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
