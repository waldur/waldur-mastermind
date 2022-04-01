from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


@ddt
class RelatedCustomersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = f'/api/marketplace-related-customers/{self.resource.offering.customer.uuid.hex}/'

    def get_response(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.get(self.url)

    @data('service_manager', 'offering_owner')
    def test_consumer_customer_is_visible_to_service_manager_and_owner(self, user):
        response = self.get_response(user)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]['uuid'], self.resource.project.customer.uuid.hex
        )

    @data('admin', 'manager', 'member')
    def test_consumer_customer_is_not_visible_to_project_users(self, user):
        response = self.get_response(user)
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)
