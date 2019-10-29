import json

import mock
import pkg_resources
from rest_framework import status, test

from waldur_core.structure.models import ServiceSettings
from waldur_openstack.openstack.tests import factories as openstack_factories
from waldur_openstack.openstack_tenant.models import OpenStackTenantServiceProjectLink
from waldur_openstack.openstack_tenant.tests import factories as openstack_tenant_factories

from . import factories, fixtures
from .. import models


class ClusterGetTest(test.APITransactionTestCase):
    def setUp(self):
        super(ClusterGetTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.fixture_2 = fixtures.RancherFixture()
        self.url = factories.ClusterFactory.get_list_url()

    def test_get_cluster_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 2)

    def test_user_cannot_get_strangers_clusters(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 1)


class BaseClusterCreateTest(test.APITransactionTestCase):
    def setUp(self):
        super(BaseClusterCreateTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.url = factories.ClusterFactory.get_list_url()
        openstack_service_settings = openstack_factories.OpenStackServiceSettingsFactory(
            customer=self.fixture.customer)
        openstack_service = openstack_factories.OpenStackServiceFactory(
            customer=self.fixture.customer, settings=openstack_service_settings)
        openstack_spl = openstack_factories.OpenStackServiceProjectLinkFactory(
            project=self.fixture.project, service=openstack_service)
        self.tenant = openstack_factories.TenantFactory(service_project_link=openstack_spl)

        settings = ServiceSettings.objects.get(scope=self.tenant)
        project = self.fixture.project
        instance_spl = OpenStackTenantServiceProjectLink.objects.get(
            project=project,
            service__settings=settings)

        openstack_tenant_factories.FlavorFactory(settings=instance_spl.service.settings)
        image = openstack_tenant_factories.ImageFactory(settings=instance_spl.service.settings)
        openstack_tenant_factories.SecurityGroupFactory(name='default',
                                                        settings=instance_spl.service.settings)
        self.fixture.settings.options['base_image_name'] = image.name
        self.fixture.settings.save()

        network = openstack_tenant_factories.NetworkFactory(
            settings=instance_spl.service.settings)
        self.subnet = openstack_tenant_factories.SubNetFactory(
            network=network,
            settings=instance_spl.service.settings)
        self.fixture.settings.options['base_subnet_name'] = self.subnet.name
        self.fixture.settings.save()

    def _create_request_(self, name, disk=1024, memory=1, cpu=1, add_payload=None):
        add_payload = add_payload or {}
        payload = {'name': name,
                   'service_project_link':
                       factories.RancherServiceProjectLinkFactory.get_url(self.fixture.spl),
                   'nodes': [{
                       'subnet': openstack_tenant_factories.SubNetFactory.get_url(self.subnet),
                       'storage': disk,
                       'memory': memory,
                       'cpu': cpu,
                       'roles': ['controlplane', 'etcd', 'worker'],
                   }],
                   }
        payload.update(add_payload)
        return self.client.post(self.url, payload)


class ClusterCreateTest(BaseClusterCreateTest):
    def setUp(self):
        super(ClusterCreateTest, self).setUp()

    @mock.patch('waldur_rancher.executors.core_tasks')
    def test_create_cluster(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        response = self._create_request_('new-cluster')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Cluster.objects.filter(name='new-cluster').exists())
        cluster = models.Cluster.objects.get(name='new-cluster')
        mock_core_tasks.BackendMethodTask.return_value.si.assert_called_once_with(
            'waldur_rancher.cluster:%s' % cluster.id,
            'create_cluster',
            state_transition='begin_creating'
        )

    def test_validate_name_uniqueness(self):
        self.client.force_authenticate(self.fixture.owner)
        self._create_request_('new-cluster')
        response = self._create_request_('new-cluster')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_name(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self._create_request_('new_cluster')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ClusterUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        super(ClusterUpdateTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.cluster_name = self.fixture.cluster.name
        self.url = factories.ClusterFactory.get_url(self.fixture.cluster)

    @mock.patch('waldur_rancher.executors.core_tasks')
    def test_send_backend_request_if_update_cluster_name(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {'name': 'new-name'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_core_tasks.BackendMethodTask.return_value.si.assert_called_once_with(
            'waldur_rancher.cluster:%s' % self.fixture.cluster.id,
            'update_cluster',
            state_transition='begin_updating'
        )

    @mock.patch('waldur_rancher.executors.core_tasks')
    def test_not_send_backend_request_if_update_cluster_description(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {'description': 'description'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_core_tasks.StateTransitionTask.return_value.si.assert_called_once_with(
            'waldur_rancher.cluster:%s' % self.fixture.cluster.id,
            state_transition='begin_updating'
        )


class ClusterDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        super(ClusterDeleteTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.cluster_name = self.fixture.cluster.name
        self.url = factories.ClusterFactory.get_url(self.fixture.cluster)

    @mock.patch('waldur_rancher.executors.core_tasks')
    def test_delete_cluster(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_core_tasks.BackendMethodTask.return_value.si.assert_called_once_with(
            'waldur_rancher.cluster:%s' % self.fixture.cluster.id,
            'delete_cluster',
            state_transition='begin_deleting'
        )

    def test_not_delete_cluster_if_state_is_not_ok(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.cluster.state = models.Cluster.States.CREATION_SCHEDULED
        self.fixture.cluster.save()
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class BaseProjectImportTest(test.APITransactionTestCase):
    def _generate_backend_clusters(self):
        backend_cluster = json.loads(
            pkg_resources.resource_stream(__name__, 'backend_cluster.json').read().decode())
        return [backend_cluster]


class ClusterImportableResourcesTest(BaseProjectImportTest):

    def setUp(self):
        super(ClusterImportableResourcesTest, self).setUp()
        self.url = factories.ClusterFactory.get_list_url('importable_resources')
        self.fixture = fixtures.RancherFixture()
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch('waldur_rancher.backend.RancherBackend.get_clusters_for_import')
    def test_importable_clusters_are_returned(self, get_projects_mock):
        backend_clusters = self._generate_backend_clusters()
        get_projects_mock.return_value = backend_clusters
        data = {
            'service_project_link':
                factories.RancherServiceProjectLinkFactory.get_url(self.fixture.spl)
        }

        response = self.client.get(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEquals(len(response.data), len(backend_clusters))
        returned_backend_ids = [item['backend_id'] for item in response.data]
        expected_backend_ids = [item['id'] for item in backend_clusters]
        self.assertItemsEqual(returned_backend_ids, expected_backend_ids)
        get_projects_mock.assert_called()


class ClusterImportResourceTest(BaseProjectImportTest):

    def setUp(self):
        super(ClusterImportResourceTest, self).setUp()
        self.url = factories.ClusterFactory.get_list_url('import_resource')
        self.fixture = fixtures.RancherFixture()
        self.client.force_authenticate(self.fixture.owner)

        self.patcher_import = mock.patch('waldur_rancher.backend.RancherBackend.import_cluster')
        self.mock_import = self.patcher_import.start()
        self.mock_import.return_value = self._generate_backend_clusters()[0]

    def tearDown(self):
        mock.patch.stopall()

    def test_backend_cluster_is_imported(self):
        backend_id = 'backend_id'

        payload = {
            'backend_id': backend_id,
            'service_project_link': factories.RancherServiceProjectLinkFactory.get_url(self.fixture.spl),
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_backend_cluster_cannot_be_imported_if_it_is_registered_in_waldur(self):
        cluster = factories.ClusterFactory(service_project_link=self.fixture.spl)

        payload = {
            'backend_id': cluster.backend_id,
            'service_project_link': factories.RancherServiceProjectLinkFactory.get_url(self.fixture.spl),
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
