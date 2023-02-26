from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.google.tests import factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


@ddt
class GoogleAuthTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        self.credentials = factories.GoogleCredentialsFactory(
            service_provider=self.service_provider,
            calendar_token='calendar_token',
            calendar_refresh_token='calendar_refresh_token',
        )
        self.url = factories.GoogleCredentialsFactory.get_authorize_url(
            self.credentials
        )

    @data('owner', 'staff')
    def test_user_can_authorize(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_user_cannot_sync_bookings_to_calendar(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
