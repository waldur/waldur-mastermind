import mock
from rest_framework import status, test

from . import factories, fixtures
from .. import models


class BaseNetworkTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()


class NetworkCreateActionTest(BaseNetworkTest):

    def test_network_create_action_is_not_allowed(self):
        self.client.force_authenticate(user=self.fixture.user)
        url = factories.NetworkFactory.get_list_url()

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class NetworkCreateSubnetActionTest(BaseNetworkTest):
    action_name = 'create_subnet'
    quota_name = 'subnet_count'

    def setUp(self):
        super(NetworkCreateSubnetActionTest, self).setUp()
        self.user = self.fixture.owner
        self.client.force_authenticate(self.user)
        self.url = factories.NetworkFactory.get_url(network=self.fixture.network, action=self.action_name)
        self.request_data = {
            'name': 'test_subnet_name',
        }

    def test_create_subnet_is_not_allowed_when_state_is_not_OK(self):
        self.fixture.network.state = models.Network.States.ERRED
        self.fixture.network.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_cannot_create_subnet_when_network_has_one_already(self):
        factories.SubNetFactory(network=self.fixture.network)
        self.assertEqual(self.fixture.network.subnets.count(), 1)

        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_openstack.openstack.executors.SubNetCreateExecutor.execute')
    def test_create_subnet_triggers_create_executor(self, executor_action_mock):
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        executor_action_mock.assert_called_once()

    @mock.patch('waldur_openstack.openstack.executors.SubNetCreateExecutor.execute')
    def test_create_subnet_increases_quota_usage(self, executor_action_mock):
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.fixture.tenant.quotas.get(name=self.quota_name).usage, 1)
        executor_action_mock.assert_called_once()

    @mock.patch('waldur_openstack.openstack.executors.SubNetCreateExecutor.execute')
    def test_create_subnet_does_not_create_subnet_if_quota_exceeds_set_limit(self, executor_action_mock):
        self.fixture.tenant.set_quota_limit(self.quota_name, 0)
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.fixture.tenant.quotas.get(name=self.quota_name).usage, 0)
        executor_action_mock.assert_not_called()


class NetworkUpdateActionTest(BaseNetworkTest):

    def setUp(self):
        super(NetworkUpdateActionTest, self).setUp()
        self.user = self.fixture.owner
        self.client.force_authenticate(self.user)
        self.request_data = {
            'name': 'test_name',
        }

    @mock.patch('waldur_openstack.openstack.executors.NetworkUpdateExecutor.execute')
    def test_update_action_triggers_update_executor(self, executor_action_mock):
        url = factories.NetworkFactory.get_url(network=self.fixture.network)
        response = self.client.put(url, self.request_data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        executor_action_mock.assert_called_once()


@mock.patch('waldur_openstack.openstack.executors.NetworkDeleteExecutor.execute')
class NetworkDeleteActionTest(BaseNetworkTest):

    def setUp(self):
        super(NetworkDeleteActionTest, self).setUp()
        self.user = self.fixture.owner
        self.client.force_authenticate(self.user)
        self.request_data = {
            'name': 'test_name',
        }

    def test_delete_action_triggers_delete_executor(self, executor_action_mock):
        url = factories.NetworkFactory.get_url(network=self.fixture.network)
        response = self.client.delete(url, self.request_data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        executor_action_mock.assert_called_once()

    def test_delete_action_decreases_quota_usage(self, executor_action_mock):
        url = factories.NetworkFactory.get_url(network=self.fixture.network)
        self.fixture.network.increase_backend_quotas_usage()
        self.assertEqual(self.fixture.tenant.quotas.get(name='network_count').usage, 1)

        response = self.client.delete(url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        executor_action_mock.assert_called_once()
