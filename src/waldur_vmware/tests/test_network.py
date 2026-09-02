from rest_framework import status, test

from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class NetworkGetTest(test.APITestCase):
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
        self.assertEqual(len(response.data), 4)

    def test_filter_network_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
