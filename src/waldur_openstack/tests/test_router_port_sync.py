"""Router <-> Port synchronisation.

Regression coverage for #387: a router interface created through Waldur could
not be removed through Waldur. `pull_tenant_routers` builds `router.fixed_ips`
straight from the Neutron response but builds the `ports` M2M by filtering
local `Port` rows, so a port Waldur had not imported yet was silently dropped —
its address showed on the router while the removal dialog, which lists
`router.ports`, had nothing to offer.
"""

from unittest import mock

from rest_framework import test

from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError

from . import factories, fixtures

ROUTER_BACKEND_ID = "router-backend-id"
TENANT_INTERFACE_PORT = {
    "id": "port-interface",
    "name": "",
    "description": "",
    "network_id": "network-backend-id",
    "status": "ACTIVE",
    "admin_state_up": True,
    "mac_address": "fa:16:3e:62:b8:95",
    "fixed_ips": [{"subnet_id": "subnet-backend-id", "ip_address": "192.168.99.1"}],
    "device_id": ROUTER_BACKEND_ID,
    "device_owner": "network:router_interface",
    "security_groups": [],
    "allowed_address_pairs": [],
    "port_security_enabled": False,
}


class PullTenantRoutersPortsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.tenant.backend_id = "tenant-backend-id"
        self.tenant.save()
        self.network = factories.NetworkFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="network-backend-id",
        )
        factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="subnet-backend-id",
        )
        self.backend = OpenStackBackend(self.tenant.service_settings)

    def _pull(self, ports, gateway_info=None):
        backend_router = {
            "id": ROUTER_BACKEND_ID,
            "name": "int-net-router",
            "description": "",
            "routes": [],
            "external_gateway_info": gateway_info,
        }
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_routers.return_value = {"routers": [backend_router]}
            client.show_router.return_value = {"router": backend_router}
            client.list_ports.return_value = {"ports": ports}
            self.backend.pull_tenant_routers(self.tenant)
        return models.Router.objects.get(
            tenant=self.tenant, backend_id=ROUTER_BACKEND_ID
        )

    def test_interface_port_is_imported_when_it_has_no_local_row(self):
        """The whole point: an interface Waldur has not seen before is usable.

        Neutron creates this port when a subnet is attached to a router. Before
        the fix nothing imported it until the two-hourly subresources pull, and
        until then it was missing from router.ports.
        """
        port = {**TENANT_INTERFACE_PORT, "tenant_id": self.tenant.backend_id}
        router = self._pull([port])

        self.assertEqual([p.backend_id for p in router.ports.all()], ["port-interface"])
        # The address was always visible here; it is the mismatch with ports
        # that made the interface unremovable.
        self.assertEqual(router.fixed_ips, ["192.168.99.1"])
        imported = models.Port.objects.get(
            tenant=self.tenant, backend_id="port-interface"
        )
        self.assertEqual(imported.device_id, ROUTER_BACKEND_ID)
        self.assertEqual(imported.device_owner, "network:router_interface")
        self.assertEqual(imported.subnet.backend_id, "subnet-backend-id")

    def test_existing_port_row_is_reused_not_duplicated(self):
        factories.PortFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="port-interface",
        )
        port = {**TENANT_INTERFACE_PORT, "tenant_id": self.tenant.backend_id}
        router = self._pull([port])

        self.assertEqual(router.ports.count(), 1)
        self.assertEqual(
            models.Port.objects.filter(
                tenant=self.tenant, backend_id="port-interface"
            ).count(),
            1,
        )

    def test_port_owned_via_project_id_alone_is_still_imported(self):
        """tenant_id is Neutron's deprecated alias for project_id.

        Both are returned today. Reading only the alias would skip every port
        and silently restore the bug, so the fallback is pinned here.
        """
        port = {
            **TENANT_INTERFACE_PORT,
            "project_id": self.tenant.backend_id,
        }
        port.pop("tenant_id", None)
        router = self._pull([port])

        self.assertEqual([p.backend_id for p in router.ports.all()], ["port-interface"])

    def test_gateway_port_of_another_project_is_not_imported(self):
        """A router gateway port belongs to the external network's project.

        Neutron leaves its tenant_id empty, and pull_tenant_ports lists by
        tenant_id — so importing it here would only have that sweep delete it
        again on the next pass. The gateway is managed through
        set/remove_external_gateway, not the router-interface actions.
        """
        gateway_port = {
            **TENANT_INTERFACE_PORT,
            "id": "port-gateway",
            "tenant_id": "",
            "device_owner": "network:router_gateway",
            "network_id": "external-network-backend-id",
            "fixed_ips": [
                {"subnet_id": "external-subnet", "ip_address": "10.20.35.93"}
            ],
        }
        interface_port = {**TENANT_INTERFACE_PORT, "tenant_id": self.tenant.backend_id}
        router = self._pull(
            [gateway_port, interface_port],
            gateway_info={
                "network_id": "external-network-backend-id",
                "enable_snat": False,
                "external_fixed_ips": [
                    {"subnet_id": "external-subnet", "ip_address": "10.20.35.93"}
                ],
            },
        )

        self.assertEqual([p.backend_id for p in router.ports.all()], ["port-interface"])
        self.assertFalse(
            models.Port.objects.filter(
                tenant=self.tenant, backend_id="port-gateway"
            ).exists()
        )
        # The gateway address still reaches the UI through fixed_ips.
        self.assertIn("10.20.35.93", router.fixed_ips)


