from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_vmware import backend, models

from . import factories


class NetworkGetTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = ProjectFixture()
        self.fixture_2 = ProjectFixture()
        network_1 = factories.NetworkFactory()
        network_2 = factories.NetworkFactory()
        network_3 = factories.NetworkFactory()
        network_4 = factories.NetworkFactory()

        factories.CustomerNetworkFactory(
            network=network_1,
            customer=self.fixture.customer,
        )
        factories.CustomerNetworkFactory(
            network=network_2,
            customer=self.fixture.customer,
        )
        factories.CustomerNetworkFactory(
            network=network_3,
            customer=self.fixture_2.customer,
        )
        factories.CustomerNetworkFactory(
            network=network_4,
            customer=self.fixture_2.customer,
        )
        self.url = factories.NetworkFactory.get_list_url()

    def test_get_network_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 4)

    def test_filter_network_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {'customer_uuid': self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 2)


class NetworkPullTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.settings = factories.VMwareServiceSettingsFactory()
        self.backend = backend.VMwareBackend(self.settings)
        self.patcher = mock.patch('waldur_vmware.backend.VMwareClient')
        self.mock_client = self.patcher.start()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_delete_old_networks(self):
        factories.NetworkFactory(settings=self.settings)
        factories.NetworkFactory(settings=self.settings)
        self.backend.pull_networks()
        self.assertEqual(models.Network.objects.count(), 0)

    def test_add_new_networks(self):
        client = mock.MagicMock()
        self.mock_client.return_value = client
        client.list_networks.return_value = self._generate_networks()

        self.backend.pull_networks()
        self.assertEqual(models.Network.objects.count(), 1)

    def _generate_networks(self, count=1):
        networks = []
        for i in range(count):
            backend_network = {
                'name': 'network_%s' % i,
                'network': 'network_%s' % i,
                'type': 'STANDARD_PORTGROUP',
            }
            networks.append(backend_network)

        return networks
