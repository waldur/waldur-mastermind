from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.openstack import models
from waldur_openstack.openstack.tests import factories
from waldur_openstack.openstack.tests.fixtures import OpenStackFixture


class StatsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

        self.settings = self.fixture.openstack_service_settings
        self.url = structure_factories.ServiceSettingsFactory.get_url(
            self.settings, 'stats'
        )

    def test_empty_statistics(self):
        empty_stats = {
            'vcpu': -1.0,
            'vcpu_quota': -1.0,
            'vcpu_usage': 0.0,
            'ram': -1.0,
            'ram_quota': -1.0,
            'ram_usage': 0.0,
            'storage': -1.0,
            'storage_quota': -1.0,
            'storage_usage': 0.0,
        }

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(empty_stats, response.data)

    def test_tenant_and_service_statistics_are_combined(self):
        self.settings.set_quota_limit(self.settings.Quotas.openstack_vcpu, 10)
        self.settings.set_quota_usage(self.settings.Quotas.openstack_vcpu, 5)

        self.settings.set_quota_limit(self.settings.Quotas.openstack_ram, 1000)
        self.settings.set_quota_usage(self.settings.Quotas.openstack_ram, 500)

        self.settings.set_quota_limit(self.settings.Quotas.openstack_storage, 10000)
        self.settings.set_quota_usage(self.settings.Quotas.openstack_storage, 5000)

        tenant1 = factories.TenantFactory(
            service_settings=self.settings, project=self.fixture.project
        )
        tenant1.set_quota_limit(models.Tenant.Quotas.vcpu, 7)
        tenant1.set_quota_limit(models.Tenant.Quotas.ram, 700)
        tenant1.set_quota_limit(models.Tenant.Quotas.storage, 7000)

        stats = {
            'vcpu': 10,
            'vcpu_quota': 7,
            'vcpu_usage': 5,
            'ram': 1000,
            'ram_quota': 700,
            'ram_usage': 500,
            'storage': 10000,
            'storage_quota': 7000,
            'storage_usage': 5000,
        }

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(stats, response.data)
