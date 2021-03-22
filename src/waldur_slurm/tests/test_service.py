from unittest import mock

from rest_framework import status, test

from waldur_core.structure.exceptions import ServiceBackendError
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.tests.factories import (
    CustomerFactory,
    ServiceSettingsFactory,
)
from waldur_core.structure.tests.fixtures import ProjectFixture


@mock.patch('waldur_core.structure.models.ServiceSettings.get_backend')
class SlurmServiceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = ProjectFixture()
        self.url = ServiceSettingsFactory.get_list_url()

    def get_valid_payload(self):
        return {
            'type': 'SLURM',
            'name': 'SLURM service',
            'customer': CustomerFactory.get_url(self.fixture.customer),
            'options': {
                'username': 'waldur_root',
                'hostname': 'slurm.waldur.com',
                'default_account': 'waldur_user',
                'port': 22,
                'use_sudo': True,
                'gateway': '8.8.8.8',
            },
        }

    def test_when_mandatory_options_are_not_provided_service_is_not_created(
        self, mocked_backend
    ):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {'default_account': 'waldur_user'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_when_valid_credentials_are_provided_settings_are_created(
        self, mocked_backend
    ):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        service = ServiceSettings.objects.get()
        self.assertEqual(service.username, 'waldur_root')
        self.assertEqual(
            service.options,
            {
                'hostname': 'slurm.waldur.com',
                'default_account': 'waldur_user',
                'port': 22,
                'use_sudo': True,
                'gateway': '8.8.8.8',
            },
        )

    def test_credentials_are_validated_against_backend(self, mocked_backend):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mocked_backend().validate_settings.assert_called_once()

    def test_invalid_credentials_are_not_accepted(self, mocked_backend):
        mocked_backend().validate_settings.side_effect = ServiceBackendError(
            'Invalid credentials'
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
