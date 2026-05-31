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
