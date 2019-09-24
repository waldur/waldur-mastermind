import mock

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories

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


class ClusterCreateTest(test.APITransactionTestCase):
    def setUp(self):
        super(ClusterCreateTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.url = factories.ClusterFactory.get_list_url()

    @mock.patch('waldur_rancher.executors.core_tasks')
    def test_create_cluster(self, mock_core_tasks):
        self.client.force_authenticate(self.fixture.owner)
        instance = self._create_new_test_instance(self.fixture.customer)
        response = self._create_request_('new-cluster', instance)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Cluster.objects.filter(name='new-cluster').exists())
        cluster = models.Cluster.objects.get(name='new-cluster')
        self.assertTrue(models.Node.objects.filter(cluster=cluster).exists())
        node = models.Node.objects.get(cluster=cluster)
        self.assertEqual(node.instance, instance)
        mock_core_tasks.BackendMethodTask.return_value.si.assert_called_once_with(
            'waldur_rancher.cluster:%s' % cluster.id,
            'create_cluster',
            state_transition='begin_creating'
        )

    def test_user_cannot_create_cluster_if_instance_is_not_available(self):
        self.client.force_authenticate(self.fixture.owner)
        instance = structure_factories.TestNewInstanceFactory()
        response = self._create_request_('new-cluster', instance)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('Can\'t restore object from' in response.data['instance'][0])

    def test_validate_if_instance_is_already_in_use(self):
        self.client.force_authenticate(self.fixture.owner)
        self._create_request_('new-cluster', self.fixture.instance)
        response = self._create_request_('new-cluster', self.fixture.instance)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('The selected instance is already in use.' in response.data['instance'][0])

    def test_validate_name_uniqueness(self):
        self.client.force_authenticate(self.fixture.owner)
        instance_1 = self._create_new_test_instance(self.fixture.customer)
        self._create_request_('new-cluster', instance_1)
        instance_2 = self._create_new_test_instance(self.fixture.customer)
        response = self._create_request_('new-cluster', instance_2)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_name(self):
        self.client.force_authenticate(self.fixture.owner)
        instance = self._create_new_test_instance(self.fixture.customer)
        response = self._create_request_('new_cluster', instance)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def _create_new_test_instance(self, customer):
        settings = structure_factories.ServiceSettingsFactory(customer=customer)
        service = structure_factories.TestServiceFactory(customer=customer, settings=settings)
        spl = structure_factories.TestServiceProjectLinkFactory(service=service, project=self.fixture.project)
        return structure_factories.TestNewInstanceFactory(service_project_link=spl)

    def _create_request_(self, name, instance):
        return self.client.post(self.url,
                                {'name': name,
                                 'instance':
                                     structure_factories.TestNewInstanceFactory.get_url(instance),
                                 'service_project_link':
                                     factories.RancherServiceProjectLinkFactory.get_url(self.fixture.spl),
                                 })


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
