from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.tests import fixtures as openstack_fixtures

TenantQuotas = openstack_models.Tenant.Quotas


class UsagesSynchronizationTest(test.APITransactionTestCase):
    def setUp(self):
        super(UsagesSynchronizationTest, self).setUp()
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.resource = marketplace_factories.ResourceFactory(scope=self.tenant)

    def assert_usage_equal(self, name, value):
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages[name], value)

    def test_cores_usage_is_synchronized(self):
        self.tenant.set_quota_usage(TenantQuotas.vcpu, 10)
        self.assert_usage_equal('cores', 10)

    def test_ram_usage_is_synchronized(self):
        self.tenant.set_quota_usage(TenantQuotas.ram, 20 * 1024)
        self.assert_usage_equal('ram', 20 * 1024)

    def test_storage_usage_is_synchronized(self):
        self.tenant.set_quota_usage(TenantQuotas.storage, 100 * 1024)
        self.assert_usage_equal('storage', 100 * 1024)
