from rest_framework import test

from waldur_openstack.openstack_tenant.tests.factories import InstanceFactory
from waldur_openstack.openstack_tenant.tests.fixtures import OpenStackTenantFixture
from waldur_zabbix import models
from waldur_zabbix.tests import factories


class SerializerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = OpenStackTenantFixture()
        self.vm = self.fixture.instance
        self.host = factories.HostFactory(scope=self.vm, state=models.Host.States.OK)
        self.client.force_authenticate(self.fixture.owner)
        self.url = InstanceFactory.get_url(self.vm)

    def test_zabbix_host_is_rendered_for_monitored_virtual_machine(self):
        response = self.client.get(self.url)
        self.assertEqual(response.data['zabbix_host']['state'], 'OK')
        self.assertEqual(response.data['zabbix_host']['uuid'], self.host.uuid.hex)

    def test_zabbix_host_is_not_rendered_if_vm_is_not_monitored_yet(self):
        self.host.delete()
        response = self.client.get(self.url)
        self.assertIsNone(response.data['zabbix_host'])