class GetRouterSelectionTest(test.APITestCase):
    """Which router a new subnet is attached to.

    `_get_router` used to return `routers[0]`. Neutron guarantees no order on
    list_routers, so in a tenant with several routers a new subnet could land
    on an unrelated one — the reported case attached it to a point-to-point
    uplink router instead of the tenant's internal one.
    """

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.tenant.name = "maanteeamet-prd-harku-1"
        self.tenant.backend_id = "tenant-backend-id"
        self.tenant.save()
        self.backend = OpenStackBackend(self.tenant.service_settings)

    @staticmethod
    def _router(name, created_at, backend_id):
        return {"id": backend_id, "name": name, "created_at": created_at}

    def _get(self, routers, preferred_names=()):
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_routers.return_value = {"routers": routers}
            return self.backend._get_router(
                self.tenant, preferred_names=preferred_names
            )

    def test_none_when_tenant_has_no_router(self):
        self.assertIsNone(self._get([]))

    def test_router_named_after_the_network_wins(self):
        routers = [
            self._router("p2p-router", "2026-08-03T11:09:31Z", "p2p"),
            self._router("marvin-test-router", "2026-09-01T00:00:00Z", "marvin"),
        ]
        chosen = self._get(routers, preferred_names=("marvin-test-router",))
        self.assertEqual(chosen["id"], "marvin")

    def test_tenant_default_router_is_the_next_preference(self):
        routers = [
            self._router("p2p-router", "2026-08-03T11:09:31Z", "p2p"),
            self._router(
                "maanteeamet-prd-harku-1-int-net-router",
                "2025-05-29T17:01:55Z",
                "int-net",
            ),
        ]
        chosen = self._get(
            routers,
            preferred_names=(
                "marvin-test-router",
                "maanteeamet-prd-harku-1-int-net-router",
            ),
        )
        self.assertEqual(chosen["id"], "int-net")

    def test_falls_back_to_the_oldest_router(self):
        routers = [
            self._router("p2p-router", "2026-08-03T11:09:31Z", "p2p"),
            self._router("some-other-router", "2025-05-29T17:01:55Z", "older"),
        ]
        self.assertEqual(self._get(routers)["id"], "older")

    def test_selection_does_not_depend_on_neutron_ordering(self):
        """The defect itself: routers[0] varied with an unordered listing."""
        routers = [
            self._router("p2p-router", "2026-08-03T11:09:31Z", "p2p"),
            self._router("some-other-router", "2025-05-29T17:01:55Z", "older"),
        ]
        self.assertEqual(
            self._get(routers)["id"], self._get(list(reversed(routers)))["id"]
        )

    def test_ties_are_broken_deterministically_without_created_at(self):
        routers = [
            {"id": "bbb", "name": "b-router"},
            {"id": "aaa", "name": "a-router"},
        ]
        self.assertEqual(self._get(routers)["id"], "aaa")
        self.assertEqual(self._get(list(reversed(routers)))["id"], "aaa")

    def test_two_routers_of_the_same_name_resolve_deterministically(self):
        """Neutron does not enforce unique router names inside a project, and a
        name lookup that keeps "whichever came last" is order-dependent again."""
        routers = [
            self._router("shared-name", "2026-08-03T11:09:31Z", "newer"),
            self._router("shared-name", "2025-05-29T17:01:55Z", "older"),
        ]
        self.assertEqual(self._get(routers, ("shared-name",))["id"], "older")
        self.assertEqual(
            self._get(list(reversed(routers)), ("shared-name",))["id"], "older"
        )


