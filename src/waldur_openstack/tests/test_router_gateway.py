"""Tests for connecting a router to an external network."""

from unittest import mock

from rest_framework import test

from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.tests import fixtures


class ConnectNetworkToRouterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.backend = OpenStackBackend(self.tenant.service_settings)
        self.router = {"id": "router-123", "name": "test-router"}

    def _connect(self, mock_neutron_client, external_gateway_info):
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client
        mock_client.add_gateway_router.return_value = {
            "router": {
                "id": self.router["id"],
                "name": self.router["name"],
                "external_gateway_info": external_gateway_info,
            }
        }
        self.backend._connect_network_to_router(
            self.tenant,
            self.router,
            external=True,
            network_id="ext-net-456",
        )
        return mock_client

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_gateway_with_allocated_address_is_connected(
        self, mock_neutron_client, mock_session
    ):
        mock_client = self._connect(
            mock_neutron_client,
            {
                "network_id": "ext-net-456",
                "external_fixed_ips": [
                    {"ip_address": "203.0.113.10", "subnet_id": "subnet-789"}
                ],
            },
        )

        mock_client.add_gateway_router.assert_called_once_with(
            "router-123", {"network_id": "ext-net-456"}
        )

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_gateway_without_allocated_address_does_not_raise(
        self, mock_neutron_client, mock_session
    ):
        """An external network with no subnets yields no address, and that is legal.

        Neutron's Subnet.network_has_no_subnet calls this case "not an error";
        _create_router_gw_port logs "No IPs available for external network" and
        returns the router with an empty external_fixed_ips. Reading [0] off it
        to build a log message used to raise IndexError and fail provisioning.
        """
        mock_client = self._connect(
            mock_neutron_client,
            {"network_id": "ext-net-456", "external_fixed_ips": []},
        )

        mock_client.add_gateway_router.assert_called_once_with(
            "router-123", {"network_id": "ext-net-456"}
        )

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_gateway_without_gateway_info_does_not_raise(
        self, mock_neutron_client, mock_session
    ):
        """A response carrying no external_gateway_info at all is tolerated too."""
        mock_client = self._connect(mock_neutron_client, None)

        mock_client.add_gateway_router.assert_called_once_with(
            "router-123", {"network_id": "ext-net-456"}
        )
