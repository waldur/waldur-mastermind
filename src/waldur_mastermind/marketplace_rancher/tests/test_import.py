from unittest import mock

from rest_framework import status, test

from waldur_mastermind.marketplace.tests.factories import OfferingFactory
from waldur_mastermind.marketplace_rancher import PLUGIN_NAME
from waldur_rancher.tests import factories, fixtures

MOCK_CLUSTER = {
    "id": "new_cluster_id",
    "name": "customer-app",
    "description": "",
    "created": "2019-09-11T12:37:57Z",
    "state": "active",
    "appliedSpec": {
        "rancherKubernetesEngineConfig": {
            "nodes": [
                {
                    "address": "10.0.2.15",
                    "nodeId": "new_cluster_id:m-dcd22bd33bfc",
                    "role": ["etcd", "controlplane", "worker"],
                }
            ]
        }
    },
}


class BaseClusterImportTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.offering = OfferingFactory(
            scope=self.fixture.settings,
            type=PLUGIN_NAME,
            shared=False,
            customer=self.fixture.customer,
        )
        self.client_patcher = mock.patch('waldur_rancher.client.RancherClient')
        self.mocked_client = self.client_patcher.start()()
        self.mocked_client.login.return_value = None

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()


class ClusterImportableResourcesTest(BaseClusterImportTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.fixture.owner)
        self.url = OfferingFactory.get_url(self.offering, 'importable_resources')

    def test_importable_clusters_are_returned(self):
        self.mocked_client.list_clusters.return_value = [MOCK_CLUSTER]
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            [
                {
                    'type': 'Rancher.Cluster',
                    'name': 'customer-app',
                    'backend_id': 'new_cluster_id',
                    'extra': [
                        {'name': 'Description', 'value': ''},
                        {'name': 'Number of nodes', 'value': 1},
                        {'name': 'Created at', 'value': '2019-09-11T12:37:57Z'},
                    ],
                }
            ],
        )


class ClusterImportResourceTest(BaseClusterImportTest):
    def setUp(self):
        super().setUp()
        self.url = OfferingFactory.get_url(self.offering, 'import_resource')
        self.client.force_authenticate(self.fixture.owner)
        self.mocked_client.get_cluster.return_value = MOCK_CLUSTER

    def test_backend_cluster_is_imported(self):
        backend_id = 'backend_id'

        payload = {
            'backend_id': backend_id,
            'project': self.fixture.project.uuid,
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_backend_cluster_cannot_be_imported_if_it_is_registered_in_waldur(self):
        cluster = factories.ClusterFactory(
            settings=self.fixture.settings,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
        )

        payload = {
            'backend_id': cluster.backend_id,
            'project': self.fixture.project.uuid,
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
