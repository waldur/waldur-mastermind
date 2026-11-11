"""A network shared as access_as_external is for router gateways and floating IPs.

Neutron lets the tenant a network is shared with this way use it as an external
network, but not put ports on it: that takes access_as_shared. Waldur creates
ports and router interfaces with the cloud's admin session, which Neutron does
not hold to that rule, so Waldur refuses them itself.
"""

from unittest import mock

from ddt import data, ddt, unpack
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_openstack.models import NetworkRBACPolicy, Port

from . import factories, fixtures

SHARED = NetworkRBACPolicy.NetworkShareType.SHARED
EXTERNAL = NetworkRBACPolicy.NetworkShareType.EXTERNAL


class ExternalShareMixin:
    """The owner's network, shared with the consumer's tenant in one cloud."""

    def setUp(self):
        super().setUp()
        self.owner = fixtures.OpenStackFixture()
        self.consumer = fixtures.OpenStackFixture()
        self.consumer.settings = self.owner.settings
        self.network = self.owner.network
        self.subnet = self.owner.subnet
        self.client.force_authenticate(self.consumer.owner)

    def share(self, *policy_types):
        for policy_type in policy_types:
            NetworkRBACPolicy.objects.create(
                network=self.network,
                target_tenant=self.consumer.tenant,
                policy_type=policy_type,
            )


@ddt
@mock.patch("waldur_openstack.executors.PortCreateExecutor.execute")
class PortCreateTest(ExternalShareMixin, test.APITestCase):
    @data(
        ((SHARED,), status.HTTP_201_CREATED),
        ((EXTERNAL,), status.HTTP_400_BAD_REQUEST),
        ((SHARED, EXTERNAL), status.HTTP_201_CREATED),
    )
    @unpack
    def test_only_a_shared_network_takes_a_port(self, policy_types, expected, execute):
        self.share(*policy_types)

        response = self.client.post(
            factories.PortFactory.get_list_url(),
            {
                "name": "port",
                "network": factories.NetworkFactory.get_url(self.network),
                "target_tenant": factories.TenantFactory.get_url(self.consumer.tenant),
            },
            format="json",
        )

        self.assertEqual(response.status_code, expected, response.data)
        self.assertEqual(execute.called, expected == status.HTTP_201_CREATED)
        if expected == status.HTTP_400_BAD_REQUEST:
            self.assertIn("target_tenant", response.data)
            self.assertFalse(Port.objects.exists())


