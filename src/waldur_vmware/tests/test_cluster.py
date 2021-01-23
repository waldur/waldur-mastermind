from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_vmware import backend, models

from . import factories


class ClusterGetTest(test.APITransactionTestCase):
    def setUp(self):
        super(ClusterGetTest, self).setUp()
        self.fixture = ProjectFixture()
        self.fixture_2 = ProjectFixture()
        cluster_1 = factories.ClusterFactory()
        cluster_2 = factories.ClusterFactory()
        cluster_3 = factories.ClusterFactory()
        cluster_4 = factories.ClusterFactory()

        factories.CustomerClusterFactory(
            cluster=cluster_1, customer=self.fixture.customer,
        )
        factories.CustomerClusterFactory(
            cluster=cluster_2, customer=self.fixture.customer,
        )
        factories.CustomerClusterFactory(
            cluster=cluster_3, customer=self.fixture_2.customer,
        )
        factories.CustomerClusterFactory(
            cluster=cluster_4, customer=self.fixture_2.customer,
        )
        self.url = factories.ClusterFactory.get_list_url()

    def test_get_cluster_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 4)

    def test_filter_cluster_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {'customer_uuid': self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 2)


class ClusterPullTest(test.APITransactionTestCase):
    def setUp(self):
        super(ClusterPullTest, self).setUp()
        self.settings = factories.VMwareServiceSettingsFactory()
        self.backend = backend.VMwareBackend(self.settings)
        self.patcher = mock.patch('waldur_vmware.backend.VMwareClient')
        self.mock_client = self.patcher.start()

    def tearDown(self):
        super(ClusterPullTest, self).tearDown()
        mock.patch.stopall()

    def test_delete_old_clusters(self):
        factories.ClusterFactory(settings=self.settings)
        factories.ClusterFactory(settings=self.settings)
        self.backend.pull_clusters()
        self.assertEqual(models.Cluster.objects.count(), 0)

    def test_add_new_clusters(self):
        client = mock.MagicMock()
        self.mock_client.return_value = client
        client.list_clusters.return_value = self._generate_clusters()

        self.backend.pull_clusters()
        self.assertEqual(models.Cluster.objects.count(), 1)

    def _generate_clusters(self, count=1):
        clusters = []
        for i in range(count):
            backend_cluster = {
                'name': 'cluster_%s' % i,
                'cluster': 'cluster_%s' % i,
            }
            clusters.append(backend_cluster)

        return clusters
