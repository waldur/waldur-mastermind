from rest_framework import status, test

from waldur_openstack import models
from waldur_openstack.tests import factories, fixtures


class TenantTopologyTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.url = factories.TenantFactory.get_url(self.tenant, "topology")

    def _get(self, user):
        self.client.force_authenticate(user=user)
        return self.client.get(self.url)

    def test_empty_tenant_returns_only_the_tenant_node(self):
        response = self._get(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        node_ids = {n["id"] for n in response.data["nodes"]}
        self.assertEqual(node_ids, {f"tenant:{self.tenant.uuid.hex}"})
        self.assertEqual(response.data["edges"], [])

    def test_full_wiring_emits_expected_nodes_and_edges(self):
        network = self.fixture.network
        subnet = self.fixture.subnet
        router = factories.RouterFactory(
            tenant=self.tenant, project=self.tenant.project
        )
        port = factories.PortFactory(
            tenant=self.tenant,
            project=self.tenant.project,
            network=network,
            subnet=subnet,
        )
        router.ports.add(port)

        response = self._get(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        node_types = {n["type"]: n for n in response.data["nodes"]}
        for expected in ("tenant", "network", "subnet", "router", "port"):
            self.assertIn(expected, node_types, f"missing node type {expected}")

        edge_pairs = {
            (e["source"], e["target"], e["kind"]) for e in response.data["edges"]
        }
        tenant_id = f"tenant:{self.tenant.uuid.hex}"
        network_id = f"network:{network.uuid.hex}"
        subnet_id = f"subnet:{subnet.uuid.hex}"
        router_id = f"router:{router.uuid.hex}"
        port_id = f"port:{port.uuid.hex}"

        self.assertIn((tenant_id, network_id, "contains"), edge_pairs)
        self.assertIn((tenant_id, router_id, "contains"), edge_pairs)
        self.assertIn((network_id, subnet_id, "has_subnet"), edge_pairs)
        self.assertIn((subnet_id, port_id, "has_port"), edge_pairs)
        self.assertIn((router_id, port_id, "has_interface"), edge_pairs)

    def test_router_with_external_gateway_links_to_external_network(self):
        ext_net = factories.ExternalNetworkFactory(
            settings=self.tenant.service_settings
        )
        router = factories.RouterFactory(
            tenant=self.tenant,
            project=self.tenant.project,
            external_network_ref=ext_net,
            external_network_id=ext_net.backend_id,
        )

        response = self._get(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        ext_id = f"external_network:{ext_net.uuid.hex}"
        router_id = f"router:{router.uuid.hex}"
        edge_pairs = {
            (e["source"], e["target"], e["kind"]) for e in response.data["edges"]
        }
        self.assertIn((router_id, ext_id, "gateway"), edge_pairs)

    def test_floating_ip_attached_to_port_is_linked(self):
        port = factories.PortFactory(
            tenant=self.tenant,
            project=self.tenant.project,
            network=self.fixture.network,
        )
        fip = factories.FloatingIPFactory(
            tenant=self.tenant,
            project=self.tenant.project,
            service_settings=self.tenant.service_settings,
            port=port,
            address="10.0.0.10",
        )

        response = self._get(self.fixture.staff)
        fip_id = f"floating_ip:{fip.uuid.hex}"
        port_id = f"port:{port.uuid.hex}"
        edge_pairs = {
            (e["source"], e["target"], e["kind"]) for e in response.data["edges"]
        }
        self.assertIn((fip_id, port_id, "floating_for"), edge_pairs)

    def test_inbound_rbac_share_appears_as_rbac_share_node(self):
        other_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
        )
        other_network = factories.NetworkFactory(
            tenant=other_tenant,
            project=self.tenant.project,
            service_settings=self.tenant.service_settings,
        )
        policy = models.NetworkRBACPolicy.objects.create(
            network=other_network,
            target_tenant=self.tenant,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.EXTERNAL,
            backend_id="rbac-1",
        )

        response = self._get(self.fixture.staff)
        share_id = f"rbac_share:{policy.uuid.hex}"
        tenant_id = f"tenant:{self.tenant.uuid.hex}"
        edge_pairs = {
            (e["source"], e["target"], e["kind"]) for e in response.data["edges"]
        }
        self.assertIn((share_id, tenant_id, "shared_with"), edge_pairs)

    def test_project_member_can_view_topology(self):
        response = self._get(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
