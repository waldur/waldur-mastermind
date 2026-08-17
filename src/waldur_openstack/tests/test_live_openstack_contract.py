"""Contract tests for push_instance_ports against a REAL OpenStack.

Opt-in. Skipped unless the live-cloud environment variables below are set::

    export WALDUR_LIVE_OS_AUTH_URL=http://<keystone>:5000
    export WALDUR_LIVE_OS_USERNAME=admin
    export WALDUR_LIVE_OS_PASSWORD=...
    export WALDUR_LIVE_OS_PROJECT_NAME=admin
    export WALDUR_LIVE_OS_PROJECT_ID=<that project's id>

Why these cannot be unit tests or emulator E2E tests
----------------------------------------------------
Everything here turns on the *typed* Neutron error. Real Neutron answers a
duplicate fixed IP with::

    HTTP 409 {"NeutronError": {"type": "IpAddressAlreadyAllocated", ...}}

and python-neutronclient turns the ``type`` into
``IpAddressAlreadyAllocatedClient``, which is the only exception
``push_instance_ports`` reclaims from. An error body without a ``NeutronError``
key degrades to the parent ``Conflict``, which is *not* an instance of that
class, so the whole reclaim branch is skipped and these tests would pass while
exercising a different code path. The bundled openstack-emulator emits
``{"error": {...}}``, so it cannot stand in here until it grows the Neutron
envelope.

The scenario is the one that erred a pair of production routers: an Ansible
play that declares instances with statically pinned addresses and fans them out
concurrently (``async``/``poll: 0``), re-run against instances that already
exist so the module takes the ``update_ports`` route.

No Nova server is booted. A port conflict is raised by ``create_port``, which
``push_instance_ports`` reaches before any Nova call, so the interesting paths
complete without compute. The single test that needs a *bound* address binds a
router interface instead, which produces the same ``device_owner`` guard.
"""

import os
import threading
import unittest
import uuid

from django.db import connection
from django.test import TransactionTestCase
from neutronclient.common import exceptions as neutron_exceptions

from waldur_core.core.enums import CoreStates
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError

from . import factories

LIVE_ENV = (
    "WALDUR_LIVE_OS_AUTH_URL",
    "WALDUR_LIVE_OS_USERNAME",
    "WALDUR_LIVE_OS_PASSWORD",
    "WALDUR_LIVE_OS_PROJECT_NAME",
    "WALDUR_LIVE_OS_PROJECT_ID",
)

CIDR = "10.253.0.0/24"
PINNED_IP = "10.253.0.14"  # mirrors the production ".14" collision
SECOND_IP = "10.253.0.15"


def live_config():
    return {name: os.environ.get(name) for name in LIVE_ENV}


