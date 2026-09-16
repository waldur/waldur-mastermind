from unittest import mock

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.logging.enums import EventType
from waldur_openstack import models

from . import factories, fixtures


class SetAllowedAddressPairsTest(test.APITestCase):
    """``POST /api/openstack-ports/{uuid}/set_allowed_address_pairs/``."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.port = factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            state=CoreStates.OK,
            backend_id="port-1",
        )
        self.url = factories.PortFactory.get_url(self.port, "set_allowed_address_pairs")

        self.backend_patcher = mock.patch(
            "waldur_openstack.backend.OpenStackBackend.set_port_allowed_address_pairs"
        )
        self.backend_mock = self.backend_patcher.start()

    def tearDown(self):
        self.backend_patcher.stop()
        super().tearDown()

    def test_staff_can_set_pairs(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "allowed_address_pairs": [
                {"ip_address": "10.0.0.10", "mac_address": "aa:bb:cc:dd:ee:ff"},
                {"ip_address": "192.168.42.0/24"},
            ]
        }
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.backend_mock.assert_called_once()
        self.port.refresh_from_db()
        ips = [p["ip_address"] for p in self.port.allowed_address_pairs]
        # validate_private_cidr normalises single-host inputs to /32.
        self.assertEqual(ips, ["10.0.0.10/32", "192.168.42.0/24"])
        # MAC normalised to lowercase by the validator.
        self.assertEqual(
            self.port.allowed_address_pairs[0]["mac_address"], "aa:bb:cc:dd:ee:ff"
        )

    def test_public_ip_rejected(self):
        """0.0.0.0/0, public IPs, link-local, metadata service must be rejected.

        Without this check a project admin could grant their port permission
        to spoof the upstream gateway, the metadata service, or arbitrary
        public IPs — the textbook allowed-address-pairs escalation.
        """
        self.client.force_authenticate(self.fixture.staff)
        for value in (
            "0.0.0.0/0",
            "8.8.8.8",
            "169.254.169.254",
            "224.0.0.1",
        ):
            response = self.client.post(
                self.url,
                {"allowed_address_pairs": [{"ip_address": value}]},
                format="json",
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_400_BAD_REQUEST,
                f"{value!r} should be rejected as non-RFC1918",
            )
            self.backend_mock.assert_not_called()

    def test_empty_list_clears_pairs(self):
        self.port.allowed_address_pairs = [
            {"ip_address": "10.0.0.10", "mac_address": "aa:bb:cc:dd:ee:ff"}
        ]
        self.port.save(update_fields=["allowed_address_pairs"])

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, {"allowed_address_pairs": []}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.port.refresh_from_db()
        self.assertEqual(self.port.allowed_address_pairs, [])

    def test_invalid_ip_rejected(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url,
            {"allowed_address_pairs": [{"ip_address": "not-an-ip"}]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.backend_mock.assert_not_called()

    def test_invalid_mac_rejected(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url,
            {
                "allowed_address_pairs": [
                    {"ip_address": "10.0.0.10", "mac_address": "BAD-MAC"}
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_pairs_rejected(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url,
            {
                "allowed_address_pairs": [
                    {"ip_address": "10.0.0.10"},
                    {"ip_address": "10.0.0.10"},
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_member_cannot_set_pairs(self):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(
            self.url,
            {"allowed_address_pairs": [{"ip_address": "10.0.0.10"}]},
            format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.backend_mock.assert_not_called()

    def test_admin_can_set_pairs(self):
        """Project admin holds CAN_MANAGE_OPENSTACK_INSTANCE in the fixture."""
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.post(
            self.url,
            {"allowed_address_pairs": [{"ip_address": "10.0.0.10"}]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.backend_mock.assert_called_once()

    def test_port_must_be_ok_state(self):
        models.Port.objects.filter(pk=self.port.pk).update(state=CoreStates.ERRED)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url,
            {"allowed_address_pairs": [{"ip_address": "10.0.0.10"}]},
            format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_409_CONFLICT),
        )

    def test_event_emitted_on_change(self):
        self.client.force_authenticate(self.fixture.staff)
        with mock.patch("waldur_openstack.audit.event_logger.emit") as emit_mock:
            response = self.client.post(
                self.url,
                {"allowed_address_pairs": [{"ip_address": "10.0.0.10"}]},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        event_types = [c.kwargs.get("event_type") for c in emit_mock.call_args_list]
        self.assertIn(
            EventType.OPENSTACK_PORT_ALLOWED_ADDRESS_PAIRS_CHANGED, event_types
        )


class Ipv6SetAllowedAddressPairsTest(test.APITestCase):
    """An IPv6 pair is accepted within unique local addresses or within one of
    the tenant's own subnets, and never over an address another host relies on."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = factories.SubNetFactory(
            network=self.fixture.network,
            tenant=self.fixture.tenant,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            cidr="2001:db8:1::/64",
            gateway_ip="2001:db8:1::1",
            ip_version=6,
            backend_id="v6-subnet",
            state=CoreStates.OK,
        )
        self.port = factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            network=self.fixture.network,
            fixed_ips=[{"subnet_id": "v6-subnet", "ip_address": "2001:db8:1::10"}],
            state=CoreStates.OK,
        )
        self.url = factories.PortFactory.get_url(self.port, "set_allowed_address_pairs")
        backend = mock.patch(
            "waldur_openstack.backend.OpenStackBackend.set_port_allowed_address_pairs"
        )
        self.backend = backend.start()
        self.addCleanup(backend.stop)
        self.client.force_authenticate(self.fixture.staff)

    def _post(self, value):
        return self.client.post(
            self.url,
            {"allowed_address_pairs": [{"ip_address": value}]},
            format="json",
        )

    def test_unique_local_and_own_subnet_pairs_are_accepted(self):
        for value, stored in (
            ("fd00::10", "fd00::10/128"),
            ("fd12:3456:789a::/64", "fd12:3456:789a::/64"),
            ("2001:db8:1::50", "2001:db8:1::50/128"),
            ("2001:db8:1::100/126", "2001:db8:1::100/126"),
        ):
            response = self._post(value)

            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.port.refresh_from_db()
            self.assertEqual(
                self.port.allowed_address_pairs, [{"ip_address": stored}], value
            )

    def test_ranges_outside_the_accepted_scope_are_rejected(self):
        for value in (
            "::/0",
            "::/1",
            "8000::/1",
            "2000::/3",
            "2001:db8:2::10",
            "fe80::/64",
            "fe80::1",
            "ff02::1",
            "::1",
            "::",
            "::ffff:10.0.0.10",
            "::ffff:0:0/96",
        ):
            response = self._post(value)

            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, f"{value!r} accepted"
            )
        self.backend.assert_not_called()

    def test_a_pair_covering_the_gateway_is_rejected(self):
        for value in ("2001:db8:1::1", "2001:db8:1::/64", "2001:db8:1::/120"):
            response = self._post(value)

            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, f"{value!r} accepted"
            )
        self.backend.assert_not_called()

    def test_an_invalid_address_gets_a_family_neutral_message(self):
        response = self._post("not-an-ip")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("IPv4 address.", str(response.data))
        self.assertIn("IPv6", str(response.data))


