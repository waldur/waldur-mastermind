from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.google.tests import factories as google_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from . import factories


@ddt
class GoogleCredentialsGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        google_credentials = google_factories.GoogleCredentialsFactory(
            service_provider=service_provider
        )
        self.url = factories.GoogleCredentialsFactory.get_url(google_credentials)

    @data('staff', 'owner')
    def test_user_can_get_google_credentials(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_user_can_not_get_google_credentials(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class GoogleCredentialsCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        google_credentials = google_factories.GoogleCredentialsFactory(
            service_provider=self.service_provider
        )
        self.url = factories.GoogleCredentialsFactory.get_url(google_credentials)

    @data('staff', 'owner')
    def test_user_can_set_google_credentials(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(
            self.url,
            {
                'client_id': 'client_id-1',
                'project_id': 'project_id-1',
                'client_secret': 'client_secret-1',
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.service_provider.refresh_from_db()
        self.assertTrue(hasattr(self.service_provider, 'googlecredentials'))

    @data('user')
    def test_user_can_not_set_google_credentials(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(
            self.url,
            {
                'client_id': 'client_id-1',
                'project_id': 'project_id-1',
                'client_secret': 'client_secret-1',
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
