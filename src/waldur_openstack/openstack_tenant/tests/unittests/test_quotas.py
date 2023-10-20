from rest_framework import test

from waldur_openstack.openstack_tenant.tests import fixtures


class TenantQuotasTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.OpenStackTenantFixture()
        self.tenant = self.fixture.tenant

    def test_tenant_quotas_are_synced_with_private_settings_quota(self):
        self.tenant.set_quota_usage('vcpu', 1)
        self.tenant.set_quota_usage('ram', 1024)
        self.tenant.set_quota_usage('storage', 102400)
        self.tenant.set_quota_usage('floating_ip_count', 2)
        self.tenant.set_quota_usage('instances', 1)

        self.assertEqual(self.tenant.get_quota_usage('vcpu'), 1)
        self.assertEqual(self.tenant.get_quota_usage('ram'), 1024)
        self.assertEqual(self.tenant.get_quota_usage('storage'), 102400)
        self.assertEqual(self.tenant.get_quota_usage('floating_ip_count'), 2)
        self.assertEqual(self.tenant.get_quota_usage('instances'), 1)
