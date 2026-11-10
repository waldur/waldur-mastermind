"""Names and descriptions show in OpenStack event messages as written."""

from unittest import mock

from rest_framework import test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_openstack.backend import OpenStackBackend

from . import factories, fixtures

TEXT = "AA{{BB}}CC {nosuchkey} {"


def last_message(event_type):
    return Event.objects.filter(event_type=event_type).latest("created").message


class BackendEventMessageTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.neutron = mock.patch("neutronclient.v2_0.client.Client").start()()
        mock.patch("cinderclient.v3.client.Client").start()
        mock.patch("novaclient.v2.client.Client").start()
        mock.patch("glanceclient.v2.client.Client").start()
        self.backend = OpenStackBackend(self.tenant.service_settings)
        fixtures.mock_session()

    def tearDown(self):
        mock.patch.stopall()
        super().tearDown()

    def port(self, **kwargs):
        return factories.PortFactory(
            tenant=self.tenant,
            network=self.fixture.network,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            name=TEXT,
            **kwargs,
        )

    def test_network_update(self):
        network = self.fixture.network
        network.name = TEXT
        network.description = TEXT
        network.save()
        self.neutron.update_network.return_value = {"network": {}}

        self.backend.update_network(network)

        self.assertEqual(
            last_message(EventType.OPENSTACK_NETWORK_UPDATED),
            f"Network name {TEXT} and description {TEXT} have been updated.",
        )

    def test_floating_ip_description(self):
        floating_ip = self.fixture.floating_ip
        self.neutron.update_floatingip.return_value = {
            "floatingip": {
                "id": floating_ip.backend_id,
                "description": TEXT,
                "status": "ACTIVE",
                "floating_ip_address": floating_ip.address,
                "floating_network_id": "external-network-id",
                "fixed_ip_address": None,
                "port_id": None,
            }
        }

        self.backend.update_floating_ip_description(floating_ip, TEXT)

        self.assertTrue(
            last_message(EventType.OPENSTACK_FLOATING_IP_DESCRIPTION_UPDATED).endswith(
                f"has been changed to [{TEXT}]."
            )
        )

    def test_port_create(self):
        port = self.port()
        self.neutron.create_port.return_value = {
            "port": {
                "id": "backend-port",
                "status": "ACTIVE",
                "mac_address": "fa:16:3e:ab:cd:ef",
                "fixed_ips": [],
                "admin_state_up": True,
                "port_security_enabled": True,
                "device_owner": "",
            }
        }

        self.backend.create_port(port)

        self.assertIn(
            f"Port [{port}] has been created",
            last_message(EventType.OPENSTACK_PORT_CREATED),
        )

    def test_port_update(self):
        port = self.port(backend_id="port-1")
        self.neutron.update_port.return_value = {"port": {}}

        self.backend.update_port_name_and_description(port)

        self.assertIn(
            f"Port [{port}] name and description",
            last_message(EventType.OPENSTACK_PORT_UPDATED),
        )

    def test_port_delete(self):
        port = self.port(backend_id="port-2")

        self.backend.delete_port(port)

        self.assertIn(
            f"Port [{port}] has been deleted",
            last_message(EventType.OPENSTACK_PORT_DELETED),
        )
