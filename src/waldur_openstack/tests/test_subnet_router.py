"""Choosing the router a subnet is attached to (#388).

A tenant can hold several routers -- typically one for the internal network and
one for a point-to-point uplink -- and until now nothing let the caller say
which one a new subnet should land on, nor showed afterwards where it went.
`SubNet.router` answers both: written as intent at creation, and rewritten from
Neutron on every router pull so it also reflects attachments made outside
Waldur.
"""

from unittest import mock

from neutronclient.common import exceptions as neutron_exceptions
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError

from . import factories, fixtures

ROUTER_BACKEND_ID = "chosen-router-backend-id"
OTHER_ROUTER_BACKEND_ID = "other-router-backend-id"


class CreateSubnetWithRouterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(self.fixture.owner)
        self.url = factories.NetworkFactory.get_url(
            network=self.fixture.network, action="create_subnet"
        )
        self.router = self.fixture.router

    def _post(self, **extra):
        return self.client.post(self.url, {"name": "test-subnet", **extra})

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_chosen_router_is_stored_on_the_subnet(self, executor):
        response = self._post(router=factories.RouterFactory.get_url(self.router))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        subnet = models.SubNet.objects.get(uuid=response.data["uuid"])
        self.assertEqual(subnet.router, self.router)
        executor.assert_called_once()

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_router_is_optional(self, executor):
        """Omitting it must keep behaving exactly as before this field existed:
        no error, no new required input, and the backend resolves the router."""
        response = self._post()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        subnet = models.SubNet.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(subnet.router)

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_router_of_another_tenant_is_rejected(self, executor):
        other_router = factories.RouterFactory(state=CoreStates.OK)

        response = self._post(router=factories.RouterFactory.get_url(other_router))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("router", response.data)
        self.assertFalse(models.SubNet.objects.filter(name="test-subnet").exists())

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_router_in_a_transitional_state_is_rejected(self, executor):
        self.router.state = CoreStates.ERRED
        self.router.save()

        response = self._post(router=factories.RouterFactory.get_url(self.router))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("router", response.data)

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_router_is_refused_when_the_tenant_skips_router_connection(self, executor):
        """skip_creation_of_default_router says "do not attach my subnets to a
        router"; naming one says "attach it here". The two contradict each other,
        and connect_subnet would ignore the router, so the request is rejected
        rather than silently half-applied."""
        tenant = self.fixture.tenant
        tenant.skip_creation_of_default_router = True
        tenant.save()

        response = self._post(router=factories.RouterFactory.get_url(self.router))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("router", response.data)

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_such_a_tenant_can_still_create_a_subnet_without_a_router(self, executor):
        tenant = self.fixture.tenant
        tenant.skip_creation_of_default_router = True
        tenant.save()

        response = self._post()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_router_without_a_backend_id_is_rejected(self, executor):
        """connect_subnet addresses the router by backend_id; an empty one is
        falsy, so the implicit resolution would attach the subnet elsewhere
        while the API kept reporting this choice."""
        self.router.backend_id = ""
        self.router.save()

        response = self._post(router=factories.RouterFactory.get_url(self.router))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("router", response.data)

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_router_is_refused_together_with_disable_gateway(self, executor):
        """Neutron will not put a router interface on a subnet with no gateway
        IP, and _connect_network_to_router returns early for that, so accepting
        both would report a router the subnet was never attached to."""
        response = self._post(
            router=factories.RouterFactory.get_url(self.router), disable_gateway=True
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("router", response.data)

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_disable_gateway_alone_is_still_accepted(self, executor):
        response = self._post(disable_gateway=True)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_creating_a_subnet_with_a_router_needs_no_extra_permission(self, executor):
        """The choice is not a privileged operation: whoever may create a subnet
        may say where it goes. An admin of the project can do both."""
        self.client.force_authenticate(self.fixture.admin)

        response = self._post(router=factories.RouterFactory.get_url(self.router))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class SubnetRouterFieldTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(self.fixture.owner)
        self.subnet = self.fixture.subnet
        self.router = self.fixture.router
        self.url = factories.SubNetFactory.get_url(self.subnet)

    def test_router_is_readable_on_the_subnet(self):
        self.subnet.router = self.router
        self.subnet.save()

        response = self.client.get(self.url)

        self.assertEqual(response.data["router_name"], self.router.name)
        self.assertEqual(response.data["router_uuid"], self.router.uuid.hex)
        self.assertIn(str(self.router.uuid.hex), response.data["router"])

    def test_fields_are_present_and_null_when_the_subnet_has_no_router(self):
        """They must not vanish from the payload: a generated client reads the
        key unconditionally."""
        response = self.client.get(self.url)

        self.assertIsNone(response.data["router"])
        self.assertIsNone(response.data["router_name"])
        self.assertIsNone(response.data["router_uuid"])

    def test_router_is_readable_on_the_subnet_list(self):
        self.subnet.router = self.router
        self.subnet.save()

        response = self.client.get(factories.SubNetFactory.get_list_url())

        self.assertEqual(response.data[0]["router_name"], self.router.name)

    @mock.patch("waldur_openstack.executors.SubNetUpdateExecutor.execute")
    def test_router_cannot_be_changed_by_updating_the_subnet(self, executor):
        """Re-targeting is remove_router_interface + add_router_interface on the
        router; accepting it here would change what the API reports without
        moving anything in Neutron."""
        response = self.client.patch(
            self.url,
            {
                "name": self.subnet.name,
                "router": factories.RouterFactory.get_url(self.router),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.subnet.refresh_from_db()
        self.assertIsNone(self.subnet.router)


class ConnectSubnetToChosenRouterTest(test.APITestCase):
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
            backend_id="subnet-backend-id",
        )
        self.router = factories.RouterFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id=ROUTER_BACKEND_ID,
            state=CoreStates.OK,
        )
        self.backend = OpenStackBackend(self.tenant.service_settings)

    def _connect(self):
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.show_router.return_value = {
                "router": {"id": ROUTER_BACKEND_ID, "name": "chosen-router"}
            }
            client.list_routers.return_value = {
                "routers": [{"id": OTHER_ROUTER_BACKEND_ID, "name": "some-router"}]
            }
            client.show_subnet.return_value = {
                "subnet": {"id": "subnet-backend-id", "gateway_ip": "192.168.99.1"}
            }
            client.list_ports.return_value = {"ports": []}
            router_backend_id = self.backend.connect_subnet(self.subnet)
        return client, router_backend_id

    def test_the_interface_is_added_to_the_chosen_router(self):
        self.subnet.router = self.router
        self.subnet.save()

        client, _ = self._connect()

        client.add_interface_router.assert_called_once_with(
            ROUTER_BACKEND_ID, {"subnet_id": "subnet-backend-id"}
        )
        # The implicit resolution is bypassed entirely.
        client.list_routers.assert_not_called()

    def test_the_chosen_router_id_is_returned_so_its_interface_gets_imported(self):
        """create_subnet feeds this to import_new_router_interface. Losing it
        brings back #387: the port is never imported and the interface cannot be
        removed through Waldur."""
        self.subnet.router = self.router
        self.subnet.save()

        _, router_backend_id = self._connect()

        self.assertEqual(router_backend_id, ROUTER_BACKEND_ID)

    def test_a_router_that_no_longer_exists_falls_back_instead_of_erring(self):
        """A router deleted in Horizon leaves a stale row until the next full
        pull. Raising here would err the subnet and abort the executor chain
        whose second task is the very pull that would clean the row up."""
        self.subnet.router = self.router
        self.subnet.save()

        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.show_router.side_effect = neutron_exceptions.NotFound()
            client.list_routers.return_value = {
                "routers": [{"id": OTHER_ROUTER_BACKEND_ID, "name": "some-router"}]
            }
            client.show_subnet.return_value = {
                "subnet": {"id": "subnet-backend-id", "gateway_ip": "192.168.99.1"}
            }
            client.list_ports.return_value = {"ports": []}
            router_backend_id = self.backend.connect_subnet(self.subnet)

        self.assertEqual(router_backend_id, OTHER_ROUTER_BACKEND_ID)
        client.add_interface_router.assert_called_once_with(
            OTHER_ROUTER_BACKEND_ID, {"subnet_id": "subnet-backend-id"}
        )

    def test_the_stale_pointer_is_dropped_when_the_fallback_attaches_elsewhere(self):
        """Otherwise the API advertises a router that neither exists nor holds
        the interface, until the next full pull nulls the column."""
        self.subnet.router = self.router
        self.subnet.save()

        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.show_router.side_effect = neutron_exceptions.NotFound()
            client.list_routers.return_value = {
                "routers": [{"id": OTHER_ROUTER_BACKEND_ID, "name": "some-router"}]
            }
            client.show_subnet.return_value = {
                "subnet": {"id": "subnet-backend-id", "gateway_ip": "192.168.99.1"}
            }
            client.list_ports.return_value = {"ports": []}
            self.backend.connect_subnet(self.subnet)

        self.subnet.refresh_from_db()
        self.assertIsNone(self.subnet.router)

    def test_a_backend_error_other_than_not_found_still_propagates(self):
        self.subnet.router = self.router
        self.subnet.save()

        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.show_router.side_effect = neutron_exceptions.NeutronClientException(
                "neutron is unwell"
            )
            with self.assertRaises(OpenStackBackendError):
                self.backend.connect_subnet(self.subnet)

    def test_without_a_chosen_router_the_implicit_resolution_still_applies(self):
        _, router_backend_id = self._connect()

        self.assertEqual(router_backend_id, OTHER_ROUTER_BACKEND_ID)

    def test_the_opt_out_flag_wins_over_a_recorded_router(self):
        """The serializer refuses the combination, so this only happens when a
        router reached the column another way -- a pull, or the flag being set
        after the fact. The tenant's opt-out still governs: nothing is attached."""
        self.tenant.skip_creation_of_default_router = True
        self.tenant.save()
        self.subnet.router = self.router
        self.subnet.save()

        client, router_backend_id = self._connect()

        self.assertIsNone(router_backend_id)
        client.add_interface_router.assert_not_called()

    def test_the_flag_wins_when_no_router_was_chosen_either(self):
        self.tenant.skip_creation_of_default_router = True
        self.tenant.save()

        _, router_backend_id = self._connect()

        self.assertIsNone(router_backend_id)


class PullSubnetRouterTest(test.APITestCase):
    """SubNet.router is derived from the router interface ports on every pull.

    Which is why the field needs no backfill migration: a subnet created long
    before this change gets its router on the next pull, as does one attached
    outside Waldur.
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
            backend_id="subnet-backend-id",
        )
        self.backend = OpenStackBackend(self.tenant.service_settings)

    def _interface_port(self, router_backend_id, subnet_backend_id="subnet-backend-id"):
        return {
            "id": f"port-on-{router_backend_id}",
            "name": "",
            "description": "",
            "network_id": "network-backend-id",
            "status": "ACTIVE",
            "admin_state_up": True,
            "mac_address": "fa:16:3e:62:b8:95",
            "fixed_ips": [
                {"subnet_id": subnet_backend_id, "ip_address": "192.168.99.1"}
            ],
            "device_id": router_backend_id,
            "device_owner": "network:router_interface",
            "security_groups": [],
            "allowed_address_pairs": [],
            "port_security_enabled": False,
            "tenant_id": self.tenant.backend_id,
        }

    def _pull(self, routers_with_ports, router_backend_id=None):
        backend_routers = [
            {
                "id": backend_id,
                "name": f"{backend_id}-name",
                "description": "",
                "routes": [],
                "external_gateway_info": None,
            }
            for backend_id in routers_with_ports
        ]
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_routers.return_value = {"routers": backend_routers}
            client.show_router.side_effect = lambda backend_id: {
                "router": next(r for r in backend_routers if r["id"] == backend_id)
            }
            client.list_ports.side_effect = lambda device_id, **kwargs: {
                "ports": routers_with_ports[device_id]
            }
            self.backend.pull_tenant_routers(self.tenant, router_backend_id)
        self.subnet.refresh_from_db()

    def test_the_router_holding_the_interface_is_recorded(self):
        self._pull({ROUTER_BACKEND_ID: [self._interface_port(ROUTER_BACKEND_ID)]})

        self.assertEqual(self.subnet.router.backend_id, ROUTER_BACKEND_ID)

    def test_a_subnet_on_no_router_gets_none(self):
        self._pull({ROUTER_BACKEND_ID: []})

        self.assertIsNone(self.subnet.router)

    def test_a_reattachment_elsewhere_overwrites_the_recorded_router(self):
        """Including one made outside Waldur: the pull reads Neutron, not
        Waldur's own record of what it did."""
        self._pull({ROUTER_BACKEND_ID: [self._interface_port(ROUTER_BACKEND_ID)]})
        self._pull(
            {
                ROUTER_BACKEND_ID: [],
                OTHER_ROUTER_BACKEND_ID: [
                    self._interface_port(OTHER_ROUTER_BACKEND_ID)
                ],
            }
        )

        self.assertEqual(self.subnet.router.backend_id, OTHER_ROUTER_BACKEND_ID)

    def test_a_detached_subnet_keeps_the_router_it_was_last_on(self):
        """Disconnecting pulls the routers immediately afterwards, so clearing
        the field here would erase the choice before the user could reconnect;
        is_connected is what says whether the attachment is live."""
        self._pull({ROUTER_BACKEND_ID: [self._interface_port(ROUTER_BACKEND_ID)]})
        self._pull({ROUTER_BACKEND_ID: []})

        self.assertEqual(self.subnet.router.backend_id, ROUTER_BACKEND_ID)

    def test_deleting_the_router_nulls_the_column_rather_than_the_subnet(self):
        self._pull({ROUTER_BACKEND_ID: [self._interface_port(ROUTER_BACKEND_ID)]})

        models.Router.objects.filter(backend_id=ROUTER_BACKEND_ID).delete()

        self.subnet.refresh_from_db()
        self.assertIsNone(self.subnet.router)

    def test_a_single_router_pull_does_not_reassign_a_recorded_router(self):
        """A subnet can sit on several routers, and a pull scoped to one of them
        sees only that router's ports -- not enough to tell whether the recorded
        one still holds an interface. Creating an unrelated subnet on the other
        router would otherwise silently move this one."""
        self._pull(
            {OTHER_ROUTER_BACKEND_ID: [self._interface_port(OTHER_ROUTER_BACKEND_ID)]}
        )
        self.assertEqual(self.subnet.router.backend_id, OTHER_ROUTER_BACKEND_ID)

        self._pull(
            {ROUTER_BACKEND_ID: [self._interface_port(ROUTER_BACKEND_ID)]},
            router_backend_id=ROUTER_BACKEND_ID,
        )

        self.assertEqual(self.subnet.router.backend_id, OTHER_ROUTER_BACKEND_ID)

    def test_a_single_router_pull_reassigns_once_the_old_router_let_go(self):
        """Moving an interface with remove_router_interface + add_router_interface
        does two scoped pulls. The first empties the old router's ports, so the
        second must be allowed to record the new one -- the help text sends users
        to those very actions, and waiting for the two-hourly full pull would
        misreport exactly what this field answers."""
        self._pull(
            {OTHER_ROUTER_BACKEND_ID: [self._interface_port(OTHER_ROUTER_BACKEND_ID)]}
        )
        self.assertEqual(self.subnet.router.backend_id, OTHER_ROUTER_BACKEND_ID)

        # The interface is removed from the first router: its own pull drops the
        # port, which is what remove_router_interface_safely does.
        self._pull(
            {OTHER_ROUTER_BACKEND_ID: []}, router_backend_id=OTHER_ROUTER_BACKEND_ID
        )
        self._pull(
            {ROUTER_BACKEND_ID: [self._interface_port(ROUTER_BACKEND_ID)]},
            router_backend_id=ROUTER_BACKEND_ID,
        )

        self.assertEqual(self.subnet.router.backend_id, ROUTER_BACKEND_ID)

    def test_a_single_router_pull_records_the_subnet_as_well(self):
        """This is the path create_subnet takes right after attaching."""
        self._pull(
            {ROUTER_BACKEND_ID: [self._interface_port(ROUTER_BACKEND_ID)]},
            router_backend_id=ROUTER_BACKEND_ID,
        )

        self.assertEqual(self.subnet.router.backend_id, ROUTER_BACKEND_ID)

    def test_interfaces_on_several_routers_resolve_deterministically(self):
        """Neutron allows it, and the listing order is not defined, so the pick
        must not depend on which router came back first -- the mistake #387 fixed
        in _get_router."""
        routers_with_ports = {
            OTHER_ROUTER_BACKEND_ID: [self._interface_port(OTHER_ROUTER_BACKEND_ID)],
            ROUTER_BACKEND_ID: [self._interface_port(ROUTER_BACKEND_ID)],
        }
        self._pull(routers_with_ports)
        first = self.subnet.router.backend_id

        self.subnet.router = None
        self.subnet.save()
        self._pull(dict(reversed(list(routers_with_ports.items()))))

        self.assertEqual(first, self.subnet.router.backend_id)

    def test_a_router_already_recorded_is_kept_when_it_still_holds_an_interface(self):
        """Otherwise a subnet attached to two routers would flap between them on
        every pull."""
        self._pull(
            {OTHER_ROUTER_BACKEND_ID: [self._interface_port(OTHER_ROUTER_BACKEND_ID)]}
        )
        self.assertEqual(self.subnet.router.backend_id, OTHER_ROUTER_BACKEND_ID)

        self._pull(
            {
                OTHER_ROUTER_BACKEND_ID: [
                    self._interface_port(OTHER_ROUTER_BACKEND_ID)
                ],
                ROUTER_BACKEND_ID: [self._interface_port(ROUTER_BACKEND_ID)],
            }
        )

        self.assertEqual(self.subnet.router.backend_id, OTHER_ROUTER_BACKEND_ID)

    def test_a_subnet_of_an_unrelated_tenant_is_left_alone(self):
        """No RBAC share, so this tenant has no business with that subnet."""
        other_fixture = fixtures.OpenStackFixture()
        other_subnet = factories.SubNetFactory(
            tenant=other_fixture.tenant,
            network=other_fixture.network,
            service_settings=other_fixture.settings,
            project=other_fixture.project,
            backend_id="foreign-subnet-backend-id",
        )

        self._pull(
            {
                ROUTER_BACKEND_ID: [
                    self._interface_port(
                        ROUTER_BACKEND_ID,
                        subnet_backend_id="foreign-subnet-backend-id",
                    )
                ]
            }
        )

        other_subnet.refresh_from_db()
        self.assertIsNone(other_subnet.router)


class PullSubnetRouterOverRbacTest(test.APITestCase):
    """A network shared over RBAC is routed by the tenant that consumes it.

    The interface port then sits on the consumer's router while the subnet
    belongs to the owner, so without special handling the consumer -- the tenant
    that can see least and asks the question this issue is about -- would see no
    router at all on their subnets tab.
    """

    def setUp(self):
        self.owner_fixture = fixtures.OpenStackFixture()
        self.owner = self.owner_fixture.tenant
        self.owner.backend_id = "owner-tenant-backend-id"
        self.owner.save()
        self.network = factories.NetworkFactory(
            tenant=self.owner,
            service_settings=self.owner.service_settings,
            project=self.owner.project,
            backend_id="shared-network-backend-id",
        )
        self.subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.owner,
            service_settings=self.owner.service_settings,
            project=self.owner.project,
            backend_id="shared-subnet-backend-id",
        )
        self.consumer = factories.TenantFactory(
            service_settings=self.owner.service_settings,
            project=self.owner.project,
            backend_id="consumer-tenant-backend-id",
            state=CoreStates.OK,
        )
        models.NetworkRBACPolicy.objects.create(
            network=self.network,
            target_tenant=self.consumer,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.SHARED,
        )
        self.backend = OpenStackBackend(self.owner.service_settings)

    def _port(self, router_backend_id, port_tenant):
        return {
            "id": f"port-on-{router_backend_id}",
            "name": "",
            "description": "",
            "network_id": "shared-network-backend-id",
            "status": "ACTIVE",
            "admin_state_up": True,
            "mac_address": "fa:16:3e:62:b8:95",
            "fixed_ips": [
                {"subnet_id": "shared-subnet-backend-id", "ip_address": "10.50.0.1"}
            ],
            "device_id": router_backend_id,
            "device_owner": "network:router_interface",
            "security_groups": [],
            "allowed_address_pairs": [],
            "port_security_enabled": False,
            "tenant_id": port_tenant.backend_id,
        }

    def _pull(self, tenant, routers_with_ports):
        backend_routers = [
            {
                "id": backend_id,
                "name": f"{backend_id}-name",
                "description": "",
                "routes": [],
                "external_gateway_info": None,
            }
            for backend_id in routers_with_ports
        ]
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_routers.return_value = {"routers": backend_routers}
            client.list_ports.side_effect = lambda device_id, **kwargs: {
                "ports": routers_with_ports[device_id]
            }
            self.backend.pull_tenant_routers(tenant)
        self.subnet.refresh_from_db()

    def test_the_consumer_router_is_recorded_on_the_shared_subnet(self):
        self._pull(
            self.consumer,
            {"consumer-router": [self._port("consumer-router", self.consumer)]},
        )

        self.assertIsNotNone(self.subnet.router)
        self.assertEqual(self.subnet.router.backend_id, "consumer-router")
        self.assertEqual(self.subnet.router.tenant, self.consumer)

    def test_the_owner_router_wins_over_the_consumer_router(self):
        self._pull(
            self.consumer,
            {"consumer-router": [self._port("consumer-router", self.consumer)]},
        )
        self._pull(
            self.owner,
            {"owner-router": [self._port("owner-router", self.owner)]},
        )

        self.assertEqual(self.subnet.router.backend_id, "owner-router")

    def test_a_consumer_pull_never_displaces_the_owner_router(self):
        """It is also what connect_subnet re-attaches to, and the owner's tenant
        session cannot address a router in the consumer's project."""
        self._pull(
            self.owner,
            {"owner-router": [self._port("owner-router", self.owner)]},
        )
        self._pull(
            self.consumer,
            {"consumer-router": [self._port("consumer-router", self.consumer)]},
        )

        self.assertEqual(self.subnet.router.backend_id, "owner-router")

    def test_several_consumers_resolve_deterministically_and_do_not_flap(self):
        """Each consumer's pull sees only its own routers, so "last pull wins"
        would rewrite the value on every sweep. The lowest backend id wins
        whichever tenant pulls first."""
        second = factories.TenantFactory(
            service_settings=self.owner.service_settings,
            project=self.owner.project,
            backend_id="second-consumer-backend-id",
            state=CoreStates.OK,
        )
        models.NetworkRBACPolicy.objects.create(
            network=self.network,
            target_tenant=second,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.SHARED,
        )
        consumer_pull = (
            self.consumer,
            {"consumer-router": [self._port("consumer-router", self.consumer)]},
        )
        second_pull = (second, {"b-router": [self._port("b-router", second)]})

        self._pull(*consumer_pull)
        self._pull(*second_pull)
        after_one_order = self.subnet.router.backend_id

        # And again in the opposite order, plus a repeat sweep.
        self.subnet.router = None
        self.subnet.save()
        self._pull(*second_pull)
        self._pull(*consumer_pull)
        self._pull(*second_pull)

        self.assertEqual(self.subnet.router.backend_id, "b-router")
        self.assertEqual(after_one_order, "b-router")

    def test_the_owner_handing_the_subnet_over_records_the_consumer_router(self):
        """The owner detaching is how a shared network is handed to the tenant
        that consumes it. A remembered owner router must then yield to the
        consumer that is actually routing the subnet -- otherwise the consumer's
        subnets tab names a router that holds nothing, which is the question
        this field exists to answer."""
        self._pull(
            self.owner,
            {"owner-router": [self._port("owner-router", self.owner)]},
        )
        self.assertEqual(self.subnet.router.backend_id, "owner-router")

        # The owner removes its interface; its own pull drops the port.
        self._pull(self.owner, {"owner-router": []})
        self._pull(
            self.consumer,
            {"consumer-router": [self._port("consumer-router", self.consumer)]},
        )

        self.assertEqual(self.subnet.router.backend_id, "consumer-router")

    def test_a_live_owner_attachment_still_beats_a_consumer(self):
        self._pull(
            self.owner,
            {"owner-router": [self._port("owner-router", self.owner)]},
        )
        self._pull(
            self.consumer,
            {"consumer-router": [self._port("consumer-router", self.consumer)]},
        )

        self.assertEqual(self.subnet.router.backend_id, "owner-router")

    def test_a_consumer_that_detached_is_replaced_by_one_that_is_still_routing(self):
        """The lowest backend id wins between consumers, but only among routers
        that actually hold an interface -- otherwise a consumer that let go
        would outrank one that is still routing the subnet, forever."""
        second = factories.TenantFactory(
            service_settings=self.owner.service_settings,
            project=self.owner.project,
            backend_id="second-consumer-backend-id",
            state=CoreStates.OK,
        )
        models.NetworkRBACPolicy.objects.create(
            network=self.network,
            target_tenant=second,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.SHARED,
        )
        self._pull(
            self.consumer,
            {"a-router": [self._port("a-router", self.consumer)]},
        )
        self._pull(second, {"z-router": [self._port("z-router", second)]})
        self.assertEqual(self.subnet.router.backend_id, "a-router")

        # The first consumer removes its interface: its own pull drops the port
        # from that router, and the subnet no longer appears in its pull at all.
        self._pull(self.consumer, {"a-router": []})
        self._pull(second, {"z-router": [self._port("z-router", second)]})

        self.assertEqual(self.subnet.router.backend_id, "z-router")

    def test_a_recorded_consumer_router_is_not_used_for_reconnecting(self):
        """connect_subnet must fall back to the implicit resolution rather than
        authenticate as the owner against a router in the consumer's project."""
        self._pull(
            self.consumer,
            {"consumer-router": [self._port("consumer-router", self.consumer)]},
        )
        self.assertEqual(self.subnet.router.tenant, self.consumer)

        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_routers.return_value = {
                "routers": [{"id": "owner-router", "name": "owner-router-name"}]
            }
            client.show_subnet.return_value = {
                "subnet": {
                    "id": "shared-subnet-backend-id",
                    "gateway_ip": "10.50.0.1",
                }
            }
            client.list_ports.return_value = {"ports": []}
            router_backend_id = self.backend.connect_subnet(self.subnet)

        client.show_router.assert_not_called()
        self.assertEqual(router_backend_id, "owner-router")
