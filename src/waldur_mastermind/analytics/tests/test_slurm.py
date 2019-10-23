import json

from django.test import TestCase

from waldur_slurm.tests import fixtures

from .. import slurm


class SlurmAnalyticsTest(TestCase):
    def test_total_usage_for_cpu_gpu_ram_is_aggregated(self):
        allocation1 = fixtures.SlurmFixture().allocation
        allocation1.cpu_usage = 100
        allocation1.gpu_usage = 200
        allocation1.ram_usage = 2000
        allocation1.save()

        allocation2 = fixtures.SlurmFixture().allocation
        allocation2.cpu_usage = 500
        allocation2.gpu_usage = 1000
        allocation2.ram_usage = 10000
        allocation2.save()

        expected_points = [
            {
                'measurement': 'slurm_cpu_usage',
                'fields': {
                    'value': 600,
                }
            },
            {
                'measurement': 'slurm_ram_usage',
                'fields': {
                    'value': 12000,
                }
            },
            {
                'measurement': 'slurm_gpu_usage',
                'fields': {
                    'value': 1200,
                }
            },
        ]
        self.assertEquals(json.dumps(slurm.get_usage()), json.dumps(expected_points))

    def test_if_there_are_no_allocations_empty_list_is_returned(self):
        self.assertEqual(slurm.get_usage(), [])
