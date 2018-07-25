from rest_framework import test, status

from . import factories, fixtures


class FloatingIPListRetreiveTestCase(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.active_ip = factories.FloatingIPFactory(runtime_state='ACTIVE', service_project_link=self.fixture.openstack_spl)
        self.down_ip = factories.FloatingIPFactory(runtime_state='DOWN', service_project_link=self.fixture.openstack_spl)
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
        self.assertItemsEqual(response_ip_uuids, expected_ip_uuids)

    def test_floating_ip_list_can_be_filtered_by_service(self):
        data = {
            'service_uuid': self.fixture.openstack_service.uuid.hex,
        }
        # when
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.FloatingIPFactory.get_list_url(), data)
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_ip_uuids = [ip['uuid'] for ip in response.data]
        expected_ip_uuids = [ip.uuid.hex for ip in (self.active_ip, self.down_ip)]
        self.assertItemsEqual(response_ip_uuids, expected_ip_uuids)

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
        self.assertItemsEqual(response_ip_uuids, expected_ip_uuids)

    def test_admin_receive_only_ips_from_his_project(self):
        # when
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(factories.FloatingIPFactory.get_list_url())
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_ip_uuids = [ip['uuid'] for ip in response.data]
        expected_ip_uuids = [ip.uuid.hex for ip in (self.active_ip, self.down_ip)]
        self.assertItemsEqual(response_ip_uuids, expected_ip_uuids)

    def test_owner_receive_only_ips_from_his_customer(self):
        # when
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.FloatingIPFactory.get_list_url())
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_ip_uuids = [ip['uuid'] for ip in response.data]
        expected_ip_uuids = [ip.uuid.hex for ip in (self.active_ip, self.down_ip)]
        self.assertItemsEqual(response_ip_uuids, expected_ip_uuids)

    def test_regular_user_does_not_receive_any_ips(self):
        # when
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(factories.FloatingIPFactory.get_list_url())
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_ip_uuids = [ip['uuid'] for ip in response.data]
        expected_ip_uuids = []
        self.assertItemsEqual(response_ip_uuids, expected_ip_uuids)

    def test_admin_can_retrieve_floating_ip_from_his_project(self):
        # when
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(factories.FloatingIPFactory.get_url(self.active_ip))
        # then
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertItemsEqual(response.data['uuid'], self.active_ip.uuid.hex)

    def test_owner_can_not_retrieve_floating_ip_not_from_his_customer(self):
        # when
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.FloatingIPFactory.get_url(self.other_ip))
        # then
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
