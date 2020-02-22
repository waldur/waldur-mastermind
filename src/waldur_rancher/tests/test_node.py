import json
from unittest import mock

import pkg_resources
from ddt import data, ddt
from rest_framework import status, test
from django.contrib.contenttypes.models import ContentType

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


@ddt
class NodeCreateTest(test_cluster.BaseClusterCreateTest):
    def setUp(self):
        super(NodeCreateTest, self).setUp()
        self.node_url = factories.NodeFactory.get_list_url()
        self.payload = {
            'cluster': factories.ClusterFactory.get_url(self.fixture.cluster),
            'subnet': openstack_tenant_factories.SubNetFactory.get_url(self.subnet),
            'system_volume_size': 1024,
            'memory': 1,
            'cpu': 1,
            'roles': ['controlplane', 'etcd', 'worker'],
        }

    @mock.patch('waldur_rancher.views.executors')
    def test_create_node_if_cluster_has_been_created(self, mock_executors):
        cluster = self._create_cluster_(name='name')
        self.assertTrue(mock_executors.NodeCreateExecutor.execute.called)
        node = cluster.node_set.first()
        self.assertTrue(node.controlplane_role)
        self.assertTrue(node.etcd_role)
        self.assertTrue(node.worker_role)

    def create_node(self, user):
        self.client.force_authenticate(user)
        return self.client.post(self.node_url, self.payload)

    @data('staff', 'owner')
    @mock.patch('waldur_rancher.executors.tasks')
    def test_authorized_user_can_create_node(self, user, mock_tasks):
        response = self.create_node(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(mock_tasks.CreateNodeTask.return_value.si.call_count, 1)

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_cannot_create_node(self, user):
        response = self.create_node(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_use_data_volumes(self, mock_tasks):
        volume_type = openstack_tenant_factories.VolumeTypeFactory(
            settings=self.fixture.tenant_spl.service.settings
        )
        self.payload = {
            'cluster': factories.ClusterFactory.get_url(self.fixture.cluster),
            'subnet': openstack_tenant_factories.SubNetFactory.get_url(self.subnet),
            'system_volume_size': 1024,
            'memory': 1,
            'cpu': 1,
            'roles': ['controlplane', 'etcd', 'worker'],
            'data_volumes': [
                {
                    'size': 12 * 1024,
                    'volume_type': openstack_tenant_factories.VolumeTypeFactory.get_url(volume_type),
                    'mount_point': '/var/lib/etcd',
                }
            ]
        }
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.fixture.cluster.node_set.count(), 2)
        node = self.fixture.cluster.node_set.exclude(name='').get()
        self.assertEqual(len(node.initial_data['data_volumes']), 1)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_poll_node_after_it_has_been_created(self, mock_tasks):
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(mock_tasks.PollRuntimeStateNodeTask.return_value.si.call_count, 1)

    @mock.patch('waldur_rancher.backend.RancherBackend.client')
    @mock.patch('waldur_rancher.tasks.PollRuntimeStateNodeTask.retry')
    def test_pulling_if_node_has_been_created(self, mock_retry, mock_client):
        backend_cluster_nodes = json.loads(
            pkg_resources.resource_stream(__name__, 'backend_cluster_nodes.json').read().decode())
        backend_node = backend_cluster_nodes[0]
        self.fixture.node.name = backend_node['nodeName']
        self.fixture.node.save()
        mock_client.get_cluster_nodes.return_value = backend_cluster_nodes
        mock_client.get_node.return_value = backend_node

        tasks.PollRuntimeStateNodeTask().execute(self.fixture.node)
        self.assertEqual(mock_retry.call_count, 0)
        self.fixture.node.refresh_from_db()
        self.assertEqual(self.fixture.node.runtime_state, models.Node.RuntimeStates.ACTIVE)
        self.assertEqual(self.fixture.node.backend_id, backend_node['id'])

    @mock.patch('waldur_rancher.backend.RancherBackend.client')
    @mock.patch('waldur_rancher.tasks.PollRuntimeStateNodeTask.retry')
    def test_pulling_if_node_has_not_been_created(self, mock_retry, mock_client):
        backend_cluster_nodes = json.loads(
            pkg_resources.resource_stream(__name__, 'backend_cluster_nodes.json').read().decode())
        backend_node = backend_cluster_nodes[0]
        mock_client.get_cluster_nodes.return_value = backend_cluster_nodes
        mock_client.get_node.return_value = backend_node

        tasks.PollRuntimeStateNodeTask().execute(self.fixture.node)
        self.assertEqual(mock_retry.call_count, 1)

    def test_staff_cannot_create_node_if_cpu_has_not_been_specified(self):
        del self.payload['cpu']
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_create_node_if_flavor_has_been_specified(self, mock_tasks):
        del self.payload['cpu']
        del self.payload['memory']
        self.payload['flavor'] = openstack_tenant_factories.FlavorFactory.get_url(self.flavor)
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(mock_tasks.CreateNodeTask.return_value.si.call_count, 1)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_do_not_create_node_if_flavor_does_not_meet_requirements(self, mock_tasks):
        self.flavor.cores = 1
        self.flavor.ram = 1024
        self.flavor.save()

        del self.payload['cpu']
        del self.payload['memory']
        self.payload['flavor'] = openstack_tenant_factories.FlavorFactory.get_url(self.flavor)
        response = self.create_node(self.fixture.staff)

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

    @mock.patch('waldur_rancher.executors.tasks')
    def test_retry_node_creating(self, mock_tasks):
        self.create_node(self.fixture.staff)
        node = self.fixture.cluster.node_set.filter(name__contains='node-1').first()
        url = factories.NodeFactory.get_url(node, 'retry')
        node.set_erred()
        node.save()
        self.client.post(url)
        self.assertEqual(mock_tasks.RetryNodeTask.return_value.si.call_count, 1)
        tasks.RetryNodeTask().execute(node)
        self.assertNotEquals(node.cluster.node_set.first(), node)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_retry_node_creating_if_related_instance_exists(self, mock_tasks):
        self.create_node(self.fixture.staff)
        node = self.fixture.cluster.node_set.filter(name__contains='node-1').first()
        instance = openstack_tenant_factories.InstanceFactory()
        node.object_id = instance.id
        node.content_type = ContentType.objects.get_for_model(instance)
        url = factories.NodeFactory.get_url(node, 'retry')
        node.set_erred()
        node.save()
        self.client.post(url)
        self.assertEqual(mock_tasks.DeleteNodeTask.return_value.si.call_count, 1)
        node.state = models.Node.States.UPDATING
        node.save()
        instance.delete()
        self.assertNotEquals(node.cluster.node_set.first(), node)


class NodeDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        super(NodeDeleteTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.cluster_name = self.fixture.cluster.name
        self.url = factories.NodeFactory.get_url(self.fixture.node)
        self.fixture.node.instance.runtime_state = self.fixture.node.instance.RuntimeStates.SHUTOFF
        self.fixture.node.instance.save()

    def test_delete_node(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)


class NodeDetailsUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        super(NodeDetailsUpdateTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.fixture.node.backend_id = 'backend_id'
        self.fixture.node.save()

        self.patcher_client = mock.patch('waldur_rancher.backend.RancherBackend.client')
        self.mock_client = self.patcher_client.start()
        self.mock_client.get_node.return_value = json.loads(
            pkg_resources.resource_stream(__name__, 'backend_node.json').read().decode())
        self.mock_client.get_cluster.return_value = json.loads(
            pkg_resources.resource_stream(__name__, 'backend_cluster.json').read().decode())
        self.mock_client.get_cluster_nodes.return_value = [json.loads(
            pkg_resources.resource_stream(__name__, 'backend_node.json').read().decode())]

    def _check_node_fields(self, node):
        node.refresh_from_db()
        self.assertEqual(node.docker_version, '19.3.4')
        self.assertEqual(node.k8s_version, 'v1.14.6')
        self.assertEqual(node.cpu_allocated, 0.38)
        self.assertEqual(node.cpu_total, 1)
        self.assertEqual(node.ram_allocated, 8002)
        self.assertEqual(node.ram_total, 15784)
        self.assertEqual(node.pods_allocated, 8)
        self.assertEqual(node.pods_total, 110)
        self.assertEqual(node.state, models.Node.States.OK)

    def test_update_node_details(self):
        tasks.update_nodes(self.fixture.cluster.id)
        self._check_node_fields(self.fixture.node)

    def test_update_node_if_key_does_not_exists(self):
        backend_node = json.loads(
            pkg_resources.resource_stream(__name__, 'backend_node.json').read().decode())
        backend_node.pop('annotations')
        self.mock_client.get_node.return_value = backend_node
        tasks.update_nodes(self.fixture.cluster.id)
        self._check_node_fields(self.fixture.node)

    def test_pull_cluster_import_new_node(self):
        backend = self.fixture.node.cluster.get_backend()
        backend.pull_cluster(self.fixture.node.cluster)
        self.assertEqual(self.fixture.cluster.node_set.count(), 2)
        node = self.fixture.cluster.node_set.get(name='k8s-node')
        self._check_node_fields(node)

    def test_pull_cluster_update_node(self):
        backend = self.fixture.node.cluster.get_backend()
        self.fixture.node.name = 'k8s-node'
        self.fixture.node.backend_id = ''
        self.fixture.node.save()
        backend.pull_cluster(self.fixture.node.cluster)
        self._check_node_fields(self.fixture.node)
