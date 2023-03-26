from unittest.mock import patch

from django.core.exceptions import ValidationError
from rest_framework import status, test

from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.tests import factories, fixtures


@patch('waldur_core.structure.models.ServiceSettings.get_backend')
class OpenStackServiceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.CustomerFixture()
        self.url = factories.ServiceSettingsFactory.get_list_url()

    def test_user_can_add_service_to_the_customer_he_owns(self, mocked_backend):
        mocked_backend().check_admin_tenant.return_value = True
        self.client.force_authenticate(user=self.fixture.owner)

        payload = self.get_payload()

        with patch(
            'waldur_core.structure.executors.ServiceSettingsCreateExecutor.execute'
        ) as mocked:
            response = self.client.post(self.url, payload)
            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, response.data
            )

            settings = ServiceSettings.objects.get(name=payload['name'])
            self.assertFalse(settings.shared)

            mocked.assert_any_call(settings)
            mocked_backend().validate_settings.assert_called_once()

    def test_admin_service_credentials_are_validated(self, mocked_backend):
        mocked_backend().validate_settings.side_effect = ValidationError(
            'Provided credentials are not for admin tenant.'
        )
        self.client.force_authenticate(user=self.fixture.owner)

        payload = self.get_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data['non_field_errors'],
            ['Provided credentials are not for admin tenant.'],
        )

    def get_payload(self):
        return {
            'name': 'service_settings name',
            'customer': factories.CustomerFactory.get_url(self.fixture.customer),
            'type': 'OpenStack',
            'options': {
                'backend_url': 'http://example.com',
                'username': 'user',
                'password': 'secret',
                'tenant_name': 'admin',
            },
        }
