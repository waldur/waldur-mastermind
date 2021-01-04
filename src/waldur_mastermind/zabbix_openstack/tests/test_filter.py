from django.urls import reverse
from rest_framework import status, test

from waldur_openstack.openstack_tenant.tests.factories import (
    InstanceFactory,
    InternalIPFactory,
)
from waldur_openstack.openstack_tenant.tests.fixtures import OpenStackTenantFixture
from waldur_zabbix.tests import factories


class ZabbixServiceProjectLinkResourceFilterBackendTest(test.APITransactionTestCase):
    def setUp(self):
        fixture = OpenStackTenantFixture()
        project = fixture.project

        server_vm = fixture.instance
        internal_ip = InternalIPFactory.create(
            instance=server_vm,
            fixed_ips=[
                {'ip_address': '10.0.10.2', 'subnet_id': fixture.subnet.backend_id}
            ],
        )
        settings = factories.ServiceSettingsFactory(
            customer=fixture.customer, scope=server_vm
        )
        service = factories.ZabbixServiceFactory(
            customer=fixture.customer, settings=settings
        )
        valid_link = factories.ZabbixServiceProjectLinkFactory(
            service=service, project=project
        )
        valid_link_url = factories.ZabbixServiceProjectLinkFactory.get_url(valid_link)

        # This SPL should not be present in output
        factories.ZabbixServiceProjectLinkFactory(project=project)

        agent_vm = InstanceFactory(service_project_link=fixture.spl)
        agent_vm_url = InstanceFactory.get_url(agent_vm)

        self.fixture = fixture
        self.internal_ip = internal_ip
        self.list_url = reverse('zabbix-openstack-links-list')
        self.agent_vm = agent_vm
        self.agent_vm_url = agent_vm_url
        self.valid_link_url = valid_link_url
        self.settings = settings
        self.client.force_authenticate(self.fixture.owner)

    def test_positive_case(self):
        query = {'resource': self.agent_vm_url}
        response = self.client.get(self.list_url, query)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['url'], self.valid_link_url)
        self.assertEqual(
            response.data[0]['internal_ip'], self.internal_ip.fixed_ips[0]['ip_address']
        )
        self.assertEqual(response.data[0]['service_settings_uuid'], self.settings.uuid)

    def test_if_server_vm_is_missing_queryset_is_empty(self):
        query = {'resource': self.agent_vm_url}
        self.fixture.instance.delete()
        response = self.client.get(self.list_url, query)
        self.assertEqual(len(response.data), 0)

    def test_if_agent_vm_is_missing_queryset_is_empty(self):
        self.agent_vm.delete()
        query = {'resource': self.agent_vm_url}
        response = self.client.get(self.list_url, query)
        self.assertEqual(len(response.data), 0)

    def test_if_scope_is_missing_queryset_is_empty(self):
        self.settings.scope = None
        self.settings.save()
        query = {'resource': self.agent_vm_url}
        response = self.client.get(self.list_url, query)
        self.assertEqual(len(response.data), 0)
