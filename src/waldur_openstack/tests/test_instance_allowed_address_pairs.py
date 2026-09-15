"""Allowed address pairs set through the instance action.

`update_allowed_address_pairs` used to take the pairs as a bare JSON value and
push them to Neutron unchecked, so a range the port action refuses -- the
any-address range above all, which lets the port bypass source-restricted
security group rules for every port sharing the group -- went straight to the
backend. Both routes now share one validator; the parity test below keeps them
from drifting apart again.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.core.enums import CoreStates

from . import factories, fixtures

REJECTED = (
    "0.0.0.0/0",
    "::/0",
    "8.8.8.8",
    "169.254.169.254",
    "224.0.0.1",
    "fe80::/64",
    "not-an-ip",
)


class InstanceAllowedAddressPairsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = self.fixture.subnet
        self.subnet.cidr = "192.168.42.0/24"
        self.subnet.gateway_ip = "192.168.42.1"
        self.subnet.save()
        self.port = factories.PortFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            network=self.subnet.network,
            subnet=self.subnet,
            instance=self.fixture.instance,
            allowed_address_pairs=[],
            state=CoreStates.OK,
        )
        executor = mock.patch(
            "waldur_openstack.executors.InstanceAllowedAddressPairsUpdateExecutor.execute"
        )
        self.executor = executor.start()
        self.addCleanup(executor.stop)
        self.client.force_authenticate(self.fixture.admin)
        self.url = factories.InstanceFactory.get_url(
            self.fixture.instance, action="update_allowed_address_pairs"
        )

    def _post(self, pairs):
        return self.client.post(
            self.url,
            {
                "subnet": factories.SubNetFactory.get_url(self.subnet),
                "allowed_address_pairs": pairs,
            },
            format="json",
        )

    def test_escalation_ranges_are_rejected_before_reaching_neutron(self):
        for value in REJECTED:
            response = self._post([{"ip_address": value}])

            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, f"{value!r} accepted"
            )
            self.assertIn("allowed_address_pairs", response.data)
        self.executor.assert_not_called()
        self.port.refresh_from_db()
        self.assertEqual(self.port.allowed_address_pairs, [])

    def test_private_pairs_are_normalised_and_applied(self):
        response = self._post(
            [
                {"ip_address": "10.0.0.10", "mac_address": "AA:BB:CC:DD:EE:FF"},
                {"ip_address": "172.16.5.0/24"},
            ]
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        expected = [
            {"ip_address": "10.0.0.10/32", "mac_address": "aa:bb:cc:dd:ee:ff"},
            {"ip_address": "172.16.5.0/24"},
        ]
        self.port.refresh_from_db()
        self.assertEqual(self.port.allowed_address_pairs, expected)
        self.assertEqual(
            self.executor.call_args.kwargs["allowed_address_pairs"], expected
        )

    def test_an_empty_list_clears_the_pairs(self):
        response = self._post([])

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    def test_a_pair_containing_the_subnet_gateway_is_rejected(self):
        for value in ("192.168.42.1", "192.168.42.0/24", "192.168.0.0/16"):
            response = self._post([{"ip_address": value}])

            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, f"{value!r} accepted"
            )
        self.executor.assert_not_called()

    def test_a_pair_next_to_the_gateway_is_accepted(self):
        response = self._post([{"ip_address": "192.168.42.50"}])

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    def test_the_list_limits_apply(self):
        too_many = [{"ip_address": f"10.1.{i // 250}.{i % 250 + 1}"} for i in range(65)]
        duplicate = [{"ip_address": "10.0.0.10"}, {"ip_address": "10.0.0.10"}]
        bad_mac = [{"ip_address": "10.0.0.10", "mac_address": "zz"}]

        for pairs in (too_many, duplicate, bad_mac, "10.0.0.10"):
            response = self._post(pairs)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.executor.assert_not_called()


class PortAllowedAddressPairsGatewayTest(test.APITestCase):
    """The port action gains the gateway check its docstring always promised."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = self.fixture.subnet
        self.subnet.cidr = "192.168.42.0/24"
        self.subnet.gateway_ip = "192.168.42.1"
        self.subnet.save()
        self.port = factories.PortFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            network=self.subnet.network,
            fixed_ips=[
                {"subnet_id": self.subnet.backend_id, "ip_address": "192.168.42.10"}
            ],
            state=CoreStates.OK,
        )
        backend = mock.patch(
            "waldur_openstack.backend.OpenStackBackend.set_port_allowed_address_pairs"
        )
        self.backend = backend.start()
        self.addCleanup(backend.stop)
        self.client.force_authenticate(self.fixture.staff)
        self.url = factories.PortFactory.get_url(self.port, "set_allowed_address_pairs")

    def test_a_pair_containing_the_gateway_of_a_fixed_ip_subnet_is_rejected(self):
        response = self.client.post(
            self.url,
            {"allowed_address_pairs": [{"ip_address": "192.168.42.0/24"}]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.backend.assert_not_called()


class AllowedAddressPairsParityTest(test.APITestCase):
    """The same entries are accepted or refused by both actions."""

    CASES = (
        [{"ip_address": "10.0.0.10"}],
        [{"ip_address": "192.168.42.0/24"}],
        [{"ip_address": "10.0.0.10", "mac_address": "aa:bb:cc:dd:ee:ff"}],
        [{"ip_address": "10.0.0.10", "mac_address": "zz"}],
        [{"ip_address": "10.0.0.10"}, {"ip_address": "10.0.0.10"}],
        *([{"ip_address": value}] for value in REJECTED),
    )

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = self.fixture.subnet
        self.subnet.cidr = "10.0.0.0/24"
        self.subnet.gateway_ip = "10.0.0.1"
        self.subnet.save()
        self.port = factories.PortFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            network=self.subnet.network,
            subnet=self.subnet,
            instance=self.fixture.instance,
            state=CoreStates.OK,
        )
        for target in (
            "waldur_openstack.executors.InstanceAllowedAddressPairsUpdateExecutor.execute",
            "waldur_openstack.backend.OpenStackBackend.set_port_allowed_address_pairs",
        ):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_authenticate(self.fixture.staff)

    def test_both_actions_agree(self):
        instance_url = factories.InstanceFactory.get_url(
            self.fixture.instance, action="update_allowed_address_pairs"
        )
        port_url = factories.PortFactory.get_url(self.port, "set_allowed_address_pairs")
        for pairs in self.CASES:
            via_instance = self.client.post(
                instance_url,
                {
                    "subnet": factories.SubNetFactory.get_url(self.subnet),
                    "allowed_address_pairs": pairs,
                },
                format="json",
            ).status_code
            via_port = self.client.post(
                port_url, {"allowed_address_pairs": pairs}, format="json"
            ).status_code
            self.assertEqual(
                via_instance < 300,
                via_port < 300,
                f"{pairs}: instance action {via_instance}, port action {via_port}",
            )
