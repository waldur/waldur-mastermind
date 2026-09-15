"""IPv6 subnets.

The address family follows the CIDR rather than being a field of its own, so a
request cannot say one thing in ``ip_version`` and another in ``cidr``. What
Neutron would reject -- an address mode on an IPv4 subnet, two modes that
disagree, SLAAC on a prefix other than /64, a gateway or nameserver of the other
family -- is rejected here with a 400 instead, so the subnet never reaches the
backend only to end up ERRED.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend

from . import factories, fixtures

IPV6_CIDR = "2001:db8:1::/64"


@mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
class CreateIpv6SubnetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(self.fixture.owner)
        self.url = factories.NetworkFactory.get_url(
            network=self.fixture.network, action="create_subnet"
        )

    def _post(self, **extra):
        return self.client.post(self.url, {"name": "v6-subnet", **extra}, format="json")

    def test_the_address_family_is_taken_from_the_cidr(self, executor):
        response = self._post(
            cidr=IPV6_CIDR, ipv6_ra_mode="slaac", ipv6_address_mode="slaac"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["ip_version"], 6)
        self.assertEqual(response.data["ipv6_ra_mode"], "slaac")
        self.assertEqual(response.data["ipv6_address_mode"], "slaac")
        subnet = models.SubNet.objects.get(uuid=response.data["uuid"])
        self.assertEqual(subnet.ip_version, 6)
        self.assertEqual(subnet.cidr, IPV6_CIDR)

    def test_a_fully_expanded_ipv6_cidr_fits(self, executor):
        cidr = "2001:0db8:0001:0000:0000:0000:0000:0000/64"

        response = self._post(cidr=cidr)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["ip_version"], 6)

    def test_an_ipv6_gateway_is_accepted(self, executor):
        response = self._post(cidr=IPV6_CIDR, gateway_ip="2001:db8:1::1")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["gateway_ip"], "2001:db8:1::1")

    def test_the_modes_are_optional(self, executor):
        response = self._post(cidr=IPV6_CIDR)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data["ipv6_ra_mode"])
        self.assertIsNone(response.data["ipv6_address_mode"])

    def test_one_mode_alone_is_accepted(self, executor):
        response = self._post(cidr=IPV6_CIDR, ipv6_address_mode="dhcpv6-stateful")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data["ipv6_ra_mode"])

    def test_stateful_dhcpv6_is_not_tied_to_a_64(self, executor):
        response = self._post(
            cidr="2001:db8:1::/56",
            ipv6_ra_mode="dhcpv6-stateful",
            ipv6_address_mode="dhcpv6-stateful",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_ipv4_creation_is_unchanged(self, executor):
        response = self._post()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["cidr"], "192.168.42.0/24")
        self.assertEqual(response.data["ip_version"], 4)
        self.assertIsNone(response.data["ipv6_ra_mode"])

    def test_default_nameservers_of_the_other_family_are_left_out(self, executor):
        settings = self.fixture.network.service_settings
        settings.options["dns_nameservers"] = ["8.8.8.8", "2001:4860:4860::8888"]
        settings.save()

        v6 = self._post(cidr=IPV6_CIDR)
        self.fixture.network.subnets.all().delete()
        v4 = self._post()

        self.assertEqual(v6.data["dns_nameservers"], ["2001:4860:4860::8888"])
        self.assertEqual(v4.data["dns_nameservers"], ["8.8.8.8"])

    def test_no_subnet_is_created_by_a_rejected_request(self, executor):
        self._post(
            cidr=IPV6_CIDR, ipv6_ra_mode="slaac", ipv6_address_mode="dhcpv6-stateful"
        )

        self.assertFalse(self.fixture.network.subnets.exists())
        executor.assert_not_called()


@mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
class RejectInvalidIpv6SubnetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(self.fixture.owner)
        self.url = factories.NetworkFactory.get_url(
            network=self.fixture.network, action="create_subnet"
        )

    def _assert_rejected(self, field, **extra):
        response = self.client.post(
            self.url, {"name": "v6-subnet", **extra}, format="json"
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn(field, response.data)
        return response

    def test_a_mode_on_an_ipv4_subnet(self, executor):
        self._assert_rejected(
            "ipv6_ra_mode", cidr="192.168.50.0/24", ipv6_ra_mode="slaac"
        )

    def test_modes_that_disagree(self, executor):
        self._assert_rejected(
            "ipv6_address_mode",
            cidr=IPV6_CIDR,
            ipv6_ra_mode="slaac",
            ipv6_address_mode="dhcpv6-stateful",
        )

    def test_slaac_on_a_prefix_other_than_64(self, executor):
        self._assert_rejected("cidr", cidr="2001:db8:1::/56", ipv6_address_mode="slaac")

    def test_stateless_dhcpv6_on_a_prefix_other_than_64(self, executor):
        self._assert_rejected(
            "cidr", cidr="2001:db8:1::/80", ipv6_ra_mode="dhcpv6-stateless"
        )

    def test_an_ipv4_gateway_on_an_ipv6_subnet(self, executor):
        self._assert_rejected("gateway_ip", cidr=IPV6_CIDR, gateway_ip="192.168.1.1")

    def test_an_ipv6_gateway_on_an_ipv4_subnet(self, executor):
        self._assert_rejected(
            "gateway_ip", cidr="192.168.50.0/24", gateway_ip="2001:db8:1::1"
        )

    def test_an_ipv6_gateway_on_the_default_ipv4_cidr(self, executor):
        self._assert_rejected("gateway_ip", gateway_ip="2001:db8:1::1")

    def test_an_ipv6_mode_without_a_cidr(self, executor):
        response = self._assert_rejected("cidr", ipv6_ra_mode="slaac")

        self.assertIn("IPv6", str(response.data["cidr"]))

    def test_a_nameserver_of_the_other_family(self, executor):
        self._assert_rejected(
            "dns_nameservers", cidr=IPV6_CIDR, dns_nameservers=["8.8.8.8"]
        )

    def test_a_cidr_that_is_not_one(self, executor):
        self._assert_rejected("cidr", cidr="not-a-network")

    def test_an_unknown_mode(self, executor):
        self._assert_rejected("ipv6_ra_mode", cidr=IPV6_CIDR, ipv6_ra_mode="dhcpv6-pd")


@mock.patch("waldur_openstack.executors.SubNetUpdateExecutor.execute")
class UpdateIpv6SubnetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(self.fixture.staff)
        self.subnet = factories.SubNetFactory(
            network=self.fixture.network,
            tenant=self.fixture.tenant,
            service_settings=self.fixture.network.service_settings,
            project=self.fixture.network.project,
            cidr=IPV6_CIDR,
            ip_version=6,
            gateway_ip="2001:db8:1::1",
            ipv6_ra_mode="slaac",
            ipv6_address_mode="slaac",
            state=CoreStates.OK,
        )
        self.url = factories.SubNetFactory.get_url(self.subnet)

    def test_the_gateway_can_move_within_the_family(self, executor):
        response = self.client.patch(self.url, {"gateway_ip": "2001:db8:1::fe"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.subnet.refresh_from_db()
        self.assertEqual(self.subnet.gateway_ip, "2001:db8:1::fe")

    def test_the_gateway_cannot_change_family(self, executor):
        response = self.client.patch(self.url, {"gateway_ip": "192.168.1.1"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("gateway_ip", response.data)

    def test_the_modes_and_family_are_read_only(self, executor):
        response = self.client.patch(
            self.url,
            {
                "ipv6_ra_mode": "dhcpv6-stateful",
                "ipv6_address_mode": "dhcpv6-stateful",
                "ip_version": 4,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.subnet.refresh_from_db()
        self.assertEqual(self.subnet.ipv6_ra_mode, "slaac")
        self.assertEqual(self.subnet.ipv6_address_mode, "slaac")
        self.assertEqual(self.subnet.ip_version, 6)


class Ipv6SubnetBackendTest(test.APITestCase):
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
        self.backend = OpenStackBackend(self.tenant.service_settings)

    def _backend_subnet(self, **overrides):
        return {
            "id": "subnet-backend-id",
            "name": "v6-subnet",
            "description": "",
            "network_id": "network-backend-id",
            "cidr": IPV6_CIDR,
            "ip_version": 6,
            "gateway_ip": "2001:db8:1::1",
            "enable_dhcp": True,
            "allocation_pools": [
                {"start": "2001:db8:1::2", "end": "2001:db8:1:0:ffff:ffff:ffff:ffff"}
            ],
            "dns_nameservers": [],
            "host_routes": [],
            "ipv6_ra_mode": "slaac",
            "ipv6_address_mode": "slaac",
            **overrides,
        }

    def test_create_sends_the_family_and_the_modes(self):
        subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="",
            cidr=IPV6_CIDR,
            ip_version=6,
            ipv6_ra_mode="slaac",
            ipv6_address_mode="slaac",
        )
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.create_subnet.return_value = {"subnet": self._backend_subnet()}
            self.backend.create_subnet(subnet, skip_router_connection=True)

        sent = client.create_subnet.call_args.args[0]["subnet"]
        self.assertEqual(sent["ip_version"], 6)
        self.assertEqual(sent["cidr"], IPV6_CIDR)
        self.assertEqual(sent["ipv6_ra_mode"], "slaac")
        self.assertEqual(sent["ipv6_address_mode"], "slaac")

    def test_create_leaves_unset_modes_out(self):
        subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="",
            cidr="192.168.50.0/24",
        )
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.create_subnet.return_value = {
                "subnet": self._backend_subnet(
                    cidr="192.168.50.0/24",
                    ip_version=4,
                    gateway_ip="192.168.50.1",
                    ipv6_ra_mode=None,
                    ipv6_address_mode=None,
                )
            }
            self.backend.create_subnet(subnet, skip_router_connection=True)

        sent = client.create_subnet.call_args.args[0]["subnet"]
        self.assertNotIn("ipv6_ra_mode", sent)
        self.assertNotIn("ipv6_address_mode", sent)

    def test_a_pulled_subnet_keeps_its_modes(self):
        imported = self.backend._backend_subnet_to_subnet(self._backend_subnet())

        self.assertEqual(imported.ip_version, 6)
        self.assertEqual(imported.ipv6_ra_mode, "slaac")
        self.assertEqual(imported.ipv6_address_mode, "slaac")

    def test_unset_modes_are_pulled_as_none(self):
        imported = self.backend._backend_subnet_to_subnet(
            self._backend_subnet(ipv6_ra_mode=None, ipv6_address_mode=None)
        )

        self.assertIsNone(imported.ipv6_ra_mode)
        self.assertIsNone(imported.ipv6_address_mode)

    def test_the_modes_are_synced_on_pull(self):
        self.assertIn("ipv6_ra_mode", models.SubNet.get_backend_fields())
        self.assertIn("ipv6_address_mode", models.SubNet.get_backend_fields())
