from unittest import mock

from rest_framework import status, test

from waldur_core.core import utils as core_utils

from .. import models
from . import factories, fixtures


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
        self.url = factories.NetworkFactory.get_url(
            network=self.fixture.network, action=self.action_name
        )
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

    @mock.patch('waldur_openstack.openstack.executors.core_tasks.BackendMethodTask')
    def test_do_not_create_router_for_subnet_if_enable_default_gateway_is_false(
        self, core_tasks_mock
    ):
        request_data = {'name': 'test_subnet_name', 'enable_default_gateway': False}
        response = self.client.post(self.url, request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        subnet = models.SubNet.objects.get(uuid=response.data['uuid'])

        core_tasks_mock().si.assert_called_once_with(
            core_utils.serialize_instance(subnet),
            'create_subnet',
            state_transition='begin_creating',
            enable_default_gateway=False,
        )

    @mock.patch('waldur_openstack.openstack.executors.SubNetCreateExecutor.execute')
    def test_create_subnet_increases_quota_usage(self, executor_action_mock):
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.fixture.tenant.quotas.get(name=self.quota_name).usage, 1)
        executor_action_mock.assert_called_once()

    @mock.patch('waldur_openstack.openstack.executors.SubNetCreateExecutor.execute')
    def test_create_subnet_does_not_create_subnet_if_quota_exceeds_set_limit(
        self, executor_action_mock
    ):
        self.fixture.tenant.set_quota_limit(self.quota_name, 0)
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.fixture.tenant.quotas.get(name=self.quota_name).usage, 0)
        executor_action_mock.assert_not_called()

    def test_metadata(self):
        self.fixture.network.state = models.Network.States.OK
        self.fixture.network.save()
        url = factories.NetworkFactory.get_url(network=self.fixture.network)
        response = self.client.options(url)
        actions = dict(response.data['actions'])
        self.assertEqual(
            actions,
            {
                "create_subnet": {
                    "title": "Create Subnet",
                    "url": url + "create_subnet/",
                    "fields": {
                        "name": {
                            "type": "string",
                            "required": True,
                            "label": "Name",
                            "max_length": 150,
                        },
                        "description": {
                            "type": "string",
                            "required": False,
                            "label": "Description",
                            "max_length": 2000,
                        },
                        "cidr": {"type": "string", "required": False, "label": "CIDR"},
                        "gateway_ip": {
                            "type": "string",
                            "required": False,
                            "label": "Gateway ip",
                        },
                        "disable_gateway": {
                            "type": "boolean",
                            "required": False,
                            "label": "Disable gateway",
                        },
                        "enable_default_gateway": {
                            "type": "boolean",
                            "required": False,
                            "label": "Enable default gateway",
                        },
                    },
                    "enabled": True,
                    "reason": None,
                    "destructive": False,
                    "type": "form",
                    "method": "POST",
                },
                "destroy": {
                    "title": "Destroy",
                    "url": url,
                    "enabled": True,
                    "reason": None,
                    "destructive": True,
                    "type": "button",
                    "method": "DELETE",
                },
                "pull": {
                    "title": "Pull",
                    "url": url + "pull/",
                    "enabled": True,
                    "reason": None,
                    "destructive": False,
                    "type": "button",
                    "method": "POST",
                },
                "update": {
                    "title": "Update",
                    "url": url,
                    "fields": {
                        "name": {
                            "type": "string",
                            "required": True,
                            "label": "Name",
                            "max_length": 150,
                        },
                        "description": {
                            "type": "string",
                            "required": False,
                            "label": "Description",
                            "max_length": 2000,
                        },
                    },
                    "enabled": True,
                    "reason": None,
                    "destructive": False,
                    "type": "form",
                    "method": "PUT",
                },
                "set_mtu": {
                    "title": "Set Mtu",
                    "method": "POST",
                    "destructive": False,
                    "url": url + "set_mtu/",
                    "reason": None,
                    "enabled": True,
                    "type": "form",
                    "fields": {
                        "mtu": {"type": "integer", "required": True, "label": "Mtu"}
                    },
                },
            },
        )


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
