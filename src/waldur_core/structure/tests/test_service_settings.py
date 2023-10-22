from unittest import mock

from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.registry import SupportedServices
from waldur_core.structure.tests import factories


class ServiceSettingsListTest(test.APITransactionTestCase):
    def setUp(self):
        self.users = {
            'staff': factories.UserFactory(is_staff=True),
            'owner': factories.UserFactory(),
            'not_owner': factories.UserFactory(),
        }

        self.customers = {
            'owned': factories.CustomerFactory(),
            'inaccessible': factories.CustomerFactory(),
        }

        self.customers['owned'].add_user(self.users['owner'], CustomerRole.OWNER)

        self.settings = {
            'shared': factories.ServiceSettingsFactory(shared=True),
            'inaccessible': factories.ServiceSettingsFactory(
                customer=self.customers['inaccessible']
            ),
            'owned': factories.ServiceSettingsFactory(
                customer=self.customers['owned'], backend_url='bk.url', password='123'
            ),
        }

        # Token is excluded, because it is not available for OpenStack
        self.credentials = ('backend_url', 'username', 'password')

    def test_user_can_see_shared_settings(self):
        self.client.force_authenticate(user=self.users['not_owner'])

        response = self.client.get(factories.ServiceSettingsFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assert_credentials_hidden(response.data[0])
        self.assertEqual(
            response.data[0]['uuid'], self.settings['shared'].uuid.hex, response.data
        )

    def test_user_can_see_shared_and_own_settings(self):
        self.client.force_authenticate(user=self.users['owner'])

        response = self.client.get(factories.ServiceSettingsFactory.get_list_url())
        uuids_received = {d['uuid'] for d in response.data}
        uuids_expected = {self.settings[s].uuid.hex for s in ('shared', 'owned')}
        self.assertEqual(uuids_received, uuids_expected, response.data)

    def test_admin_can_see_all_settings(self):
        self.client.force_authenticate(user=self.users['staff'])

        response = self.client.get(factories.ServiceSettingsFactory.get_list_url())
        uuids_received = {d['uuid'] for d in response.data}
        uuids_expected = {s.uuid.hex for s in self.settings.values()}
        self.assertEqual(uuids_received, uuids_expected, uuids_received)

    def test_user_can_see_credentials_of_own_settings(self):
        self.client.force_authenticate(user=self.users['owner'])

        response = self.client.get(
            factories.ServiceSettingsFactory.get_url(self.settings['owned'])
        )
        self.assert_credentials_visible(response.data)

    def test_user_cant_see_others_settings(self):
        self.client.force_authenticate(user=self.users['not_owner'])

        response = self.client.get(
            factories.ServiceSettingsFactory.get_url(self.settings['owned'])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_can_see_all_credentials(self):
        self.client.force_authenticate(user=self.users['staff'])

        response = self.client.get(
            factories.ServiceSettingsFactory.get_url(self.settings['owned'])
        )
        self.assert_credentials_visible(response.data)

    def test_user_cant_see_shared_credentials(self):
        self.client.force_authenticate(user=self.users['owner'])

        response = self.client.get(
            factories.ServiceSettingsFactory.get_url(self.settings['shared'])
        )
        self.assert_credentials_hidden(response.data)

    def assert_credentials_visible(self, data):
        for field in self.credentials:
            self.assertIn(field, data['options'])

    def assert_credentials_hidden(self, data):
        for field in self.credentials:
            self.assertNotIn(field, data['options'])


class ServiceBackendClassesTest(test.APITransactionTestCase):
    def setUp(self):
        self.service_settings = factories.ServiceSettingsFactory()

    def test_all_required_methods_are_implemented(self):
        for key in [s[0] for s in SupportedServices.get_choices()]:
            klass = SupportedServices.get_service_backend(key)
            try:
                klass(mock.MagicMock())
            except TypeError as e:
                self.fail(str(e))
