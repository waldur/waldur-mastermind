from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.tests import factories
from waldur_openstack.tests.fixtures import OpenStackFixture, mock_session
from waldur_openstack.utils import get_external_network, get_external_network_id


class ExternalNetworkApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()
        self.ext_net = factories.ExternalNetworkFactory(settings=self.fixture.settings)
        self.ext_subnet = factories.ExternalSubnetFactory(
            network=self.ext_net,
            cidr="203.0.113.0/24",
            gateway_ip="203.0.113.1",
        )

    def test_staff_can_list_external_networks(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.ExternalNetworkFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) >= 1)

    def test_external_network_includes_subnets(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            factories.ExternalNetworkFactory.get_url(self.ext_net)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["subnets"]), 1)
        self.assertEqual(response.data["subnets"][0]["cidr"], "203.0.113.0/24")
        self.assertEqual(response.data["subnets"][0]["gateway_ip"], "203.0.113.1")

    def test_filter_by_settings(self):
        other_settings = structure_factories.ServiceSettingsFactory(type="OpenStack")
        factories.ExternalNetworkFactory(settings=other_settings)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            factories.ExternalNetworkFactory.get_list_url(),
            {"settings_uuid": self.fixture.settings.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.ext_net.uuid.hex)


class GetExternalNetworkUtilTest(test.APITestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()

    def test_returns_external_network_from_tenant_fk(self):
        ext_net = factories.ExternalNetworkFactory(settings=self.fixture.settings)
        tenant = self.fixture.tenant
        tenant.external_network_ref = ext_net
        tenant.save()
        result = get_external_network(tenant)
        self.assertEqual(result, ext_net)

    def test_returns_external_network_from_customer_openstack_fk(self):
        ext_net = factories.ExternalNetworkFactory(settings=self.fixture.settings)
        models.CustomerOpenStack.objects.create(
            settings=self.fixture.settings,
            customer=self.fixture.customer,
            external_network_id="old-id",
            external_network_ref=ext_net,
        )
        result = get_external_network(self.fixture.tenant)
        self.assertEqual(result, ext_net)

    def test_returns_external_network_from_settings_option(self):
        ext_net = factories.ExternalNetworkFactory(
            settings=self.fixture.settings,
            backend_id="test_network_id",
        )
        result = get_external_network(self.fixture.tenant)
        self.assertEqual(result, ext_net)

    def test_get_external_network_id_returns_backend_id(self):
        ext_net = factories.ExternalNetworkFactory(settings=self.fixture.settings)
        tenant = self.fixture.tenant
        tenant.external_network_ref = ext_net
        tenant.save()
        result = get_external_network_id(tenant)
        self.assertEqual(result, ext_net.backend_id)

    def test_legacy_fallback_to_string_field(self):
        tenant = self.fixture.tenant
        tenant.external_network_id = "legacy-ext-net-id"
        tenant.save()
        # No ExternalNetwork record exists for this ID or FK
        result = get_external_network_id(tenant)
        self.assertEqual(result, "legacy-ext-net-id")


class PullExternalNetworksTest(test.APITestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()
        self.neutron_patcher = mock.patch("waldur_openstack.backend.get_neutron_client")
        self.mock_neutron = self.neutron_patcher.start()
        mock_session()

    def tearDown(self):
        mock.patch.stopall()

    def test_pull_creates_networks_and_subnets(self):
        mock_neutron_client = mock.MagicMock()
        self.mock_neutron.return_value = mock_neutron_client
        mock_neutron_client.list_networks.return_value = {
            "networks": [
                {
                    "id": "ext-net-1",
                    "name": "public",
                    "shared": True,
                    "is_default": True,
                    "status": "ACTIVE",
                    "description": "Public network",
                }
            ]
        }
        mock_neutron_client.list_subnets.return_value = {
            "subnets": [
                {
                    "id": "ext-subnet-1",
                    "name": "public-subnet",
                    "cidr": "192.168.1.0/24",
                    "gateway_ip": "192.168.1.1",
                    "ip_version": 4,
                    "enable_dhcp": False,
                    "allocation_pools": [
                        {"start": "192.168.1.2", "end": "192.168.1.254"}
                    ],
                    "dns_nameservers": ["8.8.8.8"],
                    "description": "Public subnet",
                }
            ]
        }

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_external_networks()

        ext_net = models.ExternalNetwork.objects.get(
            settings=self.fixture.settings, backend_id="ext-net-1"
        )
        self.assertEqual(ext_net.name, "public")
        self.assertTrue(ext_net.is_shared)
        self.assertTrue(ext_net.is_default)
        self.assertEqual(ext_net.status, "ACTIVE")

        ext_subnet = models.ExternalSubnet.objects.get(
            network=ext_net, backend_id="ext-subnet-1"
        )
        self.assertEqual(ext_subnet.name, "public-subnet")
        self.assertEqual(ext_subnet.cidr, "192.168.1.0/24")
        self.assertEqual(ext_subnet.gateway_ip, "192.168.1.1")
        self.assertFalse(ext_subnet.enable_dhcp)

    def test_pull_removes_stale_networks(self):
        stale_net = factories.ExternalNetworkFactory(
            settings=self.fixture.settings, backend_id="stale-net"
        )

        mock_neutron_client = mock.MagicMock()
        self.mock_neutron.return_value = mock_neutron_client
        mock_neutron_client.list_networks.return_value = {"networks": []}

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_external_networks()

        self.assertFalse(
            models.ExternalNetwork.objects.filter(pk=stale_net.pk).exists()
        )
