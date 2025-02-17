from django.test import TestCase

from . import fixtures


class InstanceTest(TestCase):
    def test_instance_size_is_sum_of_volumes_size(self):
        fixture = fixtures.OpenStackFixture()
        expected_size = sum(
            fixture.instance.volumes.all().values_list("size", flat=True)
        )
        self.assertEqual(fixture.instance.size, expected_size)
