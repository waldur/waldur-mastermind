"""`SubNet.is_connected` has to survive a pull.

`pull_subnets` built the imported row with `_backend_subnet_to_subnet`, which
never passed `is_connected`, so the model default -- `True` -- applied. The field
is in `SubNet.get_backend_fields()`, so `update_pulled_fields` then wrote that
default back over reality: every tenant pull reported every subnet as connected,
whether or not a router held it, and a subnet disconnected through Waldur
reported itself connected again within two hours.
"""

from unittest import mock

from rest_framework import test

from waldur_core.core.enums import CoreStates
from waldur_openstack.backend import OpenStackBackend

from . import factories, fixtures

NETWORK_BACKEND_ID = "network-backend-id"
SUBNET_BACKEND_ID = "subnet-backend-id"


def backend_subnet(**overrides):
    return {
        "id": SUBNET_BACKEND_ID,
        "name": "subnet",
        "description": "",
        "network_id": NETWORK_BACKEND_ID,
        "cidr": "192.168.42.0/24",
        "ip_version": 4,
        "enable_dhcp": True,
        "gateway_ip": "192.168.42.1",
        "dns_nameservers": [],
        "allocation_pools": [],
        "host_routes": [],
        **overrides,
    }


def interface_port(tenant_id, subnet_id=SUBNET_BACKEND_ID):
    return {
        "id": "port-interface",
        "network_id": NETWORK_BACKEND_ID,
        "device_owner": "network:router_interface",
        "device_id": "router-backend-id",
        "tenant_id": tenant_id,
        "fixed_ips": [{"subnet_id": subnet_id, "ip_address": "192.168.42.1"}],
    }


class SubnetIsConnectedPullTest(test.APITestCase):
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
        self.subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id=SUBNET_BACKEND_ID,
            cidr="192.168.42.0/24",
        )
        self.backend = OpenStackBackend(self.tenant.service_settings)

    def _pull(self, ports):
        with (
            mock.patch("waldur_openstack.backend.get_keystone_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_subnets.return_value = {"subnets": [backend_subnet()]}
            client.list_ports.return_value = {"ports": ports}
            client.list_networks.return_value = {"networks": []}
            self.backend.pull_subnets(self.tenant)
        self.subnet.refresh_from_db()
        return self.subnet.is_connected

    def test_a_subnet_no_router_holds_is_reported_as_disconnected(self):
        """The regression: the pull used to write the model default, True."""
        self.subnet.is_connected = True
        self.subnet.save()

        self.assertFalse(self._pull([]))

    def test_a_subnet_with_a_router_interface_is_reported_as_connected(self):
        self.subnet.is_connected = False
        self.subnet.save()

        self.assertTrue(self._pull([interface_port(self.tenant.backend_id)]))

    def test_a_disconnect_is_not_undone_by_the_next_pull(self):
        self.backend_disconnect()

        self.assertFalse(self._pull([]))

    def backend_disconnect(self):
        """What disconnect_subnet records once Neutron has removed the port."""
        self.subnet.is_connected = False
        self.subnet.save(update_fields=["is_connected"])

    def test_an_interface_owned_by_another_tenant_still_counts(self):
        """A subnet shared over RBAC is routed by the tenant that consumes it,
        so its interface port carries that tenant's project id. Listing ports by
        network rather than by tenant is what keeps the owner's subnet from
        looking unconnected."""
        self.subnet.is_connected = False
        self.subnet.save()

        self.assertTrue(self._pull([interface_port("some-other-tenant")]))

    def test_a_port_that_is_not_a_router_interface_does_not_count(self):
        self.subnet.is_connected = True
        self.subnet.save()

        instance_port = interface_port(self.tenant.backend_id)
        instance_port["device_owner"] = "compute:nova"

        self.assertFalse(self._pull([instance_port]))

    def test_the_settings_wide_sweep_leaves_the_flag_alone(self):
        """It does not filter by network at all, so one port listing per network
        would be unbounded; better to leave the locally known value than to
        overwrite it with a guess."""
        self.subnet.is_connected = False
        self.subnet.save()
        self.network.state = CoreStates.OK
        self.network.save()

        with (
            mock.patch("waldur_openstack.backend.get_keystone_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_subnets.return_value = {"subnets": [backend_subnet()]}
            client.list_ports.return_value = {"ports": []}
            self.backend.pull_subnets()
            client.list_ports.assert_not_called()

        self.subnet.refresh_from_db()
        self.assertFalse(self.subnet.is_connected)
