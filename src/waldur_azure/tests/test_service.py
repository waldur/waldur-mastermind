import uuid
from unittest import mock

from rest_framework import status, test

from waldur_azure.client import AzureBackendError
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.tests.factories import (
    CustomerFactory,
    ServiceSettingsFactory,
)
from waldur_core.structure.tests.fixtures import ProjectFixture


@mock.patch('waldur_core.structure.models.ServiceSettings.get_backend')
class AzureServiceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = ProjectFixture()
        self.url = ServiceSettingsFactory.get_list_url()
        self.valid_payload = {
            'type': 'Azure',
            'name': 'Azure service',
            'customer': CustomerFactory.get_url(self.fixture.customer),
            'options': {
                'tenant_id': uuid.uuid4().hex,
                'client_id': uuid.uuid4().hex,
                'client_secret': uuid.uuid4().hex,
                'subscription_id': uuid.uuid4().hex,
            },
        }

    def test_when_mandatory_options_are_not_provided_service_is_not_created(
        self, mocked_backend
    ):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {'tenant_id': uuid.uuid4()})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_when_valid_credentials_are_provided_settings_are_created(
        self, mocked_backend
    ):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        service = ServiceSettings.objects.get()
        self.assertEqual(service.options, self.valid_payload['options'])

    def test_credentials_are_validated_against_backend(self, mocked_backend):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mocked_backend().validate_settings.assert_called_once()

    def test_invalid_credentials_are_not_accepted(self, mocked_backend):
        mocked_backend().validate_settings.side_effect = AzureBackendError(
            'Invalid credentials'
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_owner_can_see_secret_fields(self, mocked_backend):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.valid_payload)
        response_options = response.data['options']
        for k, v in self.valid_payload['options'].items():
            self.assertEqual(response_options[k], v)

    def test_manager_can_not_see_secret_fields(self, mocked_backend):
        ServiceSettingsFactory(
            shared=True,
            type='Azure',
            name='Azure service',
            customer=self.fixture.customer,
            options=self.valid_payload['options'],
        )
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(self.url)
        response_options = response.data[0]['options']
        for key in self.valid_payload['options'].keys():
            self.assertFalse(key in response_options)
