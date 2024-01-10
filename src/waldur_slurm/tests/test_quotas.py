from django.test import TestCase

from . import factories, fixtures, utils


@utils.override_plugin_settings(ENABLED=True)
class QuotasTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()

        allocation1 = factories.AllocationFactory(
            service_settings=self.fixture.settings, project=self.fixture.project
        )
        allocation1.cpu_usage = 1000
        allocation1.gpu_usage = 2000
        allocation1.ram_usage = 10000
        allocation1.save()

        allocation2 = factories.AllocationFactory(
            service_settings=self.fixture.settings, project=self.fixture.project
        )
        allocation2.cpu_usage = 5000
        allocation2.gpu_usage = 2000
        allocation2.ram_usage = 50000
        allocation2.save()

        self.expected_cpu_usage = allocation1.cpu_usage + allocation2.cpu_usage
        self.expected_gpu_usage = allocation1.gpu_usage + allocation2.gpu_usage
        self.expected_ram_usage = allocation1.ram_usage + allocation2.ram_usage

    def test_project_quotas_are_updated(self):
        actual_cpu_usage = self.fixture.project.get_quota_usage("nc_cpu_usage")
        self.assertEqual(self.expected_cpu_usage, actual_cpu_usage)

        actual_gpu_usage = self.fixture.project.get_quota_usage("nc_gpu_usage")
        self.assertEqual(self.expected_gpu_usage, actual_gpu_usage)

        actual_ram_usage = self.fixture.project.get_quota_usage("nc_ram_usage")
        self.assertEqual(self.expected_ram_usage, actual_ram_usage)

    def test_customer_quotas_are_updated(self):
        actual_cpu_usage = self.fixture.customer.get_quota_usage("nc_cpu_usage")
        self.assertEqual(self.expected_cpu_usage, actual_cpu_usage)

        actual_gpu_usage = self.fixture.customer.get_quota_usage("nc_gpu_usage")
        self.assertEqual(self.expected_gpu_usage, actual_gpu_usage)

        actual_ram_usage = self.fixture.customer.get_quota_usage("nc_ram_usage")
        self.assertEqual(self.expected_ram_usage, actual_ram_usage)

    def test_allocation_count_is_updated_for_project(self):
        self.assertEqual(self.fixture.project.get_quota_usage("nc_allocation_count"), 2)
        self.assertEqual(
            fixtures.SlurmFixture().project.get_quota_usage("nc_allocation_count"),
            0,
        )
