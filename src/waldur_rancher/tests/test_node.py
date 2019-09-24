from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories

from . import factories, fixtures
from .. import models


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


class NodeCreateTest(test.APITransactionTestCase):
    def setUp(self):
        super(NodeCreateTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.url = factories.NodeFactory.get_list_url()

    def test_create_node_if_cluster_has_been_created(self):
        self.client.force_authenticate(self.fixture.owner)
        instance = self._create_new_test_instance(customer=self.fixture.customer)
        url = factories.ClusterFactory.get_list_url()
        response = self.client.post(url,
                                    {'name': 'name',
                                     'instance':
                                         structure_factories.TestNewInstanceFactory.get_url(instance),
                                     'service_project_link':
                                         factories.RancherServiceProjectLinkFactory.get_url(self.fixture.spl),
                                     })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        cluster = models.Cluster.objects.get(name='name')
        self.assertTrue(cluster.node_set.filter(object_id=instance.id).exists())

    def test_create_node(self):
        self.client.force_authenticate(self.fixture.owner)
        cluster = self.fixture.cluster
        instance = self._create_new_test_instance(customer=self.fixture.customer)
        response = self.client.post(self.url,
                                    {'cluster': factories.ClusterFactory.get_url(cluster),
                                     'instance': structure_factories.TestNewInstanceFactory.get_url(instance),
                                     })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(cluster.node_set.filter(object_id=instance.id).exists())

    def test_user_cannot_create_node_if_instance_is_not_available(self):
        self.client.force_authenticate(self.fixture.owner)
        cluster = self.fixture.cluster
        instance = structure_factories.TestNewInstanceFactory()
        response = self.client.post(self.url,
                                    {'cluster': factories.ClusterFactory.get_url(cluster),
                                     'instance': structure_factories.TestNewInstanceFactory.get_url(instance),
                                     })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('Can\'t restore object from' in response.data['instance'][0])

    def test_validate_if_instance_is_already_in_use(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url,
                                    {'cluster': factories.ClusterFactory.get_url(self.fixture.cluster),
                                     'instance': structure_factories.TestNewInstanceFactory.get_url(self.fixture.instance),
                                     })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('The selected instance is already in use.' in response.data['instance'][0])

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
