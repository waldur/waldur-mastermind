from __future__ import unicode_literals

from ddt import ddt, data
from rest_framework import status, test

from . import factories, fixtures


@ddt
class AllocationUsageGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.usage = self.fixture.allocation_usage

    @data('staff', 'owner', 'manager', 'admin')
    def test_authorized_user_can_get_allocation_usage(self, username):
        self.client.force_login(getattr(self.fixture, username))
        response = self.client.get(factories.AllocationUsageFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['allocation'], factories.AllocationFactory.get_url(self.usage.allocation))

        self.assertEqual(response.data[0]['cpu_usage'], self.usage.cpu_usage)
        self.assertEqual(response.data[0]['gpu_usage'], self.usage.gpu_usage)
        self.assertEqual(response.data[0]['ram_usage'], self.usage.ram_usage)

    @data('user')
    def test_unauthorized_user_can_not_get_allocation_usage(self, username):
        self.client.force_login(getattr(self.fixture, username))
        response = self.client.get(factories.AllocationUsageFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)