@ddt
@mock.patch("waldur_openstack.executors.InstancePortsUpdateExecutor.execute")
class InstancePortsTest(ExternalShareMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.instance = self.consumer.instance
        self.instance.state = CoreStates.OK
        self.instance.save()

    @data((SHARED, status.HTTP_202_ACCEPTED), (EXTERNAL, status.HTTP_400_BAD_REQUEST))
    @unpack
    def test_only_a_shared_network_takes_a_port(self, policy_type, expected, execute):
        self.share(policy_type)

        response = self.client.post(
            factories.InstanceFactory.get_url(self.instance, "update_ports"),
            {"ports": [{"subnet": factories.SubNetFactory.get_url(self.subnet)}]},
            format="json",
        )

        self.assertEqual(response.status_code, expected, response.data)
        self.assertEqual(
            self.instance.ports.filter(subnet=self.subnet).exists(),
            expected == status.HTTP_202_ACCEPTED,
        )
        if expected == status.HTTP_400_BAD_REQUEST:
            self.assertIn("access_as_shared", str(response.data))


@ddt
class RouterInterfaceTest(ExternalShareMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.router = factories.RouterFactory(
            tenant=self.consumer.tenant,
            service_settings=self.owner.settings,
            project=self.consumer.project,
            state=CoreStates.OK,
            backend_id="consumer-router",
        )
        patcher = mock.patch(
            "waldur_openstack.backend.OpenStackBackend.get_free_ip",
            return_value="192.168.42.10",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("create_port", "add_router_interface", "pull_tenant_routers"):
            patcher = mock.patch(f"waldur_openstack.backend.OpenStackBackend.{name}")
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    @data((SHARED, status.HTTP_202_ACCEPTED), (EXTERNAL, status.HTTP_400_BAD_REQUEST))
    @unpack
    def test_only_a_shared_subnet_takes_an_interface(self, policy_type, expected):
        self.share(policy_type)

        response = self.client.post(
            factories.RouterFactory.get_url(self.router, "add_router_interface"),
            {"subnet": factories.SubNetFactory.get_url(self.subnet)},
        )

        self.assertEqual(response.status_code, expected, response.data)
        self.assertEqual(
            self.add_router_interface.called, expected == status.HTTP_202_ACCEPTED
        )

    def left_over_port(self):
        # A port of the consumer's on the owner's network: created before the
        # share was narrowed or revoked, or before shares were checked.
        return factories.PortFactory(
            tenant=self.consumer.tenant,
            service_settings=self.owner.settings,
            project=self.consumer.project,
            network=self.network,
            subnet=self.subnet,
            state=CoreStates.OK,
            backend_id="left-over-port",
        )

    @data(
        ((SHARED,), status.HTTP_202_ACCEPTED),
        ((EXTERNAL,), status.HTTP_400_BAD_REQUEST),
        ((), status.HTTP_400_BAD_REQUEST),
    )
    @unpack
    def test_only_a_port_on_a_shared_network_takes_an_interface(
        self, policy_types, expected
    ):
        self.share(*policy_types)
        port = self.left_over_port()

        response = self.client.post(
            factories.RouterFactory.get_url(self.router, "add_router_interface"),
            {"port": factories.PortFactory.get_url(port)},
        )

        self.assertEqual(response.status_code, expected, response.data)
        self.assertEqual(
            self.add_router_interface.called, expected == status.HTTP_202_ACCEPTED
        )

    @data("subnet", "port")
    @mock.patch("waldur_openstack.executors.RouterInterfaceDeleteExecutor.execute")
    def test_an_interface_on_an_external_share_can_still_be_removed(
        self, field, execute
    ):
        self.share(EXTERNAL)
        port = self.left_over_port()
        target = {
            "subnet": factories.SubNetFactory.get_url(self.subnet),
            "port": factories.PortFactory.get_url(port),
        }[field]

        response = self.client.post(
            factories.RouterFactory.get_url(self.router, "remove_router_interface"),
            {field: target},
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        execute.assert_called_once()

    def test_a_port_whose_network_is_unknown_takes_no_interface(self):
        # A pull leaves the network empty when Waldur does not mirror it, as
        # after a share is gone; the port's subnet then names it, or nothing.
        port = self.left_over_port()
        port.network = None
        port.subnet = None
        port.save()

        response = self.client.post(
            factories.RouterFactory.get_url(self.router, "add_router_interface"),
            {"port": factories.PortFactory.get_url(port)},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.add_router_interface.assert_not_called()

    def test_a_port_with_only_its_subnet_known_is_checked_by_it(self):
        port = self.left_over_port()
        port.network = None
        port.save()

        response = self.client.post(
            factories.RouterFactory.get_url(self.router, "add_router_interface"),
            {"port": factories.PortFactory.get_url(port)},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.add_router_interface.assert_not_called()

    @mock.patch("waldur_openstack.executors.RouterInterfaceDeleteExecutor.execute")
    def test_the_routers_own_interface_comes_off_by_subnet_after_the_share_is_gone(
        self, execute
    ):
        self.subnet.router = self.router
        self.subnet.save()

        response = self.client.post(
            factories.RouterFactory.get_url(self.router, "remove_router_interface"),
            {"subnet": factories.SubNetFactory.get_url(self.subnet)},
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        execute.assert_called_once()
