from rest_framework import test

from . import fixtures


class TenantQuotasTest(test.APITransactionTestCase):

    def setUp(self):
        super(TenantQuotasTest, self).setUp()
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.project = self.fixture.project
        self.customer = self.fixture.customer

    def test_quotas_for_tenant_are_created_for_project_and_customer(self):
        self.assertEqual(self.project.quotas.get(name='vpc_cpu_count').usage, 0)
        self.assertEqual(self.project.quotas.get(name='vpc_ram_size').usage, 0)
        self.assertEqual(self.project.quotas.get(name='vpc_storage_size').usage, 0)
        self.assertEqual(self.project.quotas.get(name='vpc_floating_ip_count').usage, 0)

        self.assertEqual(self.customer.quotas.get(name='vpc_cpu_count').usage, 0)
        self.assertEqual(self.customer.quotas.get(name='vpc_ram_size').usage, 0)
        self.assertEqual(self.customer.quotas.get(name='vpc_storage_size').usage, 0)
        self.assertEqual(self.customer.quotas.get(name='vpc_floating_ip_count').usage, 0)

    def test_quotas_for_tenant_are_increased_for_project_and_customer(self):
        self.tenant.set_quota_usage('vcpu', 1)
        self.tenant.set_quota_usage('ram', 1024)
        self.tenant.set_quota_usage('storage', 102400)
        self.tenant.set_quota_usage('floating_ip_count', 2)

        self.assertEqual(self.project.quotas.get(name='vpc_cpu_count').usage, 1)
        self.assertEqual(self.project.quotas.get(name='vpc_ram_size').usage, 1024)
        self.assertEqual(self.project.quotas.get(name='vpc_storage_size').usage, 102400)
        self.assertEqual(self.project.quotas.get(name='vpc_floating_ip_count').usage, 2)

        self.assertEqual(self.customer.quotas.get(name='vpc_cpu_count').usage, 1)
        self.assertEqual(self.customer.quotas.get(name='vpc_ram_size').usage, 1024)
        self.assertEqual(self.customer.quotas.get(name='vpc_storage_size').usage, 102400)
        self.assertEqual(self.customer.quotas.get(name='vpc_floating_ip_count').usage, 2)