class DefaultRouterNameTest(test.APITestCase):
    """The name of the router Waldur creates alongside a tenant.

    `connect_router` used to guess it as f"{tenant.name}-int-net-router", but
    the internal network is created as slugify(name)[:25] + "-int-net", so the
    guess missed every tenant whose name is not already a short slug -- and
    names come from user-supplied order attributes. The preference then never
    matched and the selection fell through to "the oldest router in the
    tenant", which is the arbitrary pick #387 removed.
    """

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.tenant.name = "Maanteeamet PRD Harku 1"
        self.tenant.backend_id = "tenant-backend-id"
        self.tenant.internal_network_id = "int-net-backend-id"
        self.tenant.save()
        self.backend = OpenStackBackend(self.tenant.service_settings)

    def test_the_name_comes_from_the_internal_network_row(self):
        factories.NetworkFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="int-net-backend-id",
            name="maanteeamet-prd-harku-1-int-net",
        )

        self.assertEqual(
            self.backend._default_router_name(self.tenant),
            "maanteeamet-prd-harku-1-int-net-router",
        )

    def test_the_name_is_derived_when_the_network_is_not_recorded(self):
        self.assertEqual(
            self.backend._default_router_name(self.tenant),
            "maanteeamet-prd-harku-1-int-net-router",
        )

    def test_a_long_tenant_name_is_truncated_the_way_creation_truncates_it(self):
        self.tenant.name = "A very long tenant name that will certainly be cut"
        self.tenant.internal_network_id = ""
        self.tenant.save()

        self.assertEqual(
            self.backend._default_router_name(self.tenant),
            "a-very-long-tenant-name-t-int-net-router",
        )

    def test_connect_router_prefers_the_tenant_default_over_the_oldest(self):
        """End to end through connect_router: the int-net router wins even
        though the point-to-point router is older."""
        factories.NetworkFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="int-net-backend-id",
            name="maanteeamet-prd-harku-1-int-net",
        )
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_routers.return_value = {
                "routers": [
                    {
                        "id": "p2p",
                        "name": "p2p-router",
                        "created_at": "2025-01-01T00:00:00Z",
                    },
                    {
                        "id": "int-net",
                        "name": "maanteeamet-prd-harku-1-int-net-router",
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                ]
            }
            client.show_subnet.return_value = {
                "subnet": {"id": "subnet-backend-id", "gateway_ip": "10.0.0.1"}
            }
            client.list_ports.return_value = {"ports": []}
            chosen = self.backend.connect_router(
                self.tenant,
                "marvin-test",
                "subnet-backend-id",
                network_id="network-backend-id",
            )

        self.assertEqual(chosen, "int-net")
        client.create_router.assert_not_called()


class CreateSubnetImportsInterfaceTest(test.APITestCase):
    """create_subnet imports the interface port it just caused Neutron to make.

    Deliberately inside the backend method rather than as a task in
    SubNetCreateExecutor's chain: a CreateExecutor marks its instance ERRED
    when any chained task fails, which would falsify the state of a subnet
    that exists and works in Neutron.
    """

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.tenant.backend_id = "tenant-backend-id"
        self.tenant.save()
        self.network = factories.NetworkFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="network-backend-id",
        )
        self.subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="",
            cidr="192.168.99.0/24",
        )
        self.backend = OpenStackBackend(self.tenant.service_settings)

    def _create(self):
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
            mock.patch.object(
                self.backend, "connect_subnet", return_value=ROUTER_BACKEND_ID
            ),
            mock.patch.object(self.backend, "pull_tenant_routers") as pull,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.create_subnet.return_value = {
                "subnet": {"id": "subnet-backend-id", "gateway_ip": "192.168.99.1"}
            }
            self.backend.create_subnet(self.subnet)
        return pull

    def test_the_new_router_is_pulled_scoped_to_that_router(self):
        pull = self._create()
        pull.assert_called_once_with(self.tenant, ROUTER_BACKEND_ID)

    def test_a_failing_pull_does_not_fail_subnet_creation(self):
        """The subnet exists in Neutron; a pull hiccup must not erred it."""
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
            mock.patch.object(
                self.backend, "connect_subnet", return_value=ROUTER_BACKEND_ID
            ),
            mock.patch.object(
                self.backend,
                "pull_tenant_routers",
                side_effect=OpenStackBackendError("neutron is having a moment"),
            ),
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.create_subnet.return_value = {
                "subnet": {"id": "subnet-backend-id", "gateway_ip": "192.168.99.1"}
            }
            # Must not raise: raising here would run the executor's failure
            # signature and mark the subnet ERRED.
            self.backend.create_subnet(self.subnet)

        self.subnet.refresh_from_db()
        self.assertEqual(self.subnet.backend_id, "subnet-backend-id")

    def test_nothing_is_pulled_when_the_tenant_skips_the_default_router(self):
        self.tenant.skip_creation_of_default_router = True
        self.tenant.save()
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
            mock.patch.object(self.backend, "pull_tenant_routers") as pull,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.create_subnet.return_value = {
                "subnet": {"id": "subnet-backend-id", "gateway_ip": "192.168.99.1"}
            }
            self.backend.create_subnet(self.subnet)
        pull.assert_not_called()
