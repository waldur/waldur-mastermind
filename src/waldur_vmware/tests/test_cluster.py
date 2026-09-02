from rest_framework import status, test

from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class ClusterGetTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = ProjectFixture()
        self.fixture_2 = ProjectFixture()
        cluster_1 = factories.ClusterFactory()
        cluster_2 = factories.ClusterFactory()
        cluster_3 = factories.ClusterFactory()
        cluster_4 = factories.ClusterFactory()

        factories.CustomerClusterFactory(
            cluster=cluster_1,
            customer=self.fixture.customer,
        )
        factories.CustomerClusterFactory(
            cluster=cluster_2,
            customer=self.fixture.customer,
        )
        factories.CustomerClusterFactory(
            cluster=cluster_3,
            customer=self.fixture_2.customer,
        )
        factories.CustomerClusterFactory(
            cluster=cluster_4,
            customer=self.fixture_2.customer,
        )
        self.url = factories.ClusterFactory.get_list_url()

    def test_get_cluster_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 4)

    def test_filter_cluster_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
