from django.test import TestCase

from . import factories, fixtures


class QuotasTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()

        allocation1 = factories.AllocationFactory(service_project_link=self.fixture.spl)
        allocation1.cpu_usage = 1000
        allocation1.gpu_usage = 2000
        allocation1.ram_usage = 10000
        allocation1.save()

        allocation2 = factories.AllocationFactory(service_project_link=self.fixture.spl)
        allocation2.cpu_usage = 5000
        allocation2.gpu_usage = 2000
        allocation2.ram_usage = 50000
        allocation2.save()

        self.expected_cpu_usage = allocation1.cpu_usage + allocation2.cpu_usage
        self.expected_gpu_usage = allocation1.gpu_usage + allocation2.gpu_usage
        self.expected_ram_usage = allocation1.ram_usage + allocation2.ram_usage

    def test_project_quotas_are_updated(self):
        actual_cpu_usage = self.fixture.project.quotas.get(name='nc_cpu_usage').usage
        self.assertEqual(self.expected_cpu_usage, actual_cpu_usage)

        actual_gpu_usage = self.fixture.project.quotas.get(name='nc_gpu_usage').usage
        self.assertEqual(self.expected_gpu_usage, actual_gpu_usage)

        actual_ram_usage = self.fixture.project.quotas.get(name='nc_ram_usage').usage
        self.assertEqual(self.expected_ram_usage, actual_ram_usage)

    def test_customer_quotas_are_updated(self):
        actual_cpu_usage = self.fixture.customer.quotas.get(name='nc_cpu_usage').usage
        self.assertEqual(self.expected_cpu_usage, actual_cpu_usage)

        actual_gpu_usage = self.fixture.customer.quotas.get(name='nc_gpu_usage').usage
        self.assertEqual(self.expected_gpu_usage, actual_gpu_usage)

        actual_ram_usage = self.fixture.customer.quotas.get(name='nc_ram_usage').usage
        self.assertEqual(self.expected_ram_usage, actual_ram_usage)

    def test_allocation_count_is_updated_for_project(self):
        self.assertEqual(self.fixture.project.quotas.get(name='nc_allocation_count').usage, 2)
        self.assertEqual(fixtures.SlurmFixture().project.quotas.get(name='nc_allocation_count').usage, 0)
