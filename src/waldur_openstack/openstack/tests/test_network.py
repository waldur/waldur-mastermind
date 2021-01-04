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

        core_tasks_mock().si.assert_has_calls(
            [
                mock.call(
                    core_utils.serialize_instance(subnet),
                    'create_subnet',
                    state_transition='begin_creating',
                    enable_default_gateway=False,
                )
            ]
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


class NetworkCreatePortActionTest(BaseNetworkTest):
    def setUp(self):
        super(NetworkCreatePortActionTest, self).setUp()
        self.client.force_authenticate(user=self.fixture.admin)
        self.network = self.fixture.network
        self.url = factories.NetworkFactory.get_url(self.network, action='create_port')
        self.request_data = {'name': 'test_port_name'}
        self.subnet = self.fixture.subnet
        self.subnet.backend_id = f'{self.subnet.name}_backend_id'
        self.subnet.save()

    def test_create_port_if_network_has_ok_state(self):
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_create_port_if_network_has_erred_state(self):
        self.network.state = models.Network.States.ERRED
        self.network.save()

        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @mock.patch('waldur_openstack.openstack.executors.PortCreateExecutor.execute')
    def test_create_port_triggers_executor(self, create_port_executor_action_mock):
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        create_port_executor_action_mock.assert_called_once()

    def test_create_port_with_fixed_ips(self):
        self.request_data['fixed_ips'] = [
            {'ip_address': '192.168.1.10', 'subnet_id': self.subnet.backend_id,},
            {'subnet_id': self.subnet.backend_id},
            {'ip_address': '192.168.1.12',},
        ]
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        floating_ips_response = [
            (item.get('ip_address'), item.get('subnet_id'))
            for item in response.data['fixed_ips']
        ]
        self.assertEqual(len(floating_ips_response), 3)
        self.assertEqual(
            floating_ips_response,
            [
                ('192.168.1.10', self.subnet.backend_id,),
                (None, self.subnet.backend_id,),
                ('192.168.1.12', None),
            ],
        )

    def test_create_port_with_subnet_from_different_tenant(self):
        new_fixture = fixtures.OpenStackFixture()
        subnet = new_fixture.subnet
        subnet.backend_id = f'{subnet.name}_backend_id'
        subnet.save()
        self.request_data['fixed_ips'] = [{'subnet_id': subnet.backend_id}]
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('There is no subnet with backend_id', response.data['subnet'][0])

    def test_create_port_with_invalid_fixed_ips(self):
        self.request_data['fixed_ips'] = [
            {'subnet_id': 'some_backend_id', 'garbage': 'value'}
        ]
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            'Only ip_address and subnet_id fields can be specified',
            response.data['non_field_errors'][0],
        )

    def test_create_port_with_blank_ip_address(self):
        self.request_data['fixed_ips'] = [
            {'ip_address': '',},
        ]
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            'ip_address field must not be blank', response.data['non_field_errors'][0],
        )

    def test_create_port_with_blank_subnet_id(self):
        self.request_data['fixed_ips'] = [
            {'subnet_id': '',},
        ]
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            'subnet_id field must not be blank', response.data['non_field_errors'][0],
        )

    def test_create_port_with_invalid_ip_address(self):
        self.request_data['fixed_ips'] = [
            {'ip_address': 'abc',},
        ]
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            'Enter a valid IPv4 or IPv6 address.', response.data['non_field_errors'][0],
        )
