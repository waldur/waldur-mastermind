from unittest import mock

from rest_framework import status, test

from . import factories, fixtures


class BasePortTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.owner)


class PortCreateActionTest(BasePortTest):
    def setUp(self):
        super(PortCreateActionTest, self).setUp()
        self.url = factories.PortFactory.get_list_url()

    def test_port_create_action_is_not_allowed(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class PortDeleteTest(BasePortTest):
    def setUp(self) -> None:
        super(PortDeleteTest, self).setUp()
        self.port = self.fixture.port
        self.url = factories.PortFactory.get_url(self.port)

    @mock.patch('waldur_openstack.openstack.executors.PortDeleteExecutor.execute')
    def test_port_update_triggers_executor(self, delete_port_executor_action_mock):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        delete_port_executor_action_mock.assert_called_once()