@unittest.skipUnless(
    all(os.environ.get(name) for name in LIVE_ENV),
    "live OpenStack credentials not configured; set %s" % ", ".join(LIVE_ENV),
)
class LivePushInstancePortsTest(TransactionTestCase):
    """Drives the real backend against a real Neutron.

    TransactionTestCase (not APITestCase) because the fan-out test needs real
    committed rows visible to other threads.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from neutronclient.v2_0 import client as neutron_client

        cfg = live_config()
        cls.project_id = cfg["WALDUR_LIVE_OS_PROJECT_ID"]
        cls.suffix = uuid.uuid4().hex[:8]

        # A throwaway ServiceSettings row is the only way to get a real
        # admin session out of the backend, so build one first and borrow
        # its session for the fixture network.
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
                    "name": f"wal-contract-net-{cls.suffix}",
                    "tenant_id": cls.project_id,
                }
            }
        )["network"]
        cls.backend_subnet = cls.neutron.create_subnet(
            {
                "subnet": {
                    "name": f"wal-contract-subnet-{cls.suffix}",
                    "network_id": cls.backend_network["id"],
                    "tenant_id": cls.project_id,
                    "ip_version": 4,
                    "cidr": CIDR,
                    "enable_dhcp": False,
                }
            }
        )["subnet"]

    @classmethod
    def tearDownClass(cls):
        for port in cls.neutron.list_ports(network_id=cls.backend_network["id"])[
            "ports"
        ]:
            for router_id in {
                port["device_id"]
                for port in [port]
                if port["device_owner"].startswith("network:router")
            }:
                try:
                    cls.neutron.remove_interface_router(
                        router_id, {"port_id": port["id"]}
                    )
                except neutron_exceptions.NeutronClientException:
                    pass
            try:
                cls.neutron.delete_port(port["id"])
            except neutron_exceptions.NeutronClientException:
                pass
        for router in cls.neutron.list_routers(
            name=f"wal-contract-router-{cls.suffix}"
        )["routers"]:
            try:
                cls.neutron.delete_router(router["id"])
            except neutron_exceptions.NeutronClientException:
                pass
        try:
            cls.neutron.delete_subnet(cls.backend_subnet["id"])
            cls.neutron.delete_network(cls.backend_network["id"])
        except neutron_exceptions.NeutronClientException:
            pass
        super().tearDownClass()

    # ------------------------------------------------------------------ setup
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
        # push_instance_ports opens BOTH an admin session and a tenant-scoped
        # one (get_tenant_session reads user_username/user_password off the
        # tenant). On a real cloud there is no per-tenant service user for a
        # hand-built row, so point the tenant credentials at the same account.
        self.tenant = factories.TenantFactory(
            service_settings=self.settings,
            backend_id=self.project_id,
            user_username=cfg["WALDUR_LIVE_OS_USERNAME"],
            user_password=cfg["WALDUR_LIVE_OS_PASSWORD"],
        )
        self.network = factories.NetworkFactory(
            service_settings=self.settings,
            project=self.tenant.project,
            tenant=self.tenant,
            state=CoreStates.OK,
            backend_id=self.backend_network["id"],
        )
        self.subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.tenant.project,
            state=CoreStates.OK,
            backend_id=self.backend_subnet["id"],
        )
        self.backend = OpenStackBackend(self.settings)
        self.addCleanup(self.drop_backend_ports)

    def drop_backend_ports(self):
        """Remove every Neutron port this test left on the fixture network."""
        for port in self.neutron.list_ports(network_id=self.backend_network["id"])[
            "ports"
        ]:
            if port["device_owner"].startswith("network:router"):
                try:
                    self.neutron.remove_interface_router(
                        port["device_id"], {"port_id": port["id"]}
                    )
                    continue
                except neutron_exceptions.NeutronClientException:
                    pass
            try:
                self.neutron.delete_port(port["id"])
            except neutron_exceptions.NeutronClientException:
                pass

    def make_instance(self, name):
        """An instance whose backend_id names no real server.

        list_ports(device_id=...) simply returns nothing for it, which is the
        state push_instance_ports sees for an instance whose ports were all
        declared but never attached.
        """
        return factories.InstanceFactory(
            project=self.tenant.project,
            tenant=self.tenant,
            state=CoreStates.OK,
            name=name,
            backend_id=f"no-such-server-{uuid.uuid4().hex[:8]}",
        )

    def declare_port(self, instance, ip):
        """The row update_ports builds from a playbook-declared pinned port."""
        return factories.PortFactory(
            network=self.network,
            subnet=self.subnet,
            tenant=self.tenant,
            project=self.tenant.project,
            service_settings=self.settings,
            state=CoreStates.OK,
            instance=instance,
            # NULL, not "": Port.backend_id was made nullable in migration 0055
            # so that unique_together (tenant, backend_id) still permits several
            # pending ports per tenant. update_ports leaves it unset, which on
            # PostgreSQL yields NULL.
            backend_id=None,
            fixed_ips=[{"subnet_id": self.backend_subnet["id"], "ip_address": ip}],
        )

    def backend_ports_at(self, ip):
        return self.neutron.list_ports(
            fixed_ips=[
                f"subnet_id={self.backend_subnet['id']}",
                f"ip_address={ip}",
            ]
        )["ports"]

    # ------------------------------------------------------------------ tests
    def test_pinned_address_is_created_on_live_neutron(self):
        """The baseline: a declared address really is taken on the cloud.

        The Nova attach then fails because the instance names no server. That
        is expected here and is exactly the ordering that matters: the port is
        created and its id recorded *before* the attach, so the address is held
        from that moment on.
        """
        instance = self.make_instance("wal-contract-router-a")
        port = self.declare_port(instance, PINNED_IP)

        with self.assertRaises(OpenStackBackendError):
            self.backend.push_instance_ports(instance)

        port.refresh_from_db()
        self.assertTrue(port.backend_id, "port id was not recorded before attach")
        created = self.backend_ports_at(PINNED_IP)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["id"], port.backend_id)
        self.assertEqual(created[0]["tenant_id"], self.project_id)

    def test_parallel_fanout_loser_declines_on_a_port_it_could_have_adopted(self):
        """The production shape: two routers, one address, pushed at once.

        The playbook loops over routers with ``async``/``poll: 0``, so every
        router is handed to Waldur simultaneously and the pushes land in
        parallel Celery workers. When two declarations resolve to the same
        address, one wins.

        The loser's 409 is then met by a port that is unbound, in the right
        tenant, holding exactly the right address -- adoptable on every count
        except that the winner's local row already names it. So the reclaim
        refuses, and the instance erreds on an address that is not actually
        in use by anything.
        """
        first = self.make_instance("wal-contract-router-1")
        second = self.make_instance("wal-contract-router-2")
        self.declare_port(first, PINNED_IP)
        self.declare_port(second, PINNED_IP)

        errors = {}
        barrier = threading.Barrier(2)

        def push(label, instance):
            try:
                barrier.wait(timeout=30)
                OpenStackBackend(self.settings).push_instance_ports(instance)
            except Exception as exc:  # noqa: BLE001 - recorded and asserted on
                errors[label] = exc
            finally:
                connection.close()

        threads = [
            threading.Thread(target=push, args=("first", first)),
            threading.Thread(target=push, args=("second", second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=180)

        # Exactly one Neutron port exists for the contested address.
        holders = self.backend_ports_at(PINNED_IP)
        self.assertEqual(len(holders), 1, f"expected 1 holder, got {holders}")
        holder = holders[0]

        # It is unbound and owned by the right tenant: adoptable on the merits.
        self.assertEqual(holder["device_id"], "")
        self.assertEqual(holder["device_owner"], "")
        self.assertEqual(holder["tenant_id"] or holder["project_id"], self.project_id)

        # A local row claims it, which is the only reason the reclaim refused.
        claimed_by = models.Port.objects.filter(backend_id=holder["id"])
        self.assertTrue(claimed_by.exists())

        # And both pushes failed: the winner on the Nova attach, the loser on
        # the allocation error it declined to reclaim.
        self.assertEqual(set(errors), {"first", "second"})
        allocation_failures = [
            exc for exc in errors.values() if "already allocated" in str(exc)
        ]
        self.assertEqual(
            len(allocation_failures),
            1,
            f"expected exactly one allocation failure, got {errors}",
        )

    def test_address_held_by_a_bound_port_is_declined(self):
        """A genuinely in-use address: declining is correct here."""
        held = self.neutron.create_port(
            {
                "port": {
                    "name": f"wal-contract-held-{self.suffix}",
                    "network_id": self.backend_network["id"],
                    "tenant_id": self.project_id,
                    "project_id": self.project_id,
                    "fixed_ips": [
                        {
                            "subnet_id": self.backend_subnet["id"],
                            "ip_address": SECOND_IP,
                        }
                    ],
                }
            }
        )["port"]
        router = self.neutron.create_router(
            {
                "router": {
                    "name": f"wal-contract-router-{self.suffix}",
                    "tenant_id": self.project_id,
                }
            }
        )["router"]
        self.neutron.add_interface_router(router["id"], {"port_id": held["id"]})

        instance = self.make_instance("wal-contract-router-b")
        port = self.declare_port(instance, SECOND_IP)

        with self.assertRaises(OpenStackBackendError) as raised:
            self.backend.push_instance_ports(instance)
        self.assertIn("already allocated", str(raised.exception))

        port.refresh_from_db()
        self.assertFalse(port.backend_id, "a bound port must not be stolen")

    def test_stranded_port_no_local_row_claims_is_reclaimed(self):
        """The self-healing case, for contrast with the fan-out test.

        Same Neutron state as the loser sees above -- an unbound port on the
        right tenant holding the address -- but with no local row naming it.
        Here the reclaim adopts, which is what makes the fan-out failure a
        Waldur-side refusal rather than a real conflict.
        """
        stranded = self.neutron.create_port(
            {
                "port": {
                    "name": f"wal-contract-stranded-{self.suffix}",
                    "network_id": self.backend_network["id"],
                    "tenant_id": self.project_id,
                    "project_id": self.project_id,
                    "fixed_ips": [
                        {
                            "subnet_id": self.backend_subnet["id"],
                            "ip_address": PINNED_IP,
                        }
                    ],
                }
            }
        )["port"]

        instance = self.make_instance("wal-contract-router-c")
        port = self.declare_port(instance, PINNED_IP)

        # Still raises, but on the Nova attach -- the reclaim itself succeeded.
        with self.assertRaises(OpenStackBackendError):
            self.backend.push_instance_ports(instance)

        port.refresh_from_db()
        self.assertEqual(
            port.backend_id,
            stranded["id"],
            "the stranded port should have been adopted",
        )


@unittest.skipUnless(
    all(os.environ.get(name) for name in LIVE_ENV),
    "live OpenStack credentials not configured",
)
class LiveUpdatePortsSemanticsTest(TransactionTestCase):
    """A second defect the playbook walks into, provable without the cloud.

    OpenStackInstancePortsUpdateSerializer.update only creates a Port row when
    no row exists for (instance, subnet). Re-declaring the same subnet with a
    *different* pinned address therefore changes nothing, and the play reports
    success while Waldur keeps the old address.
    """

    def test_changed_pinned_address_is_silently_discarded(self):
        from waldur_openstack import serializers

        fixture_settings = factories.SettingsFactory(state=CoreStates.OK)
        tenant = factories.TenantFactory(
            service_settings=fixture_settings, backend_id="tenant-backend-id"
        )
        network = factories.NetworkFactory(
            service_settings=fixture_settings,
            project=tenant.project,
            tenant=tenant,
            state=CoreStates.OK,
        )
        subnet = factories.SubNetFactory(
            network=network,
            tenant=tenant,
            service_settings=fixture_settings,
            project=tenant.project,
            state=CoreStates.OK,
            backend_id="subnet-backend-id",
        )
        instance = factories.InstanceFactory(
            project=tenant.project, tenant=tenant, state=CoreStates.OK
        )
        existing = factories.PortFactory(
            network=network,
            subnet=subnet,
            tenant=tenant,
            project=tenant.project,
            service_settings=fixture_settings,
            state=CoreStates.OK,
            instance=instance,
            backend_id="already-created",
            fixed_ips=[{"subnet_id": "subnet-backend-id", "ip_address": "10.0.0.10"}],
        )

        # What the module sends on a re-run after the hostnames map changed.
        incoming = models.Port(
            subnet=subnet,
            network=network,
            tenant=tenant,
            project=tenant.project,
            service_settings=fixture_settings,
            fixed_ips=[{"subnet_id": "subnet-backend-id", "ip_address": "10.0.0.99"}],
        )
        serializer = serializers.OpenStackInstancePortsUpdateSerializer()
        serializer.update(instance, {"ports": [incoming]})

        existing.refresh_from_db()
        self.assertEqual(
            existing.fixed_ips,
            [{"subnet_id": "subnet-backend-id", "ip_address": "10.0.0.10"}],
            "the newly requested address was applied -- defect is fixed, "
            "update this test",
        )
        self.assertEqual(instance.ports.count(), 1)
