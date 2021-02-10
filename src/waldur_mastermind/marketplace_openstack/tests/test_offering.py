import uuid
from unittest import mock

from ddt import data, ddt
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.management.commands.load_categories import (
    load_category,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import (
    STORAGE_MODE_DYNAMIC,
    STORAGE_MODE_FIXED,
)
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.tests import fixtures as openstack_fixtures
from waldur_openstack.openstack_base.tests.fixtures import OpenStackFixture

from .. import INSTANCE_TYPE, TENANT_TYPE, VOLUME_TYPE
from .utils import BaseOpenStackTest, override_plugin_settings


class VpcExternalFilterTest(BaseOpenStackTest):
    def setUp(self):
        super(VpcExternalFilterTest, self).setUp()
        self.fixture = OpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(
            category=self.tenant_category
        )
        self.url = marketplace_factories.OfferingFactory.get_list_url()

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_staff_can_see_vpc_offering(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(1, len(response.data))

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_other_users_can_not_see_vpc_offering_if_feature_is_enabled(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(0, len(response.data))

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=False)
    def test_other_users_can_see_vpc_offering_if_feature_is_disabled(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(1, len(response.data))


class PlanComponentsTest(test.APITransactionTestCase):
    prices = {
        'cores': 10,
        'ram': 100,
        'storage': 1000,
    }
    quotas = prices

    def setUp(self):
        super(PlanComponentsTest, self).setUp()
        self.category = load_category('vpc')

    def test_plan_components_are_validated(self):
        response = self.create_offering()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(uuid=response.data['uuid'])
        self.assertEqual(offering.plans.first().components.count(), 3)

    def test_plan_components_have_parent(self):
        response = self.create_offering()
        offering = marketplace_models.Offering.objects.get(uuid=response.data['uuid'])
        self.assertEqual(3, offering.components.exclude(parent=None).count())

    def test_plan_without_components_is_valid(self):
        response = self.create_offering(False)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_total_price_is_calculated_from_components(self):
        response = self.create_offering()
        offering = marketplace_models.Offering.objects.get(uuid=response.data['uuid'])
        self.assertEqual(
            offering.plans.first().unit_price, 10 * 10 + 100 * 100 + 1000 * 1000
        )

    def create_offering(self, components=True):
        fixture = structure_fixtures.ProjectFixture()
        url = marketplace_factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(fixture.owner)
        payload = {
            'name': 'offering',
            'category': marketplace_factories.CategoryFactory.get_url(self.category),
            'customer': structure_factories.CustomerFactory.get_url(fixture.customer),
            'type': TENANT_TYPE,
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
            ],
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
        self.assertEqual(
            offering.customer, tenant.service_project_link.project.customer
        )

    @data(INSTANCE_TYPE, VOLUME_TYPE)
    @override_plugin_settings(AUTOMATICALLY_CREATE_PRIVATE_OFFERING=False)
    def test_offering_is_not_created_if_feature_is_disabled(self, offering_type):
        self.trigger_offering_creation()

        self.assertRaises(
            marketplace_models.Offering.DoesNotExist,
            lambda: marketplace_models.Offering.objects.get(type=offering_type),
        )

    @data(INSTANCE_TYPE, VOLUME_TYPE)
    def test_offering_is_not_created_if_tenant_is_not_created_via_marketplace(
        self, offering_type
    ):
        fixture = OpenStackFixture()
        tenant = openstack_models.Tenant.objects.create(
            service_project_link=fixture.openstack_spl,
            state=openstack_models.Tenant.States.CREATING,
        )

        tenant.set_ok()
        tenant.save()

        self.assertRaises(
            ObjectDoesNotExist,
            marketplace_models.Offering.objects.get,
            type=offering_type,
        )

    @data(INSTANCE_TYPE, VOLUME_TYPE)
    def test_offering_is_archived_when_tenant_is_deleted(self, offering_type):
        tenant = self.trigger_offering_creation()
        tenant.delete()
        offering = marketplace_models.Offering.objects.get(type=offering_type)
        self.assertEqual(offering.state, marketplace_models.Offering.States.ARCHIVED)

    def trigger_offering_creation(self):
        fixture = OpenStackFixture()
        tenant = openstack_models.Tenant.objects.create(
            service_project_link=fixture.openstack_spl,
            state=openstack_models.Tenant.States.CREATING,
        )
        resource = marketplace_factories.ResourceFactory(scope=tenant)
        marketplace_factories.OrderItemFactory(resource=resource)

        tenant.set_ok()
        tenant.save()
        return tenant


class OfferingComponentForVolumeTypeTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type=TENANT_TYPE, scope=self.fixture.openstack_service_settings
        )
        self.volume_type = self.fixture.volume_type

    def test_offering_component_for_volume_type_is_created(self):
        component = marketplace_models.OfferingComponent.objects.get(
            scope=self.volume_type
        )
        self.assertEqual(component.offering, self.offering)
        self.assertEqual(
            component.billing_type,
            marketplace_models.OfferingComponent.BillingTypes.FIXED,
        )
        self.assertEqual(component.name, 'Storage (%s)' % self.volume_type.name)
        self.assertEqual(component.type, 'gigabytes_' + self.volume_type.name)

    def test_offering_component_name_is_updated(self):
        self.volume_type.name = 'new name'
        self.volume_type.save()
        component = marketplace_models.OfferingComponent.objects.get(
            scope=self.volume_type
        )
        self.assertEqual(component.name, 'Storage (%s)' % self.volume_type.name)

    def test_offering_component_is_deleted(self):
        self.volume_type.delete()
        self.assertRaises(
            marketplace_models.OfferingComponent.DoesNotExist,
            marketplace_models.OfferingComponent.objects.get,
            scope=self.volume_type,
        )

    def test_switch_from_fixed_to_dynamic_billing(self):
        self.offering.plugin_options = {'storage_mode': STORAGE_MODE_FIXED}
        url = marketplace_factories.OfferingFactory.get_url(self.offering)
        new_options = {
            'plugin_options': {'storage_mode': STORAGE_MODE_DYNAMIC},
            'plans': [
                {
                    'name': 'small',
                    'description': 'CPU 1',
                    'prices': {'gigabytes_' + self.volume_type.name: 10},
                }
            ],
        }

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(url, new_options)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options['storage_mode'], STORAGE_MODE_DYNAMIC
        )

    def test_switch_from_dynamic_to_fixed_billing(self):
        self.offering.plugin_options = {'storage_mode': STORAGE_MODE_DYNAMIC}
        url = marketplace_factories.OfferingFactory.get_url(self.offering)
        new_options = {'plugin_options': {'storage_mode': STORAGE_MODE_FIXED}}

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(url, new_options)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options['storage_mode'], STORAGE_MODE_FIXED
        )
