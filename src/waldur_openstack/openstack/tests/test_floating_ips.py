from unittest import mock

from rest_framework import status, test

from waldur_openstack.openstack import models
from waldur_openstack.openstack.tests import factories, fixtures


class FloatingIPListRetrieveTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.active_ip = factories.FloatingIPFactory(
            runtime_state='ACTIVE',
            service_settings=self.fixture.openstack_service_settings,
            project=self.fixture.project,
        )
        self.down_ip = factories.FloatingIPFactory(
            runtime_state='DOWN',
            service_settings=self.fixture.openstack_service_settings,
            project=self.fixture.project,
        )
        self.other_ip = factories.FloatingIPFactory(runtime_state='UNDEFINED')

    def test_floating_ip_list_can_be_filtered_by_project(self):
        data = {
            'project': self.fixture.project.uuid.hex,
        }
        # when
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.FloatingIPFactory.get_list_url(), data)
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_ip_uuids = [ip['uuid'] for ip in response.data]
        expected_ip_uuids = [ip.uuid.hex for ip in (self.active_ip, self.down_ip)]
        self.assertEqual(sorted(response_ip_uuids), sorted(expected_ip_uuids))

    def test_floating_ip_list_can_be_filtered_by_status(self):
        data = {
            'runtime_state': 'ACTIVE',
        }
        # when
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.FloatingIPFactory.get_list_url(), data)
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_ip_uuids = [ip['uuid'] for ip in response.data]
        expected_ip_uuids = [self.active_ip.uuid.hex]
        self.assertEqual(response_ip_uuids, expected_ip_uuids)

    def test_admin_receive_only_ips_from_his_project(self):
        # when
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(factories.FloatingIPFactory.get_list_url())
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_ip_uuids = [ip['uuid'] for ip in response.data]
        expected_ip_uuids = [ip.uuid.hex for ip in (self.active_ip, self.down_ip)]
        self.assertEqual(sorted(response_ip_uuids), sorted(expected_ip_uuids))

    def test_owner_receive_only_ips_from_his_customer(self):
        # when
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.FloatingIPFactory.get_list_url())
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_ip_uuids = [ip['uuid'] for ip in response.data]
        expected_ip_uuids = [ip.uuid.hex for ip in (self.active_ip, self.down_ip)]
        self.assertEqual(sorted(response_ip_uuids), sorted(expected_ip_uuids))

    def test_regular_user_does_not_receive_any_ips(self):
        # when
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(factories.FloatingIPFactory.get_list_url())
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_ip_uuids = [ip['uuid'] for ip in response.data]
        expected_ip_uuids = []
        self.assertEqual(response_ip_uuids, expected_ip_uuids)

    def test_admin_can_retrieve_floating_ip_from_his_project(self):
        # when
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(factories.FloatingIPFactory.get_url(self.active_ip))
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['uuid'], self.active_ip.uuid.hex)

    def test_owner_can_not_retrieve_floating_ip_not_from_his_customer(self):
        # when
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.FloatingIPFactory.get_url(self.other_ip))
        # then
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class BaseFloatingIPTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OpenStackFixture()
        self.ip = self.fixture.floating_ip
        self.port = self.fixture.port
        self.client.force_login(self.fixture.staff)


class FloatingIPRetrieveTest(BaseFloatingIPTest):
    def test_floating_ip_access_from_port(self):
        self.ip.port = self.port
        self.ip.save()
        response = self.client.get(factories.PortFactory.get_url(self.port))
        response_fips = response.data['floating_ips']
        self.assertEqual([factories.FloatingIPFactory.get_url(self.ip)], response_fips)


class FloatingIPAttachTest(BaseFloatingIPTest):
    def setUp(self) -> None:
        super().setUp()
        self.request_data = {'port': factories.PortFactory.get_url(self.port)}
        self.url = factories.FloatingIPFactory.get_url(self.ip, action='attach_to_port')

    def test_floating_ip_attach(self):
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_floating_ip_attaching_for_ip_with_not_ok_state(self):
        self.ip.state = models.FloatingIP.States.ERRED
        self.ip.save()
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @mock.patch('waldur_openstack.openstack.executors.FloatingIPAttachExecutor.execute')
    def test_floating_ip_attaching_triggers_executor(
        self, attach_ip_executor_action_mock
    ):
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        attach_ip_executor_action_mock.assert_called_once()

    def test_floating_ip_attaching_to_port_with_not_ok_state(self):
        self.port.state = models.Port.States.ERRED
        self.port.save()
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('state', response.data['detail'])

    def test_floating_ip_attaching_to_port_from_different_tenant(self):
        self.port.tenant = factories.TenantFactory()
        self.port.save()
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('tenant', response.data['detail'])


class FloatingIPDetachTest(BaseFloatingIPTest):
    def setUp(self) -> None:
        super().setUp()
        self.url = factories.FloatingIPFactory.get_url(
            self.ip, action='detach_from_port'
        )
        self.ip.port = self.port
        self.ip.save()

    def test_floating_ip_detach(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_floating_ip_detaching_for_ip_with_not_ok_state(self):
        self.ip.state = models.FloatingIP.States.ERRED
        self.ip.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @mock.patch('waldur_openstack.openstack.executors.FloatingIPDetachExecutor.execute')
    def test_floating_ip_detaching_triggers_executor(
        self, detach_ip_executor_action_mock
    ):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        detach_ip_executor_action_mock.assert_called_once()

    def test_floating_ip_detaching_without_port(self):
        self.ip.port = None
        self.ip.save()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not attached to any port', response.data['port'])
