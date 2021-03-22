from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure import models
from waldur_core.structure.tests import factories, fixtures


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

        self.customers['owned'].add_user(self.users['owner'], models.CustomerRole.OWNER)

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


@ddt
class ServiceSettingsUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.service_settings = self.fixture.service_settings
        self.url = factories.ServiceSettingsFactory.get_url(self.service_settings)

    @data('staff', 'owner')
    def test_staff_and_owner_can_update_service_settings(self, user):
        self.assert_user_can_update_service_settings(user)

    @data('admin', 'manager')
    def test_admin_and_manager_can_not_update_service_settings(self, user):
        self.assert_user_can_not_update_service_settings(user)

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_only_staff_manages_services_he_can_update_it(self):
        self.assert_user_can_update_service_settings('staff')

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_only_staff_manages_services_owner_can_not_update_it(self):
        self.assert_user_can_not_update_service_settings('owner')

    def assert_user_can_update_service_settings(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.update_service_settings()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.service_settings.refresh_from_db()
        self.assertEqual(self.service_settings.name, 'Valid new name')

    def assert_user_can_not_update_service_settings(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.update_service_settings()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.service_settings.refresh_from_db()
        self.assertNotEqual(self.service_settings.name, 'Valid new name')

    def test_service_setting_updating_is_not_available_for_blocked_organization(self):
        customer = self.fixture.customer
        customer.blocked = True
        customer.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.update_service_settings()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def update_service_settings(self):
        return self.client.patch(self.url, {'name': 'Valid new name'})


@ddt
class SharedServiceSettingUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.service_settings = self.fixture.service_settings
        self.service_settings.shared = True
        self.service_settings.save()
        self.url = factories.ServiceSettingsFactory.get_url(self.service_settings)

    def get_valid_payload(self):
        return {'name': 'test'}

    @data('staff', 'owner')
    def test_only_staff_and_an_owner_of_an_private_service_settings_can_update_the_settings(
        self, user
    ):
        self.service_settings.shared = False
        self.service_settings.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self.get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.service_settings.refresh_from_db()
        self.assertEqual(self.service_settings.name, payload['name'])

    @data('owner', 'manager', 'admin')
    def test_user_cannot_update_shared_service_settings_without_customer_if_he_has_no_permission(
        self, user
    ):
        self.service_settings.customer = None
        self.service_settings.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self.get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.service_settings.refresh_from_db()
        self.assertNotEqual(self.service_settings.name, payload['name'])

    @data('staff')
    def test_user_can_update_shared_service_settings_without_customer_if_he_has_permission(
        self, user
    ):
        self.service_settings.customer = None
        self.service_settings.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self.get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.service_settings.refresh_from_db()
        self.assertEqual(self.service_settings.name, payload['name'])

    @data('manager', 'admin')
    def test_user_cannot_update_shared_service_settings_with_customer_if_he_has_no_permission(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self.get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.service_settings.refresh_from_db()
        self.assertNotEqual(self.service_settings.name, payload['name'])

    def test_user_cannot_change_private_settings_type(self):
        self.service_settings.shared = False
        self.service_settings.save()
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {'name': 'Test backend', 'type': 2}

        response = self.client.patch(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.service_settings.refresh_from_db()
        self.assertNotEqual(self.service_settings.type, payload['type'], response.data)

    def test_user_can_change_private_settings_secret_options(self):
        self.service_settings.shared = False
        self.service_settings.save()
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {
            'username': 'admin',
            'password': 'secret',
            'backend_url': 'http://example.com',
        }

        response = self.client.patch(self.url, data={'options': payload})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.service_settings.refresh_from_db()
        self.assertEqual(
            self.service_settings.password,
            payload['password'],
            response.data['options'],
        )

    def test_user_cannot_change_shared_settings_secret_options(self):
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {
            'username': 'admin',
            'password': 'secret',
            'backend_url': 'http://example.com',
        }

        response = self.client.patch(self.url, data={'options': payload})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_private_service_setting_updating_is_not_available_for_blocked_organization(
        self,
    ):
        self.service_settings.shared = False
        self.service_settings.save()
        customer = self.fixture.customer
        customer.blocked = True
        customer.save()
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        response = self.client.patch(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
