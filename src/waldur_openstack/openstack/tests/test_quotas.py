from rest_framework import test

from waldur_openstack.openstack.tests import fixtures


class TenantQuotasTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.project = self.fixture.project
        self.customer = self.fixture.customer

    def test_quotas_for_tenant_are_created_for_project_and_customer(self):
        self.assertEqual(self.project.get_quota_usage('vpc_cpu_count'), 0)
        self.assertEqual(self.project.get_quota_usage('vpc_ram_size'), 0)
        self.assertEqual(self.project.get_quota_usage('vpc_storage_size'), 0)
        self.assertEqual(self.project.get_quota_usage('vpc_floating_ip_count'), 0)
        self.assertEqual(self.project.get_quota_usage('vpc_instance_count'), 0)

        self.assertEqual(self.customer.get_quota_usage('vpc_cpu_count'), 0)
        self.assertEqual(self.customer.get_quota_usage('vpc_ram_size'), 0)
        self.assertEqual(self.customer.get_quota_usage('vpc_storage_size'), 0)
        self.assertEqual(self.customer.get_quota_usage('vpc_floating_ip_count'), 0)
        self.assertEqual(self.customer.get_quota_usage('vpc_instance_count'), 0)

    def test_quotas_for_tenant_are_increased_for_project_and_customer(self):
        self.tenant.set_quota_usage('vcpu', 1)
        self.tenant.set_quota_usage('ram', 1024)
        self.tenant.set_quota_usage('storage', 102400)
        self.tenant.set_quota_usage('floating_ip_count', 2)
        self.tenant.set_quota_usage('instances', 1)

        self.assertEqual(self.project.get_quota_usage('vpc_cpu_count'), 1)
        self.assertEqual(self.project.get_quota_usage('vpc_ram_size'), 1024)
        self.assertEqual(self.project.get_quota_usage('vpc_storage_size'), 102400)
        self.assertEqual(self.project.get_quota_usage('vpc_floating_ip_count'), 2)
        self.assertEqual(self.project.get_quota_usage('vpc_instance_count'), 1)

        self.assertEqual(self.customer.get_quota_usage('vpc_cpu_count'), 1)
        self.assertEqual(self.customer.get_quota_usage('vpc_ram_size'), 1024)
        self.assertEqual(self.customer.get_quota_usage('vpc_storage_size'), 102400)
        self.assertEqual(self.customer.get_quota_usage('vpc_floating_ip_count'), 2)
        self.assertEqual(self.customer.get_quota_usage('vpc_instance_count'), 1)
