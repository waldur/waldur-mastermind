from __future__ import unicode_literals

from rest_framework import test, status

from waldur_core.structure.tests import factories as structure_factories

from . import factories
from .. import models


class StatsTest(test.APITransactionTestCase):

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

        self.service = factories.OpenStackServiceFactory()
        self.settings = self.service.settings
        self.url = structure_factories.ServiceSettingsFactory.get_url(self.settings, 'stats')

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
            'storage_usage': 0.0
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

        link = factories.OpenStackServiceProjectLinkFactory(service=self.service)
        tenant1 = factories.TenantFactory(service_project_link=link)
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
            'storage_usage': 5000
        }

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(stats, response.data)
