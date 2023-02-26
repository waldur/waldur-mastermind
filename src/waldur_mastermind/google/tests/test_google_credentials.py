from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.google.tests import factories as google_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


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
        self.url = google_factories.GoogleCredentialsFactory.get_url(google_credentials)

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
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
