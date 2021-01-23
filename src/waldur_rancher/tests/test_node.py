import json
from unittest import mock

import pkg_resources
from rest_framework import status, test

from waldur_core.structure.tests.factories import SshPublicKeyFactory
from waldur_openstack.openstack_tenant.tests import (
    factories as openstack_tenant_factories,
)
from waldur_rancher import models, tasks
from waldur_rancher import utils as rancher_utils
from waldur_rancher.tests import factories, fixtures, test_cluster, utils


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
        self.client.force_authenticate(self.fixture.owner)
        response = self._create_request_(name='name')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        cluster = models.Cluster.objects.get(name='name')
        self.assertTrue(mock_executors.ClusterCreateExecutor.execute.called)
        create_node_task = tasks.CreateNodeTask()
        create_node_task.execute(
            mock_executors.ClusterCreateExecutor.execute.mock_calls[0][1][
                0
            ].node_set.first(),
            user_id=mock_executors.ClusterCreateExecutor.execute.mock_calls[0][2][
                'user'
            ].id,
        )
        self.assertTrue(cluster.node_set.filter(cluster=cluster).exists())

    def create_node(self, user):
        self.client.force_authenticate(user)
        return self.client.post(self.node_url, self.payload)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_staff_can_create_node(self, mock_tasks):
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(mock_tasks.CreateNodeTask.return_value.si.call_count, 1)

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
                    'volume_type': openstack_tenant_factories.VolumeTypeFactory.get_url(
                        volume_type
                    ),
                    'mount_point': '/var/lib/etcd',
                }
            ],
        }
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.fixture.cluster.node_set.count(), 2)
        node = self.fixture.cluster.node_set.exclude(name='').get()
        self.assertEqual(len(node.initial_data['data_volumes']), 1)

    @utils.override_plugin_settings(MOUNT_POINT_CHOICE_IS_MANDATORY=False)
    @mock.patch('waldur_rancher.executors.tasks')
    def test_use_data_volumes_without_mount_point(self, mock_tasks):
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
                    'volume_type': openstack_tenant_factories.VolumeTypeFactory.get_url(
                        volume_type
                    ),
                }
            ],
        }
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.fixture.cluster.node_set.count(), 2)
        node = self.fixture.cluster.node_set.exclude(name='').get()
        self.assertEqual(len(node.initial_data['data_volumes']), 1)

    @utils.override_plugin_settings(MOUNT_POINT_CHOICE_IS_MANDATORY=True)
    @mock.patch('waldur_rancher.executors.tasks')
    def test_if_mount_point_is_required(self, mock_tasks):
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
                    'volume_type': openstack_tenant_factories.VolumeTypeFactory.get_url(
                        volume_type
                    ),
                }
            ],
        }
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_poll_node_after_it_has_been_created(self, mock_tasks):
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            mock_tasks.PollRuntimeStateNodeTask.return_value.si.call_count, 1
        )

    @mock.patch('waldur_rancher.backend.RancherBackend.pull_node')
    @mock.patch('waldur_rancher.backend.RancherBackend.client')
    @mock.patch('waldur_rancher.tasks.PollRuntimeStateNodeTask.retry')
    def test_not_pulling_if_node_has_been_created(
        self, mock_retry, mock_client, mock_pull_node
    ):
        backend_node = json.loads(
            pkg_resources.resource_stream(__name__, 'backend_node.json').read().decode()
        )
        self.fixture.node.backend_id = ''
        self.fixture.node.name = backend_node['requestedHostname']
        self.fixture.node.runtime_state = models.Node.RuntimeStates.ACTIVE
        self.fixture.node.save()
        mock_client.get_cluster_nodes.return_value = [backend_node]
        tasks.PollRuntimeStateNodeTask().execute(self.fixture.node)
        self.assertEqual(mock_retry.call_count, 0)
        self.fixture.node.refresh_from_db()
        self.assertEqual(self.fixture.node.backend_id, backend_node['id'])

    @mock.patch('waldur_rancher.tasks.pull_cluster_nodes')
    @mock.patch('waldur_rancher.tasks.PollRuntimeStateNodeTask.retry')
    def test_pulling_if_node_has_not_been_created(self, mock_retry, mock_update_nodes):
        tasks.PollRuntimeStateNodeTask().execute(self.fixture.node)
        self.assertEqual(mock_retry.call_count, 1)

    def test_others_cannot_create_node(self):
        response = self.create_node(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_cannot_create_node_if_cpu_has_not_been_specified(self):
        del self.payload['cpu']
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_create_node_if_flavor_has_been_specified(self, mock_tasks):
        del self.payload['cpu']
        del self.payload['memory']
        self.payload['flavor'] = openstack_tenant_factories.FlavorFactory.get_url(
            self.flavor
        )
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
        self.payload['flavor'] = openstack_tenant_factories.FlavorFactory.get_url(
            self.flavor
        )
        response = self.create_node(self.fixture.staff)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_create_is_disabled_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self._create_request_(name='name')
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @mock.patch('waldur_rancher.executors.tasks')
    def test_use_ssh_public_key(self, mock_tasks):
        ssh_public_key = SshPublicKeyFactory(user=self.fixture.owner)
        self.payload = {
            'cluster': factories.ClusterFactory.get_url(self.fixture.cluster),
            'subnet': openstack_tenant_factories.SubNetFactory.get_url(self.subnet),
            'system_volume_size': 1024,
            'memory': 1,
            'cpu': 1,
            'roles': ['controlplane', 'etcd', 'worker'],
            'ssh_public_key': SshPublicKeyFactory.get_url(ssh_public_key),
        }
        response = self.create_node(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.fixture.cluster.node_set.count(), 2)
        node = self.fixture.cluster.node_set.exclude(name='').get()
        self.assertEqual(node.initial_data['ssh_public_key'], ssh_public_key.uuid.hex)

    def test_node_config_formatting(self):
        template = '''
                #cloud-config
                  packages:
                    - curl
                  runcmd:
                    - curl -fsSL https://get.docker.com -o get-docker.sh; sh get-docker.sh
                    - sudo systemctl start docker
                    - sudo systemctl enable docker
                    - [ sh, -c, "{command}" ]
                '''
        service_settings = factories.RancherServiceSettingsFactory(
            options={'cloud_init_template': template}
        )
        service = factories.RancherServiceFactory(settings=service_settings)
        spl = factories.RancherServiceProjectLinkFactory(service=service)
        cluster = factories.ClusterFactory(
            settings=self.fixture.settings, service_project_link=spl
        )
        node = factories.NodeFactory(
            cluster=cluster, initial_data={'data_volumes': [{'mount_point': 'path'}]}
        )
        result = rancher_utils.format_node_cloud_config(node)
        expected_config = """
            fs_setup:
                - device: /dev/vdb
                filesystem: ext4
                mounts:
                    - /dev/vdb
                    - path
                packages:
                    - curl
                runcmd:
                    - curl -fsSL https://get.docker.com -o get-docker.sh; sh get-docker.sh
                    - sudo systemctl start docker
                    - sudo systemctl enable docker
                    - - sh
                      - -c
                      - ' '
        """

        self.assertTrue(expected_config, result)


class NodePullTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.url = factories.NodeFactory.get_url(self.fixture.node, action='pull')

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_pull_is_enabled_for_staff_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_pull_is_disabled_for_owner_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_pull_is_enabled_for_owner_when_read_only_mode_is_disabled(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)


class NodeDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        super(NodeDeleteTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.cluster_name = self.fixture.cluster.name
        self.url = factories.NodeFactory.get_url(self.fixture.node)
        self.fixture.node.instance.runtime_state = (
            self.fixture.node.instance.RuntimeStates.SHUTOFF
        )
        self.fixture.node.instance.save()

    def test_delete_node(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_delete_is_disabled_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class NodePullBackendTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.fixture.node.backend_id = 'backend_id'
        self.fixture.node.save()

        self.patcher_client = mock.patch('waldur_rancher.backend.RancherBackend.client')
        self.mock_client = self.patcher_client.start()

        self.mock_client.get_node.return_value = json.loads(
            pkg_resources.resource_stream(__name__, 'backend_node.json').read().decode()
        )
        self.mock_client.get_cluster.return_value = json.loads(
            pkg_resources.resource_stream(__name__, 'backend_cluster.json')
            .read()
            .decode()
        )
        self.mock_client.get_cluster_nodes.return_value = [
            json.loads(
                pkg_resources.resource_stream(__name__, 'backend_node.json').read()
            )
        ]

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

    def test_update_node_if_key_does_not_exists(self):
        backend_node = json.loads(
            pkg_resources.resource_stream(__name__, 'backend_node.json').read().decode()
        )
        backend_node.pop('annotations')
        self.mock_client.get_node.return_value = backend_node
        tasks.pull_cluster_nodes(self.fixture.cluster.id)
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

    def test_pull_node(self):
        backend = self.fixture.node.get_backend()
        self.fixture.node.name = 'k8s-node'
        self.fixture.node.backend_id = 'backend_id'
        self.fixture.node.save()
        backend.pull_node(self.fixture.node)
        self._check_node_fields(self.fixture.node)


class NodeLinkTest(test_cluster.BaseClusterCreateTest):
    def setUp(self):
        super().setUp()
        self.settings = factories.RancherServiceSettingsFactory()
        self.cluster = factories.ClusterFactory(settings=self.settings)
        self.node = factories.NodeFactory(cluster=self.cluster)
        self.url = factories.NodeFactory.get_url(self.node, 'link_openstack')
        self.instance = openstack_tenant_factories.InstanceFactory()
        self.instance_url = openstack_tenant_factories.InstanceFactory.get_url(
            self.instance
        )

    def test_link_is_enabled_when_read_only_mode_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {'instance': self.instance_url})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.node.refresh_from_db()
        self.assertEqual(self.node.instance, self.instance)

    def test_link_is_disabled_when_user_is_not_staff(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {'instance': self.instance_url})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_link_is_disabled_when_node_is_already_linked(self):
        self.node.instance = self.instance
        self.node.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {'instance': self.instance_url})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_link_is_disabled_when_read_only_mode_is_enabled(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {'instance': self.instance_url})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class NodeUnlinkTest(test_cluster.BaseClusterCreateTest):
    def setUp(self):
        super().setUp()
        self.settings = factories.RancherServiceSettingsFactory()
        self.cluster = factories.ClusterFactory(settings=self.settings)
        self.instance = openstack_tenant_factories.InstanceFactory()
        self.node = factories.NodeFactory(cluster=self.cluster, instance=self.instance)
        self.url = factories.NodeFactory.get_url(self.node, 'unlink_openstack')

    def test_unlink_is_enabled_when_read_only_mode_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.node.refresh_from_db()
        self.assertEqual(self.node.instance, None)

    def test_unlink_is_disabled_when_user_is_not_staff(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unlink_is_disabled_when_node_is_already_unlinked(self):
        self.node.instance = None
        self.node.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_unlink_is_disabled_when_read_only_mode_is_enabled(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class NodeActionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.RancherFixture()
        self.node = self.fixture.node

        self.url = factories.NodeFactory.get_url(self.node, action=self.action)
        self.mock_path = mock.patch(
            'waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.%s'
            % self.backend_method
        )
        self.mock_console = self.mock_path.start()
        self.mock_console.return_value = self.backend_return_value

        mock_path = mock.patch('waldur_rancher.utils.InstanceViewSet')
        mock_instance_view_set = mock_path.start()
        self.mock_check_permissions = mock.MagicMock()
        mock_instance_view_set.console_permissions = [self.mock_check_permissions]
        mock_instance_view_set.console_log_permissions = [self.mock_check_permissions]

    def tearDown(self):
        super(NodeActionsTest, self).tearDown()
        mock.patch.stopall()


class NodeConsoleTest(NodeActionsTest):
    action = 'console'
    backend_method = 'get_console_url'
    backend_return_value = 'url'

    def test_check_of_permissions_is_the_same_as_openstack_tenant_view(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(self.mock_check_permissions.called)


class NodeConsoleLogTest(NodeActionsTest):
    action = 'console_log'
    backend_method = 'get_console_output'
    backend_return_value = 'openstack-vm login: '

    def test_check_of_permissions_is_the_same_as_openstack_tenant_view(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(self.mock_check_permissions.called)
