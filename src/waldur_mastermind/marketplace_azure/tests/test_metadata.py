from rest_framework import test

from waldur_azure.tests import fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class NetworkMetadataTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.AzureFixture()
        self.vm = self.fixture.virtual_machine
        self.nic = self.fixture.network_interface
        self.resource = marketplace_factories.ResourceFactory(scope=self.vm)

    def get_external_ips(self):
        return self.resource.backend_metadata['external_ips']

    def get_internal_ips(self):
        return self.resource.backend_metadata['internal_ips']

    def test_floating_ip_address_is_synchronized_when_public_ip_is_assigned(self):
        public_ip = self.fixture.public_ip
        self.nic.public_ip = public_ip
        self.nic.save()

        self.resource.refresh_from_db()
        self.assertEqual(self.get_external_ips(), [public_ip.ip_address])

    def test_floating_ip_address_is_synchronized_when_public_ip_is_updated(self):
        public_ip = self.fixture.public_ip
        self.nic.public_ip = public_ip
        self.nic.save()
        self.nic.public_ip = None
        self.nic.save()

        self.resource.refresh_from_db()
        self.assertEqual(self.get_external_ips(), [])

    def test_internal_ip_address_is_synchronized(self):
        self.nic.ip_address = '192.168.0.101'
        self.nic.save()

        self.resource.refresh_from_db()
        self.assertEqual(self.get_internal_ips(), ['192.168.0.101'])
