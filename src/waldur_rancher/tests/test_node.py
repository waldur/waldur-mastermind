from unittest import mock
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.openstack_tenant.tests import factories as openstack_tenant_factories

from . import factories, fixtures, test_cluster
from .. import models, tasks


class NodeGetTest(test.APITransactionTestCase):
    def setUp(self):
        super(NodeGetTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.fixture_2 = fixtures.RancherFixture()
        self.url = factories.NodeFactory.get_list_url()

    def test_get_node_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 2)

    def test_user_cannot_get_strangers_nodes(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 1)


class NodeCreateTest(test_cluster.BaseClusterCreateTest):
    def setUp(self):
        super(NodeCreateTest, self).setUp()
        self.node_url = factories.NodeFactory.get_list_url()

    @mock.patch('waldur_rancher.views.executors')
    def test_create_node_if_cluster_has_been_created(self, mock_executors):
        self.client.force_authenticate(self.fixture.owner)
        response = self._create_request_(name='name')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        cluster = models.Cluster.objects.get(name='name')
        self.assertTrue(mock_executors.ClusterCreateExecutor.execute.called)
        create_node_task = tasks.CreateNodeTask()
        create_node_task.execute(
            mock_executors.ClusterCreateExecutor.execute.mock_calls[0][1][0].node_set.first(),
            user_id=mock_executors.ClusterCreateExecutor.execute.mock_calls[0][2]['user'].id,
        )
        self.assertTrue(cluster.node_set.filter(cluster=cluster).exists())
        node = cluster.node_set.first()
        self.assertTrue(node.controlplane_role)
        self.assertTrue(node.etcd_role)
        self.assertTrue(node.worker_role)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_staff_can_create_node(self, mock_tasks):
        self.client.force_authenticate(self.fixture.staff)
        cluster = self.fixture.cluster
        response = self.client.post(self.node_url,
                                    {'cluster': factories.ClusterFactory.get_url(cluster),
                                     'subnet': openstack_tenant_factories.SubNetFactory.get_url(self.subnet),
                                     'system_volume_size': 1024,
                                     'memory': 1,
                                     'cpu': 1,
                                     'roles': ['controlplane', 'etcd', 'worker'],
                                     })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(mock_tasks.CreateNodeTask.return_value.si.call_count, 1)

    def test_others_cannot_create_node(self):
        self.client.force_authenticate(self.fixture.owner)
        cluster = self.fixture.cluster
        response = self.client.post(self.node_url,
                                    {'cluster': factories.ClusterFactory.get_url(cluster),
                                     'subnet': openstack_tenant_factories.SubNetFactory.get_url(self.subnet),
                                     'system_volume_size': 1024,
                                     'memory': 1,
                                     'cpu': 1,
                                     'roles': ['controlplane', 'etcd', 'worker'],
                                     })
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_cannot_create_node_if_cpu_has_not_been_specified(self):
        self.client.force_authenticate(self.fixture.staff)
        cluster = self.fixture.cluster
        response = self.client.post(self.node_url,
                                    {'cluster': factories.ClusterFactory.get_url(cluster),
                                     'subnet': openstack_tenant_factories.SubNetFactory.get_url(self.subnet),
                                     'system_volume_size': 1024,
                                     'memory': 1,
                                     'roles': ['controlplane', 'etcd', 'worker'],
                                     })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_create_node_if_flavor_has_been_specified(self, mock_tasks):
        self.client.force_authenticate(self.fixture.staff)
        cluster = self.fixture.cluster
        response = self.client.post(self.node_url,
                                    {'cluster': factories.ClusterFactory.get_url(cluster),
                                     'subnet': openstack_tenant_factories.SubNetFactory.get_url(self.subnet),
                                     'system_volume_size': 1024,
                                     'flavor': openstack_tenant_factories.FlavorFactory.get_url(self.flavor),
                                     'roles': ['controlplane', 'etcd', 'worker'],
                                     })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(mock_tasks.CreateNodeTask.return_value.si.call_count, 1)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_do_not_create_node_if_flavor_does_not_meet_requirements(self, mock_tasks):
        self.flavor.cores = 1
        self.flavor.ram = 1024
        self.flavor.save()
        self.client.force_authenticate(self.fixture.staff)
        cluster = self.fixture.cluster
        response = self.client.post(self.node_url,
                                    {'cluster': factories.ClusterFactory.get_url(cluster),
                                     'subnet': openstack_tenant_factories.SubNetFactory.get_url(self.subnet),
                                     'system_volume_size': 1024,
                                     'flavor': openstack_tenant_factories.FlavorFactory.get_url(self.flavor),
                                     'roles': ['controlplane', 'etcd', 'worker'],
                                     })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_linking_rancher_nodes_with_openStack_instance(self):
        self.client.force_authenticate(self.fixture.staff)
        node = factories.NodeFactory()
        url = factories.NodeFactory.get_url(node, 'link_openstack')
        instance = openstack_tenant_factories.InstanceFactory()
        instance_url = openstack_tenant_factories.InstanceFactory.get_url(instance)
        response = self.client.post(url, {'instance': instance_url})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        node.refresh_from_db()
        self.assertEqual(node.instance, instance)

    def _create_new_test_instance(self, customer):
        settings = structure_factories.ServiceSettingsFactory(customer=customer)
        service = structure_factories.TestServiceFactory(customer=customer, settings=settings)
        spl = structure_factories.TestServiceProjectLinkFactory(service=service, project=self.fixture.project)
        return structure_factories.TestNewInstanceFactory(service_project_link=spl)


class NodeDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        super(NodeDeleteTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.cluster_name = self.fixture.cluster.name
        self.url = factories.NodeFactory.get_url(self.fixture.node)

    def test_delete_node(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
