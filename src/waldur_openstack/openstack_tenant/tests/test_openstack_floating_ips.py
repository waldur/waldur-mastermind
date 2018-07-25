from rest_framework import test, status

from waldur_openstack.openstack.tests import factories as openstack_factories

from . import fixtures


class OpenStackFloatingIPGetTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.client.force_authenticate(self.fixture.staff)

    def test_instance_information_is_returned_for_associated_floating_ip(self):
        self.fixture.floating_ip.runtime_state = 'ACTIVE'
        self.fixture.floating_ip.internal_ip = self.fixture.internal_ip
        self.fixture.floating_ip.save()
        openstack_factories.FloatingIPFactory(backend_id=self.fixture.floating_ip.backend_id,
                                              address=self.fixture.floating_ip.address)

        response = self.client.get(openstack_factories.FloatingIPFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['instance_uuid'], self.fixture.instance.uuid)
        self.assertEqual(response.data[0]['instance_name'], self.fixture.instance.name)
        self.assertIn(self.fixture.instance.uuid.hex, response.data[0]['instance_url'])

    def test_instance_information_is_empty_for_unassociated_floating_ip(self):
        openstack_factories.FloatingIPFactory(backend_id=self.fixture.floating_ip.backend_id,
                                              address=self.fixture.floating_ip.address)

        response = self.client.get(openstack_factories.FloatingIPFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertIsNone(response.data[0]['instance_uuid'])
        self.assertIsNone(response.data[0]['instance_name'])
        self.assertIsNone(response.data[0]['instance_url'])

    def test_instance_information_is_empty_if_floating_ip_service_property_is_missing(self):
        openstack_factories.FloatingIPFactory()

        response = self.client.get(openstack_factories.FloatingIPFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertIsNone(response.data[0]['instance_uuid'])
        self.assertIsNone(response.data[0]['instance_name'])
        self.assertIsNone(response.data[0]['instance_url'])
