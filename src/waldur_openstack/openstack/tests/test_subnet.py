from unittest import mock

from rest_framework import status, test

from waldur_core.core import utils as core_utils

from . import factories, fixtures


class BaseSubNetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()


class SubNetCreateActionTest(BaseSubNetTest):
    def setUp(self):
        super(SubNetCreateActionTest, self).setUp()
        self.client.force_authenticate(user=self.fixture.user)

    def test_subnet_create_action_is_not_allowed(self):
        url = factories.SubNetFactory.get_list_url()
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


@mock.patch('waldur_openstack.openstack.executors.SubNetDeleteExecutor.execute')
class SubNetDeleteActionTest(BaseSubNetTest):
    def setUp(self):
        super(SubNetDeleteActionTest, self).setUp()
        self.client.force_authenticate(user=self.fixture.admin)
        self.url = factories.SubNetFactory.get_url(self.fixture.subnet)

    def test_subnet_delete_action_triggers_create_executor(self, executor_action_mock):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        executor_action_mock.assert_called_once()

    def test_subnet_delete_action_schedules_executor(self, executor_action_mock):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        executor_action_mock.assert_called_once()


class SubNetUpdateActionTest(BaseSubNetTest):
    def setUp(self):
        super(SubNetUpdateActionTest, self).setUp()
        self.client.force_authenticate(user=self.fixture.admin)
        self.url = factories.SubNetFactory.get_url(self.fixture.subnet)
        self.request_data = {'name': 'test_name'}

    @mock.patch('waldur_openstack.openstack.executors.SubNetUpdateExecutor.execute')
    def test_subnet_update_action_triggers_update_executor(self, executor_action_mock):
        response = self.client.put(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        executor_action_mock.assert_called_once()

    def test_subnet_update_does_not_reset_cidr(self):
        CIDR = '10.1.0.0/24'
        subnet = self.fixture.subnet
        subnet.cidr = CIDR
        subnet.save()

        response = self.client.put(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        subnet.refresh_from_db()
        self.assertEqual(subnet.cidr, CIDR)

    @mock.patch('waldur_openstack.openstack.executors.core_tasks.BackendMethodTask')
    def test_subnet_updating_if_enable_default_gateway_is_false(self, core_tasks_mock):
        self.request_data['enable_default_gateway'] = False
        response = self.client.put(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        core_tasks_mock().si.assert_has_calls(
            [
                mock.call(
                    core_utils.serialize_instance(self.fixture.subnet),
                    'update_subnet',
                    state_transition='begin_updating',
                    enable_default_gateway=False,
                )
            ]
        )
