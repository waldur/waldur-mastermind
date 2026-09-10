"""Contract test for #390 against a REAL OpenStack (the emulator will do).

Opt-in, skipped unless the live-cloud variables the other live contract tests use
are set::

    export WALDUR_LIVE_OS_AUTH_URL=http://localhost:5000/v3
    export WALDUR_LIVE_OS_USERNAME=admin
    export WALDUR_LIVE_OS_PASSWORD=...
    export WALDUR_LIVE_OS_PROJECT_NAME=admin
    export WALDUR_LIVE_OS_PROJECT_ID=<that project's id>

Why this one cannot be a unit test
----------------------------------
The bug was Waldur discarding a value only the cloud produces. Neutron allocates
a pool for a subnet whose request carries none, and a mock can only assert that we
copy whatever the mock was told to answer -- which is the same mistake in a
different place. Here the pool is read back out of Neutron and compared, and the
consequence the report was really about is exercised end to end: attaching a
subnet created seconds ago to a router, which used to answer
``400 No available IP addresses in subnet <id>``.
"""

import os
import unittest
import uuid

from django.test import TransactionTestCase
from neutronclient.common import exceptions as neutron_exceptions
from neutronclient.v2_0 import client as neutron_client
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.backend import OpenStackBackend

from . import factories

LIVE_ENV = (
    "WALDUR_LIVE_OS_AUTH_URL",
    "WALDUR_LIVE_OS_USERNAME",
    "WALDUR_LIVE_OS_PASSWORD",
    "WALDUR_LIVE_OS_PROJECT_NAME",
    "WALDUR_LIVE_OS_PROJECT_ID",
)

CIDR = "10.96.0.0/24"


def live_config():
    return {name: os.environ.get(name) for name in LIVE_ENV}


@unittest.skipUnless(
    all(os.environ.get(name) for name in LIVE_ENV),
    "live OpenStack credentials not configured; set %s" % ", ".join(LIVE_ENV),
)
class LiveSubnetAllocationPoolsTest(TransactionTestCase):
    def setUp(self):
        super().setUp()
        cfg = live_config()
        self.project_id = cfg["WALDUR_LIVE_OS_PROJECT_ID"]
        self.suffix = uuid.uuid4().hex[:8]
        self.settings = factories.SettingsFactory(
            backend_url=cfg["WALDUR_LIVE_OS_AUTH_URL"],
            username=cfg["WALDUR_LIVE_OS_USERNAME"],
            password=cfg["WALDUR_LIVE_OS_PASSWORD"],
            options={"tenant_name": cfg["WALDUR_LIVE_OS_PROJECT_NAME"]},
            shared=True,
            state=CoreStates.OK,
        )
        self.backend = OpenStackBackend(self.settings)
        self.neutron = neutron_client.Client(session=self.backend.admin_session)

        self.tenant = factories.TenantFactory(
            service_settings=self.settings,
            backend_id=self.project_id,
            state=CoreStates.OK,
            user_username=cfg["WALDUR_LIVE_OS_USERNAME"],
            user_password=cfg["WALDUR_LIVE_OS_PASSWORD"],
        )
        backend_network = self.neutron.create_network(
            {
                "network": {
                    "name": f"wal-390-net-{self.suffix}",
                    "tenant_id": self.project_id,
                }
            }
        )["network"]
        self.addCleanup(self.drop_network, backend_network["id"])
        self.network = factories.NetworkFactory(
            service_settings=self.settings,
            project=self.tenant.project,
            tenant=self.tenant,
            state=CoreStates.OK,
            backend_id=backend_network["id"],
        )
        # No pool and no backend_id: what the API hands the backend when the
        # caller did not ask for a particular range.
        self.subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.tenant.project,
            state=CoreStates.OK,
            backend_id="",
            cidr=CIDR,
            allocation_pools=[],
        )

    def drop_network(self, backend_id):
        for port in self.neutron.list_ports(network_id=backend_id)["ports"]:
            if port["device_owner"].startswith("network:router"):
                try:
                    self.neutron.remove_interface_router(
                        port["device_id"], {"port_id": port["id"]}
                    )
                except neutron_exceptions.NeutronClientException:
                    pass
        for subnet in self.neutron.list_subnets(network_id=backend_id)["subnets"]:
            try:
                self.neutron.delete_subnet(subnet["id"])
            except neutron_exceptions.NeutronClientException:
                pass
        try:
            self.neutron.delete_network(backend_id)
        except neutron_exceptions.NeutronClientException:
            pass

    def drop_router(self, backend_id):
        # A router that still holds an interface cannot be deleted, so detach
        # first -- this cleanup runs before the network's.
        for port in self.neutron.list_ports(device_id=backend_id)["ports"]:
            if port["device_owner"].startswith("network:router"):
                try:
                    self.neutron.remove_interface_router(
                        backend_id, {"port_id": port["id"]}
                    )
                except neutron_exceptions.NeutronClientException:
                    pass
        try:
            self.neutron.delete_router(backend_id)
        except neutron_exceptions.NeutronClientException:
            pass

    def create_subnet(self):
        self.backend.create_subnet(self.subnet, skip_router_connection=True)
        self.subnet.refresh_from_db()

    def backend_pools(self):
        return self.neutron.show_subnet(self.subnet.backend_id)["subnet"][
            "allocation_pools"
        ]

    def make_router(self):
        backend_router = self.neutron.create_router(
            {
                "router": {
                    "name": f"wal-390-router-{self.suffix}",
                    "tenant_id": self.project_id,
                }
            }
        )["router"]
        self.addCleanup(self.drop_router, backend_router["id"])
        return factories.RouterFactory(
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.tenant.project,
            state=CoreStates.OK,
            backend_id=backend_router["id"],
        )

    def test_the_pool_neutron_allocated_is_stored_on_create(self):
        self.create_subnet()

        pools = self.subnet.allocation_pools
        self.assertIsInstance(pools, list)
        self.assertTrue(pools, "the subnet was stored with no pool at all")
        self.assertEqual(pools, self.backend_pools())

    def test_a_subnet_created_seconds_ago_can_be_attached_to_a_router(self):
        """The report's real consequence: `get_free_ip` reads the field this bug
        left empty, so the action called a brand-new subnet full."""
        self.create_subnet()
        router = self.make_router()

        client = test.APIClient()
        client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = client.post(
            factories.RouterFactory.get_url(router, action="add_router_interface"),
            {"subnet": factories.SubNetFactory.get_url(self.subnet)},
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        interfaces = [
            port
            for port in self.neutron.list_ports(network_id=self.network.backend_id)[
                "ports"
            ]
            if port["device_id"] == router.backend_id
            and any(
                fixed_ip["subnet_id"] == self.subnet.backend_id
                for fixed_ip in port["fixed_ips"]
            )
        ]
        self.assertEqual(len(interfaces), 1, "no interface reached the router")
        self.subnet.refresh_from_db()
        self.assertTrue(self.subnet.is_connected)

    def test_a_row_written_before_the_fix_still_finds_a_free_address(self):
        """Existing subnets hold `{}` and are not pulled again for up to two
        hours; the fallback reads the pool where it actually lives."""
        self.create_subnet()
        self.subnet.allocation_pools = {}
        self.subnet.save(update_fields=["allocation_pools"])

        free_ip = self.backend.get_free_ip(self.subnet)

        self.assertTrue(free_ip, "no address was found for a pool-less row")
        self.assertTrue(free_ip.startswith("10.96.0."), free_ip)
