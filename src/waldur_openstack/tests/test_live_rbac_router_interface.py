"""Contract test for #394 against a REAL OpenStack (the emulator will do).

Opt-in, and skipped unless the same variables the other live contract test uses
are set::

    export WALDUR_LIVE_OS_AUTH_URL=http://localhost:5000
    export WALDUR_LIVE_OS_USERNAME=admin
    export WALDUR_LIVE_OS_PASSWORD=...
    export WALDUR_LIVE_OS_PROJECT_NAME=admin
    export WALDUR_LIVE_OS_PROJECT_ID=<that project's id>

Why this one cannot be a unit test
----------------------------------
The claim being tested is about the *cloud's* behaviour, not Waldur's: that
Neutron accepts an interface between a router in one project and a subnet in
another when an `access_as_shared` RBAC policy links them, and that it puts the
resulting `network:router_interface` port in the **router's** project. Waldur's
code was written around the opposite assumption, and a mock asserts only that
the assumption was restated. Here the port is read back out of Neutron.

It runs against the bundled openstack-emulator as well as a real cloud -- the
emulator implements Keystone project creation, RBAC policies and
add/remove_router_interface, which is all this needs.
"""

import os
import unittest
import uuid

from django.test import TransactionTestCase
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.common import exceptions as neutron_exceptions
from neutronclient.v2_0 import client as neutron_client
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.session import get_keystone_client

from . import factories

LIVE_ENV = (
    "WALDUR_LIVE_OS_AUTH_URL",
    "WALDUR_LIVE_OS_USERNAME",
    "WALDUR_LIVE_OS_PASSWORD",
    "WALDUR_LIVE_OS_PROJECT_NAME",
    "WALDUR_LIVE_OS_PROJECT_ID",
)

CIDR = "10.94.0.0/24"


def live_config():
    return {name: os.environ.get(name) for name in LIVE_ENV}


