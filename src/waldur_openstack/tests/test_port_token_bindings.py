"""A personal access token acts only within its bindings when it puts a port
into a tenant or uses a network owner's settings.

Waldur creates ports with the cloud's admin session, so these checks are the
only ones made. A token bound to one project must not use its user's roles in
another project's tenants or networks.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.enums import CoreStates
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.tests.pat_utils import use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.models import NetworkRBACPolicy, Port

from . import factories, fixtures

# update_ports asks for it, so a token must carry it to reach the port checks.
MANAGE_INSTANCE = PermissionEnum.CAN_MANAGE_OPENSTACK_INSTANCE.value


class TwoProjectsMixin:
    """A user who administers projects A and B, each with a tenant."""

    def setUp(self):
        super().setUp()
        self.a = fixtures.OpenStackFixture()
        self.b = fixtures.OpenStackFixture()
        # Both tenants live in one cloud, as a share between them requires.
        self.b.settings = self.a.settings
        self.user = structure_factories.UserFactory()
        self.a.project.add_user(self.user, ProjectRole.ADMIN)
        self.b.project.add_user(self.user, ProjectRole.ADMIN)

    def share_b_network_with_a(self):
        NetworkRBACPolicy.objects.create(
            network=self.b.network,
            target_tenant=self.a.tenant,
            policy_type=NetworkRBACPolicy.NetworkShareType.SHARED,
        )


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_openstack.executors.PortCreateExecutor.execute")
class PortCreateTest(TwoProjectsMixin, test.APITestCase):
    url = factories.PortFactory.get_list_url()

    def post(self, network, target_tenant=None, **extra):
        data = {"name": "port", "network": factories.NetworkFactory.get_url(network)}
        if target_tenant:
            data["target_tenant"] = factories.TenantFactory.get_url(target_tenant)
        return self.client.post(self.url, {**data, **extra}, format="json")

    def test_token_bound_to_the_project_creates_a_port_there(self, execute):
        use_pat(self.client, self.user, bindings=[self.a.project])

        response = self.post(self.a.network)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        execute.assert_called_once()

    def test_token_bound_to_the_organization_creates_a_port_there(self, execute):
        use_pat(self.client, self.user, bindings=[self.a.customer])

        response = self.post(self.a.network)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_token_bound_to_another_project_cannot(self, execute):
        use_pat(self.client, self.user, bindings=[self.a.project])

        response = self.post(self.b.network)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("network", response.data)
        execute.assert_not_called()
        self.assertFalse(Port.objects.exists())

    def test_target_tenant_outside_the_binding_is_refused(self, execute):
        NetworkRBACPolicy.objects.create(
            network=self.a.network,
            target_tenant=self.b.tenant,
            policy_type=NetworkRBACPolicy.NetworkShareType.SHARED,
        )
        use_pat(self.client, self.user, bindings=[self.a.project])

        response = self.post(self.a.network, target_tenant=self.b.tenant)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("target_tenant", response.data)
        self.assertFalse(Port.objects.exists())

    def test_unbound_token_acts_with_the_users_roles(self, execute):
        use_pat(self.client, self.user)

        self.assertEqual(self.post(self.a.network).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.post(self.b.network).status_code, status.HTTP_201_CREATED)

    def test_pinning_an_address_on_a_network_outside_the_binding_is_refused(
        self, execute
    ):
        self.share_b_network_with_a()
        pinned = {
            "fixed_ips": [
                {"ip_address": "192.168.42.100", "subnet_id": self.b.subnet.backend_id}
            ]
        }

        use_pat(self.client, self.user, bindings=[self.a.project])
        refused = self.post(self.b.network, target_tenant=self.a.tenant, **pinned)
        use_pat(self.client, self.user)
        accepted = self.post(self.b.network, target_tenant=self.a.tenant, **pinned)

        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(accepted.status_code, status.HTTP_201_CREATED, accepted.data)


@override_config(PAT_ENABLED=True)
class InstancePortsTest(TwoProjectsMixin, test.APITestCase):
    """Pinning an address on a shared network when changing a VM's ports."""

    def setUp(self):
        super().setUp()
        self.share_b_network_with_a()
        self.instance = self.a.instance
        self.instance.state = CoreStates.OK
        self.instance.save()
        self.url = factories.InstanceFactory.get_url(self.instance, "update_ports")
        self.ports = {
            "ports": [
                {
                    "subnet": factories.SubNetFactory.get_url(self.b.subnet),
                    "fixed_ips": [
                        {
                            "ip_address": "192.168.42.100",
                            "subnet_id": self.b.subnet.backend_id,
                        }
                    ],
                }
            ]
        }

    @mock.patch("waldur_openstack.executors.InstancePortsUpdateExecutor.execute")
    def test_token_bound_to_the_instance_project_cannot_pin_on_the_owners_network(
        self, execute
    ):
        use_pat(self.client, self.user, MANAGE_INSTANCE, bindings=[self.a.project])

        response = self.client.post(self.url, self.ports, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        execute.assert_not_called()

    @mock.patch("waldur_openstack.executors.InstancePortsUpdateExecutor.execute")
    def test_unbound_token_of_the_networks_owner_can(self, execute):
        use_pat(self.client, self.user, MANAGE_INSTANCE)

        response = self.client.post(self.url, self.ports, format="json")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_openstack.backend.OpenStackBackend.disable_port_security")
class PortSecurityTest(TwoProjectsMixin, test.APITestCase):
    """Turning port security off on a port that sits on another tenant's network."""

    def setUp(self):
        super().setUp()
        self.share_b_network_with_a()
        self.port = factories.PortFactory(
            service_settings=self.a.settings,
            project=self.a.project,
            tenant=self.a.tenant,
            network=self.b.network,
            port_security_enabled=True,
            state=CoreStates.OK,
        )
        self.url = factories.PortFactory.get_url(self.port, "disable_port_security")

    def test_token_bound_to_the_ports_project_cannot(self, backend):
        use_pat(self.client, self.user, bindings=[self.a.project])

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        backend.assert_not_called()

    def test_unbound_token_of_the_networks_owner_can(self, backend):
        use_pat(self.client, self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


@override_config(PAT_ENABLED=True)
class ShareTest(TwoProjectsMixin, test.APITestCase):
    """A token bound to A must not share B's network into A's tenant, after
    which the port checks would let it onto that network."""

    url = "/api/openstack-network-rbac-policies/"

    def setUp(self):
        super().setUp()
        neutron = mock.patch("waldur_openstack.backend.get_neutron_client").start()
        neutron().create_rbac_policy.return_value = {"rbac_policy": {"id": "1"}}
        mock.patch("waldur_openstack.backend.get_keystone_session").start()
        self.addCleanup(mock.patch.stopall)

    def share(self):
        return self.client.post(
            self.url,
            {
                "network": factories.NetworkFactory.get_url(self.b.network),
                "target_tenant": factories.TenantFactory.get_url(self.a.tenant),
                "policy_type": NetworkRBACPolicy.NetworkShareType.SHARED,
            },
        )

    def test_token_bound_to_the_target_project_cannot(self):
        use_pat(self.client, self.user, bindings=[self.a.project])

        response = self.share()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(NetworkRBACPolicy.objects.exists())

    def test_unbound_token_of_who_manages_both_sides_can(self):
        use_pat(self.client, self.user)

        response = self.share()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_openstack.executors.RouterCreateExecutor.execute")
class RouterCreateTest(TwoProjectsMixin, test.APITestCase):
    def create(self):
        return self.client.post(
            factories.RouterFactory.get_list_url(),
            {
                "name": "router",
                "tenant": factories.TenantFactory.get_url(self.b.tenant),
            },
        )

    def test_token_bound_to_another_project_cannot(self, execute):
        use_pat(self.client, self.user, bindings=[self.a.project])

        self.assertEqual(self.create().status_code, status.HTTP_400_BAD_REQUEST)
        execute.assert_not_called()

    def test_unbound_token_can(self, execute):
        use_pat(self.client, self.user)

        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_openstack.executors.LoadBalancerCreateExecutor.execute")
class LoadBalancerCreateTest(TwoProjectsMixin, test.APITestCase):
    """The load balancer's VIP is a Neutron port in the tenant."""

    def create(self):
        return self.client.post(
            factories.LoadBalancerFactory.get_list_url(),
            {
                "name": "lb",
                "tenant": factories.TenantFactory.get_url(self.b.tenant),
                "vip_subnet": factories.SubNetFactory.get_url(self.b.subnet),
            },
        )

    def test_token_bound_to_another_project_cannot(self, execute):
        use_pat(self.client, self.user, bindings=[self.a.project])

        self.assertEqual(self.create().status_code, status.HTTP_400_BAD_REQUEST)
        execute.assert_not_called()

    def test_unbound_token_can(self, execute):
        use_pat(self.client, self.user)

        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
