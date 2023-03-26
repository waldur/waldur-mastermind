from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.callbacks import create_resource_plan_period
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.utils import create_offering_components
from waldur_mastermind.marketplace_openstack import TENANT_TYPE
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.tests import fixtures as openstack_fixtures

TenantQuotas = openstack_models.Tenant.Quotas


@freeze_time('2019-01-01')
class UsagesSynchronizationTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.tenant, plan=marketplace_factories.PlanFactory()
        )
        self.resource.offering.type = TENANT_TYPE
        self.resource.offering.save()
        create_offering_components(self.resource.offering)
        create_resource_plan_period(self.resource)

    def assert_usage_equal(self, name, value):
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages[name], value)
        component_usage = marketplace_models.ComponentUsage.objects.get(
            resource=self.resource, component__type=name
        )
        self.assertEqual(component_usage.usage, value)

    def test_cores_usage_is_synchronized(self):
        self.tenant.set_quota_usage(TenantQuotas.vcpu, 10)
        self.assert_usage_equal('cores', 10)

    def test_ram_usage_is_synchronized(self):
        self.tenant.set_quota_usage(TenantQuotas.ram, 20 * 1024)
        self.assert_usage_equal('ram', 20 * 1024)

    def test_storage_usage_is_synchronized(self):
        self.tenant.set_quota_usage(TenantQuotas.storage, 100 * 1024)
        self.assert_usage_equal('storage', 100 * 1024)
