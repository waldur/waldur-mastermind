import uuid
from unittest import mock

from rest_framework import status, test

from waldur_azure.client import AzureBackendError
from waldur_core.structure.tests.factories import CustomerFactory
from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


@mock.patch('waldur_core.structure.models.ServiceSettings.get_backend')
class AzureServiceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = ProjectFixture()
        self.url = factories.AzureServiceFactory.get_list_url()

    def get_valid_payload(self):
        return {
            'name': 'Azure service',
            'customer': CustomerFactory.get_url(self.fixture.customer),
            'tenant_id': uuid.uuid4(),
            'client_id': uuid.uuid4(),
            'client_secret': uuid.uuid4(),
            'subscription_id': uuid.uuid4(),
        }

    def test_azure_credentials_are_validated(self, mocked_backend):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mocked_backend().ping.assert_called_once()

    def test_invalid_credentials_are_not_accepted(self, mocked_backend):
        mocked_backend().ping.side_effect = AzureBackendError('Invalid credentials')
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