class RouterAddressAllowedAddressPairsTest(test.APITestCase):
    """A pair must not cover an address a router answers on: the gateway, a
    further router interface on the subnet, or the next hop of a host route.
    Any of them would let the port intercept traffic meant for that router."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.v4 = self.fixture.subnet
        self.v4.cidr = "10.0.0.0/24"
        self.v4.gateway_ip = "10.0.0.1"
        self.v4.host_routes = [
            {"destination": "172.16.0.0/16", "nexthop": "10.0.0.254"}
        ]
        self.v4.save()
        self.v6 = factories.SubNetFactory(
            network=self.fixture.network,
            tenant=self.fixture.tenant,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            cidr="2001:db8:1::/64",
            gateway_ip="2001:db8:1::1",
            ip_version=6,
            host_routes=[
                {"destination": "2001:db8:9::/48", "nexthop": "2001:db8:1::fe"}
            ],
            backend_id="v6-subnet",
            state=CoreStates.OK,
        )
        # A second router attached with add_router_interface takes the first
        # free address rather than the gateway.
        factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            network=self.fixture.network,
            device_owner="network:router_interface",
            fixed_ips=[
                {"subnet_id": self.v4.backend_id, "ip_address": "10.0.0.2"},
                {"subnet_id": "v6-subnet", "ip_address": "2001:db8:1::2"},
            ],
            state=CoreStates.OK,
        )
        self.port = factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            network=self.fixture.network,
            fixed_ips=[
                {"subnet_id": self.v4.backend_id, "ip_address": "10.0.0.10"},
                {"subnet_id": "v6-subnet", "ip_address": "2001:db8:1::10"},
            ],
            state=CoreStates.OK,
        )
        self.url = factories.PortFactory.get_url(self.port, "set_allowed_address_pairs")
        backend = mock.patch(
            "waldur_openstack.backend.OpenStackBackend.set_port_allowed_address_pairs"
        )
        self.backend = backend.start()
        self.addCleanup(backend.stop)
        self.client.force_authenticate(self.fixture.staff)

    def _post(self, value):
        return self.client.post(
            self.url,
            {"allowed_address_pairs": [{"ip_address": value}]},
            format="json",
        )

    def test_a_pair_covering_a_router_address_is_rejected(self):
        for value in (
            "10.0.0.2",
            "10.0.0.0/30",
            "10.0.0.254",
            "10.0.0.252/30",
            "2001:db8:1::2",
            "2001:db8:1::fe",
            "2001:db8:1::f0/124",
        ):
            response = self._post(value)

            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, f"{value!r} accepted"
            )
            self.assertIn("allowed_address_pairs", response.data)
        self.backend.assert_not_called()

    def test_a_pair_next_to_the_router_addresses_is_accepted(self):
        for value in ("10.0.0.50", "10.0.0.128/26", "2001:db8:1::50"):
            response = self._post(value)

            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class TenantSubnetScopeAllowedAddressPairsTest(test.APITestCase):
    """A pair is measured against every subnet of the tenant.

    Covering one of them would let the port answer for every instance in it,
    even where no gateway is set. A range that overlaps none of them is a
    routed network behind the instance -- Magnum sets the whole pod CIDR as a
    pair -- and an address in a neighbouring subnet is a failover address.
    """

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = self.fixture.subnet
        self.subnet.cidr = "10.0.0.0/24"
        self.subnet.gateway_ip = None
        self.subnet.backend_id = "v4-subnet"
        self.subnet.save()
        self.neighbour = factories.SubNetFactory(
            network=self.fixture.network,
            tenant=self.fixture.tenant,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            cidr="2001:db8:7::/64",
            gateway_ip="2001:db8:7::1",
            ip_version=6,
            backend_id="v6-neighbour",
            state=CoreStates.OK,
        )
        self.port = factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            network=self.fixture.network,
            fixed_ips=[{"subnet_id": "v4-subnet", "ip_address": "10.0.0.10"}],
            state=CoreStates.OK,
        )
        self.url = factories.PortFactory.get_url(self.port, "set_allowed_address_pairs")
        backend = mock.patch(
            "waldur_openstack.backend.OpenStackBackend.set_port_allowed_address_pairs"
        )
        self.backend = backend.start()
        self.addCleanup(backend.stop)
        self.client.force_authenticate(self.fixture.staff)

    def _post(self, value):
        return self.client.post(
            self.url,
            {"allowed_address_pairs": [{"ip_address": value}]},
            format="json",
        )

    def test_a_pair_covering_a_tenant_subnet_is_rejected(self):
        # The subnet has no gateway, so only this rule stands between the port
        # and answering for every instance on it.
        for value in ("10.0.0.0/24", "10.0.0.0/16", "2001:db8:7::/64"):
            response = self._post(value)

            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, f"{value!r} accepted"
            )
            self.assertIn("covers the subnet", str(response.data), value)
        self.backend.assert_not_called()

    def test_a_routed_network_that_overlaps_no_tenant_subnet_is_accepted(self):
        # A container network behind the instance, the shape Magnum creates.
        for value, stored in (
            ("10.100.0.0/16", "10.100.0.0/16"),
            ("fd12:3456:789a::/64", "fd12:3456:789a::/64"),
        ):
            response = self._post(value)

            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.port.refresh_from_db()
            self.assertEqual(
                self.port.allowed_address_pairs, [{"ip_address": stored}], value
            )

    def test_an_address_in_a_neighbouring_subnet_is_accepted(self):
        # A failover address kept on another subnet of the same tenant.
        response = self._post("2001:db8:7::50")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.port.refresh_from_db()
        self.assertEqual(
            self.port.allowed_address_pairs, [{"ip_address": "2001:db8:7::50/128"}]
        )

    def test_an_address_outside_every_tenant_subnet_is_still_rejected(self):
        response = self._post("2001:db8:9::50")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.backend.assert_not_called()
