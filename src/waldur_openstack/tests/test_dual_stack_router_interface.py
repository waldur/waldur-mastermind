"""Attaching each subnet of a dual-stack network to the router.

A network can carry an IPv4 and an IPv6 subnet side by side. The router gets
one interface per subnet, so whether a subnet still needs attaching is a
question about that subnet, not about the network: a router interface on the
IPv4 subnet says nothing about the IPv6 one. Deciding per network left the
second subnet unrouted while it was recorded as connected.
"""

from unittest import mock

from rest_framework import test

from waldur_openstack.backend import OpenStackBackend

from . import factories, fixtures

ROUTER_BACKEND_ID = "router-backend-id"
NETWORK_BACKEND_ID = "network-backend-id"
V4_SUBNET_BACKEND_ID = "v4-subnet-backend-id"
V6_SUBNET_BACKEND_ID = "v6-subnet-backend-id"


def interface_port(*fixed_ips, device_owner="network:router_interface"):
    return {
        "id": "port-" + "-".join(subnet_id for subnet_id, _ in fixed_ips),
        "network_id": NETWORK_BACKEND_ID,
        "device_id": ROUTER_BACKEND_ID,
        "device_owner": device_owner,
        "fixed_ips": [
            {"subnet_id": subnet_id, "ip_address": ip_address}
            for subnet_id, ip_address in fixed_ips
        ],
    }


V4_INTERFACE = (V4_SUBNET_BACKEND_ID, "192.168.42.1")
V6_INTERFACE = (V6_SUBNET_BACKEND_ID, "2001:db8:42::1")


class ConnectDualStackSubnetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.tenant.backend_id = "tenant-backend-id"
        self.tenant.save()
        self.network = factories.NetworkFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id=NETWORK_BACKEND_ID,
        )
        self.v6_subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id=V6_SUBNET_BACKEND_ID,
            cidr="2001:db8:42::/64",
            ip_version=6,
            is_connected=False,
        )
        self.backend = OpenStackBackend(self.tenant.service_settings)

    def _connect(self, subnet, ports, gateway_ip="2001:db8:42::1"):
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_routers.return_value = {
                "routers": [{"id": ROUTER_BACKEND_ID, "name": "the-router"}]
            }
            client.show_subnet.return_value = {
                "subnet": {"id": subnet.backend_id, "gateway_ip": gateway_ip}
            }
            client.list_ports.return_value = {"ports": ports}
            router_backend_id = self.backend.connect_subnet(subnet)
        subnet.refresh_from_db()
        return client, router_backend_id

    def test_second_subnet_is_attached_next_to_an_attached_first_one(self):
        """The regression: the IPv4 interface on the same network made the
        IPv6 subnet look attached already."""
        client, router_backend_id = self._connect(
            self.v6_subnet, [interface_port(V4_INTERFACE)]
        )

        client.add_interface_router.assert_called_once_with(
            ROUTER_BACKEND_ID, {"subnet_id": V6_SUBNET_BACKEND_ID}
        )
        self.assertTrue(self.v6_subnet.is_connected)
        self.assertEqual(router_backend_id, ROUTER_BACKEND_ID)

    def test_a_subnet_the_router_already_has_an_interface_on_is_not_re_added(self):
        """Neutron refuses a second interface on the same subnet, which would
        fail the whole connect."""
        client, router_backend_id = self._connect(
            self.v6_subnet,
            [interface_port(V4_INTERFACE), interface_port(V6_INTERFACE)],
        )

        client.add_interface_router.assert_not_called()
        self.assertTrue(self.v6_subnet.is_connected)
        self.assertEqual(router_backend_id, ROUTER_BACKEND_ID)

    def test_one_port_holding_addresses_in_both_subnets_counts_for_either(self):
        """Neutron may add an IPv6 subnet to the router's existing port on the
        network rather than create a new one, so a port can carry fixed IPs of
        several subnets."""
        client, _ = self._connect(
            self.v6_subnet, [interface_port(V4_INTERFACE, V6_INTERFACE)]
        )

        client.add_interface_router.assert_not_called()
        self.assertTrue(self.v6_subnet.is_connected)

    def test_a_port_that_is_not_a_router_interface_does_not_count(self):
        client, _ = self._connect(
            self.v6_subnet,
            [interface_port(V6_INTERFACE, device_owner="network:dhcp")],
        )

        client.add_interface_router.assert_called_once_with(
            ROUTER_BACKEND_ID, {"subnet_id": V6_SUBNET_BACKEND_ID}
        )

    def test_a_single_subnet_network_is_attached_as_before(self):
        v4_subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id=V4_SUBNET_BACKEND_ID,
            cidr="192.168.42.0/24",
            is_connected=False,
        )

        client, router_backend_id = self._connect(
            v4_subnet, [], gateway_ip="192.168.42.1"
        )

        client.add_interface_router.assert_called_once_with(
            ROUTER_BACKEND_ID, {"subnet_id": V4_SUBNET_BACKEND_ID}
        )
        self.assertTrue(v4_subnet.is_connected)
        self.assertEqual(router_backend_id, ROUTER_BACKEND_ID)

    def test_a_subnet_without_a_gateway_is_not_reported_as_connected(self):
        """Neutron cannot put a router interface on it, so nothing is attached,
        and is_connected has to say so rather than claim the router holds it."""
        self.v6_subnet.is_connected = True
        self.v6_subnet.save()

        client, router_backend_id = self._connect(self.v6_subnet, [], gateway_ip=None)

        client.add_interface_router.assert_not_called()
        self.assertFalse(self.v6_subnet.is_connected)
        # There is no interface for create_subnet to import.
        self.assertIsNone(router_backend_id)
