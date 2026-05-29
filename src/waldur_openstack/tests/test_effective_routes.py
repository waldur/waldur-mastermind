from rest_framework import status, test

from waldur_openstack.tests import factories, fixtures


class EffectiveRoutesTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.router = factories.RouterFactory(
            tenant=self.tenant, project=self.tenant.project
        )
        self.url = factories.RouterFactory.get_url(self.router, "effective_routes")

    def _get(self):
        self.client.force_authenticate(user=self.fixture.staff)
        return self.client.get(self.url)

    def test_router_without_anything_returns_empty_routes(self):
        response = self._get()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["routes"], [])
        self.assertFalse(response.data["has_external_gateway"])
        self.assertIsNone(response.data["snat"])

    def test_static_routes_are_emitted(self):
        self.router.routes = [{"destination": "10.10.0.0/16", "nexthop": "10.0.0.254"}]
        self.router.save()
        response = self._get()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        statics = [r for r in response.data["routes"] if r["source"] == "static"]
        self.assertEqual(len(statics), 1)
        self.assertEqual(statics[0]["destination"], "10.10.0.0/16")
        self.assertEqual(statics[0]["nexthop"], "10.0.0.254")

    def test_connected_routes_per_router_interface(self):
        subnet = self.fixture.subnet
        subnet.cidr = "10.0.42.0/24"
        subnet.save()
        port = factories.PortFactory(
            tenant=self.tenant,
            project=self.tenant.project,
            network=self.fixture.network,
            subnet=subnet,
            fixed_ips=[{"subnet_id": subnet.backend_id, "ip_address": "10.0.42.1"}],
        )
        self.router.ports.add(port)

        response = self._get()
        connected = [r for r in response.data["routes"] if r["source"] == "connected"]
        self.assertEqual(len(connected), 1)
        self.assertEqual(connected[0]["destination"], "10.0.42.0/24")
        self.assertIsNone(connected[0]["nexthop"])
        self.assertEqual(connected[0]["ip_on_router"], "10.0.42.1")
        self.assertEqual(connected[0]["subnet_uuid"], str(subnet.uuid))

    def test_default_route_inherits_gateway_subnet_gateway_ip(self):
        ext_net = factories.ExternalNetworkFactory(
            settings=self.tenant.service_settings
        )
        ext_subnet = factories.ExternalSubnetFactory(
            network=ext_net,
            backend_id="ext-sub-1",
            cidr="192.168.240.96/28",
            gateway_ip="192.168.240.97",
        )
        self.router.external_network_ref = ext_net
        self.router.external_network_id = ext_net.backend_id
        self.router.external_fixed_ips = [
            {"subnet_id": ext_subnet.backend_id, "ip_address": "192.168.240.104"}
        ]
        self.router.enable_snat = True
        self.router.save()

        response = self._get()
        self.assertTrue(response.data["has_external_gateway"])
        self.assertTrue(response.data["snat"])
        defaults = [r for r in response.data["routes"] if r["source"] == "default"]
        self.assertEqual(len(defaults), 1)
        d = defaults[0]
        self.assertEqual(d["destination"], "0.0.0.0/0")
        self.assertEqual(d["nexthop"], "192.168.240.97")
        self.assertEqual(d["gateway_ip_on_router"], "192.168.240.104")
        self.assertEqual(d["subnet_uuid"], str(ext_subnet.uuid))
        self.assertEqual(d["external_network_uuid"], str(ext_net.uuid))

    def test_default_route_emitted_even_when_subnet_not_yet_synced(self):
        ext_net = factories.ExternalNetworkFactory(
            settings=self.tenant.service_settings
        )
        self.router.external_network_ref = ext_net
        self.router.external_network_id = ext_net.backend_id
        self.router.external_fixed_ips = [
            {"subnet_id": "missing-subnet", "ip_address": "10.0.0.5"}
        ]
        self.router.enable_snat = False
        self.router.save()

        response = self._get()
        defaults = [r for r in response.data["routes"] if r["source"] == "default"]
        self.assertEqual(len(defaults), 1)
        d = defaults[0]
        self.assertEqual(d["destination"], "0.0.0.0/0")
        self.assertIsNone(d["nexthop"])
        self.assertFalse(response.data["snat"])

    def test_project_member_can_view_routes(self):
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