@unittest.skipUnless(
    all(os.environ.get(name) for name in LIVE_ENV),
    "live OpenStack credentials not configured; set %s" % ", ".join(LIVE_ENV),
)
class LiveSharedSubnetRouterInterfaceTest(TransactionTestCase):
    """The consumer routes a subnet the owner shared with it.

    TransactionTestCase because the action's own pull re-reads rows it wrote.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cfg = live_config()
        cls.owner_project_id = cfg["WALDUR_LIVE_OS_PROJECT_ID"]
        cls.suffix = uuid.uuid4().hex[:8]
        cls.bootstrap_settings = factories.SettingsFactory(
            backend_url=cfg["WALDUR_LIVE_OS_AUTH_URL"],
            username=cfg["WALDUR_LIVE_OS_USERNAME"],
            password=cfg["WALDUR_LIVE_OS_PASSWORD"],
            options={"tenant_name": cfg["WALDUR_LIVE_OS_PROJECT_NAME"]},
            shared=True,
            state=CoreStates.OK,
        )
        backend = OpenStackBackend(cls.bootstrap_settings)
        cls.neutron = neutron_client.Client(session=backend.admin_session)

        cls.backend_network = cls.neutron.create_network(
            {
                "network": {
                    "name": f"wal-394-net-{cls.suffix}",
                    "tenant_id": cls.owner_project_id,
                }
            }
        )["network"]
        cls.backend_subnet = cls.neutron.create_subnet(
            {
                "subnet": {
                    "name": f"wal-394-subnet-{cls.suffix}",
                    "network_id": cls.backend_network["id"],
                    "tenant_id": cls.owner_project_id,
                    "ip_version": 4,
                    "cidr": CIDR,
                    "enable_dhcp": False,
                }
            }
        )["subnet"]

    @classmethod
    def tearDownClass(cls):
        try:
            cls.neutron.delete_subnet(cls.backend_subnet["id"])
            cls.neutron.delete_network(cls.backend_network["id"])
        except neutron_exceptions.NeutronClientException:
            pass
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        cfg = live_config()
        self.settings = factories.SettingsFactory(
            backend_url=cfg["WALDUR_LIVE_OS_AUTH_URL"],
            username=cfg["WALDUR_LIVE_OS_USERNAME"],
            password=cfg["WALDUR_LIVE_OS_PASSWORD"],
            options={"tenant_name": cfg["WALDUR_LIVE_OS_PROJECT_NAME"]},
            shared=True,
            state=CoreStates.OK,
        )
        self.backend = OpenStackBackend(self.settings)
        credentials = {
            "user_username": cfg["WALDUR_LIVE_OS_USERNAME"],
            "user_password": cfg["WALDUR_LIVE_OS_PASSWORD"],
        }

        self.owner_tenant = factories.TenantFactory(
            service_settings=self.settings,
            backend_id=self.owner_project_id,
            state=CoreStates.OK,
            **credentials,
        )
        # A second real project, so the two tenants are as separate in the cloud
        # as they are in Waldur.
        self.consumer_tenant = factories.TenantFactory(
            name=f"wal-394-consumer-{self.suffix}",
            service_settings=self.settings,
            project=self.owner_tenant.project,
            backend_id="",
            state=CoreStates.OK,
            **credentials,
        )
        self.backend.create_tenant(self.consumer_tenant)
        self.consumer_tenant.refresh_from_db()
        self.assertTrue(self.consumer_tenant.backend_id)
        # Provisioning grants the settings' admin a role in every tenant it
        # creates; without it a tenant-scoped session (`get_tenant_session`,
        # which the pulls and `is_subnet_connected` open) is refused with 401.
        self.backend.add_admin_user_to_tenant(self.consumer_tenant)
        self.addCleanup(self.drop_tenant, self.consumer_tenant.backend_id)

        self.network = factories.NetworkFactory(
            service_settings=self.settings,
            project=self.owner_tenant.project,
            tenant=self.owner_tenant,
            state=CoreStates.OK,
            backend_id=self.backend_network["id"],
        )
        self.subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.owner_tenant,
            service_settings=self.settings,
            project=self.owner_tenant.project,
            state=CoreStates.OK,
            backend_id=self.backend_subnet["id"],
            cidr=CIDR,
            allocation_pools=self.backend_subnet["allocation_pools"],
            is_connected=False,
        )

        # The share itself, in Neutron and in Waldur -- the API reads the row,
        # the cloud enforces the policy. Registered before the router so the
        # cleanups unwind the other way round: Neutron refuses to drop a policy
        # while an interface still depends on it.
        rbac_id = self.backend.create_network_rbac_policy(
            self.network, self.consumer_tenant
        )
        self.addCleanup(self.drop_rbac_policy, rbac_id)
        self.policy = factories.NetworkRBACPolicyFactory(
            network=self.network,
            target_tenant=self.consumer_tenant,
            backend_id=rbac_id,
        )

        backend_router = self.neutron.create_router(
            {
                "router": {
                    "name": f"wal-394-router-{self.suffix}",
                    "tenant_id": self.consumer_tenant.backend_id,
                }
            }
        )["router"]
        self.addCleanup(self.drop_router, backend_router["id"])
        self.router = factories.RouterFactory(
            tenant=self.consumer_tenant,
            service_settings=self.settings,
            project=self.owner_tenant.project,
            state=CoreStates.OK,
            backend_id=backend_router["id"],
        )

        self.client = test.APIClient()
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        self.url_add = factories.RouterFactory.get_url(
            self.router, action="add_router_interface"
        )
        self.url_remove = factories.RouterFactory.get_url(
            self.router, action="remove_router_interface"
        )
        self.payload = {"subnet": factories.SubNetFactory.get_url(self.subnet)}

    def drop_tenant(self, backend_id):
        try:
            get_keystone_client(self.backend.admin_session).projects.delete(backend_id)
        except keystone_exceptions.ClientException:
            pass

    def drop_router(self, backend_id):
        try:
            self.neutron.remove_interface_router(
                backend_id, {"subnet_id": self.backend_subnet["id"]}
            )
        except neutron_exceptions.NeutronClientException:
            pass
        try:
            self.neutron.delete_router(backend_id)
        except neutron_exceptions.NeutronClientException:
            pass

    def drop_rbac_policy(self, rbac_id):
        try:
            self.backend.delete_network_rbac_policy(rbac_id)
        except Exception:
            pass

    def interface_ports(self):
        """The live interface ports on the consumer's router for this subnet."""
        return [
            port
            for port in self.neutron.list_ports(network_id=self.backend_network["id"])[
                "ports"
            ]
            if port["device_id"] == self.router.backend_id
            and any(
                fixed_ip["subnet_id"] == self.backend_subnet["id"]
                for fixed_ip in port["fixed_ips"]
            )
        ]

    def test_the_consumer_routes_the_shared_subnet(self):
        response = self.client.post(self.url_add, self.payload)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        ports = self.interface_ports()
        self.assertEqual(len(ports), 1, "the interface is not on the consumer's router")
        port = ports[0]
        self.assertEqual(port["device_owner"], "network:router_interface")
        # The claim this test exists for: the cloud bills the interface to the
        # tenant whose router holds it, and Waldur's row now agrees.
        self.assertEqual(port["tenant_id"], self.consumer_tenant.backend_id)
        local_port = models.Port.objects.get(backend_id=port["id"])
        self.assertEqual(local_port.tenant, self.consumer_tenant)

        self.subnet.refresh_from_db()
        self.assertEqual(self.subnet.router, self.router)
        self.assertTrue(self.subnet.is_connected)

    def test_the_consumer_can_detach_it_again(self):
        self.client.post(self.url_add, self.payload)
        self.assertEqual(len(self.interface_ports()), 1)

        response = self.client.post(self.url_remove, self.payload)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        # Unlike the attach, removal is asynchronous: the action schedules
        # `remove_router_interface_safely` and the test settings run no eager
        # Celery, so drive the worker's half here rather than assert on a task
        # that was never executed.
        self.backend.remove_router_interface_safely(
            self.router, subnet_id=self.subnet.id
        )

        self.assertEqual(self.interface_ports(), [])
        self.subnet.refresh_from_db()
        self.assertFalse(self.subnet.is_connected)
        # #388: the router is remembered, so a reconnect returns it there.
        self.assertEqual(self.subnet.router, self.router)

    def test_without_the_share_the_cloud_is_never_asked(self):
        self.policy.delete()

        response = self.client.post(self.url_add, self.payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.interface_ports(), [])
