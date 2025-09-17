from unittest import mock

from django.test import override_settings
from rest_framework import status, test

from waldur_mastermind.marketplace.enums import RANCHER_OFFERING
from waldur_mastermind.marketplace.tests.factories import OfferingFactory
from waldur_mastermind.marketplace_rancher import (
    const,
)
from waldur_openstack.tests import factories as os_factories
from waldur_rancher import models
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
    "version": {"gitVersion": "v1.31.7+rke2r1"},
    "capacity": {"cpu": "24", "memory": "49125240Ki", "pods": "330"},
    "requested": {"cpu": "1450m", "memory": "884Mi", "pods": "13"},
}

MOCK_NODES = [
    {"id": "new_cluster_id:m-dcd22bd33bfc", "requestedHostname": "k8s-node-00"}
]


class BaseClusterImportTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.offering = OfferingFactory(
            scope=self.fixture.settings,
            type=RANCHER_OFFERING,
            shared=False,
            customer=self.fixture.customer,
        )
        self.client_patcher = mock.patch("waldur_rancher.client.RancherClient")
        self.mocked_client = self.client_patcher.start()()
        self.mocked_client.login.return_value = None

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()


class ClusterImportableResourcesTest(BaseClusterImportTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.fixture.staff)
        self.url = OfferingFactory.get_url(self.offering, "importable_resources")

    def test_importable_clusters_are_returned(self):
        self.mocked_client.list_clusters.return_value = [MOCK_CLUSTER]
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            [
                {
                    "type": "Rancher.Cluster",
                    "name": "customer-app",
                    "backend_id": "new_cluster_id",
                    "extra": [
                        {"name": "Description", "value": ""},
                        {"name": "Number of nodes", "value": 1},
                        {"name": "Created at", "value": "2019-09-11T12:37:57Z"},
                    ],
                }
            ],
        )


class ClusterImportResourceTest(BaseClusterImportTest):
    def setUp(self):
        super().setUp()
        self.url = OfferingFactory.get_url(self.offering, "import_resource")
        self.client.force_authenticate(self.fixture.staff)
        self.mocked_client.get_cluster.return_value = MOCK_CLUSTER

    def test_backend_cluster_is_imported(self):
        backend_id = "backend_id"

        payload = {
            "backend_id": backend_id,
            "project": self.fixture.project.uuid,
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
            "backend_id": cluster.backend_id,
            "project": self.fixture.project.uuid,
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ManagedClusterImportResourceTest(BaseClusterImportTest):
    def setUp(self):
        super().setUp()
        self.tenant = self.fixture.tenant

        self.offering.plugin_options["deployment_mode"] = const.DEPLOYMENT_MODE_MANAGED
        self.offering.save()

        self.url = OfferingFactory.get_url(self.offering, "import_resource")
        self.client.force_authenticate(self.fixture.staff)
        self.mocked_client.get_cluster.return_value = MOCK_CLUSTER
        self.mocked_client.get_cluster_nodes.return_value = MOCK_NODES

    @override_settings(task_always_eager=True)
    def test_managed_cluster_is_imported(self):
        backend_id = MOCK_CLUSTER["id"]

        payload = {
            "backend_id": backend_id,
            "project": self.fixture.project.uuid,
            "additional_details": {"tenant_uuid": self.tenant.uuid.hex},
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Cluster.objects.filter(backend_id=backend_id).exists())

    @override_settings(task_always_eager=True)
    def test_managed_cluster_is_imported_with_tenant(self):
        backend_id = MOCK_CLUSTER["id"]
        node_backend_id = MOCK_NODES[0]["id"]

        payload = {
            "backend_id": backend_id,
            "project": self.fixture.project.uuid,
            "additional_details": {"tenant_uuid": self.tenant.uuid.hex},
        }

        lb_instance = self.fixture.instance
        lb_instance.name = f"{const.OS_LB_PREFIX}-{lb_instance.name}"
        lb_instance.save()

        node_instance = os_factories.InstanceFactory(
            service_settings=self.fixture.settings,
            tenant=self.tenant,
            project=self.fixture.project,
            state=lb_instance.state,
            name="k8s-node-00",
        )

        port = os_factories.PortFactory(
            tenant=self.tenant,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            instance=lb_instance,
        )
        floating_ip = os_factories.FloatingIPFactory(
            tenant=self.tenant,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            port=port,
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        cluster = models.Cluster.objects.get(backend_id=backend_id)
        self.assertEqual(cluster.tenant, self.tenant)

        self.assertTrue(models.ClusterPublicIP.objects.filter(cluster=cluster).exists())
        public_ip = models.ClusterPublicIP.objects.get(cluster=cluster)
        self.assertEqual(public_ip.floating_ip, floating_ip)

        self.assertTrue(cluster.node_set.filter(backend_id=node_backend_id).exists())
        cluster_node = cluster.node_set.get(backend_id=node_backend_id)
        self.assertEqual(cluster_node.instance, node_instance)
