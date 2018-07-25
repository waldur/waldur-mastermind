from rest_framework import status, test
from six.moves import mock

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_core.structure.tests import factories as structure_factories

from . import factories
from ..backend import RijkscloudBackendError


class ServiceCreateTest(test.APITransactionTestCase):
    def create_service(self):
        url = factories.ServiceFactory.get_list_url()
        fixture = ProjectFixture()
        self.client.force_login(fixture.owner)
        return self.client.post(url, {
            'customer': structure_factories.CustomerFactory.get_url(fixture.customer),
            'username': 'admin',
            'token': 'secret',
            'name': 'Test Service',
        })

    @mock.patch('waldur_rijkscloud.backend.RijkscloudClient')
    def test_credentials_are_passed_to_client(self, mocked_client):
        self.create_service()
        mocked_client.assert_called_with(userid='admin', apikey='secret')

    @mock.patch('waldur_rijkscloud.backend.RijkscloudBackend')
    def test_if_ping_fails_service_is_not_created(self, mocked_backend):
        mocked_backend().ping.return_value = RijkscloudBackendError('Invalid credentials')
        response = self.create_service()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
