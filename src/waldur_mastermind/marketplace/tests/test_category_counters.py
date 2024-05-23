from rest_framework import status, test

from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


class CategoryCountersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()

    def test_resources(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get("/api/marketplace-global-categories/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {self.fixture.offering.category.uuid.hex: 1})
