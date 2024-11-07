from rest_framework import status, test

from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


class CategoryCountersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.client.force_authenticate(self.fixture.staff)

    def test_resources(self):
        response = self.client.get("/api/marketplace-global-categories/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {self.fixture.offering.category.uuid.hex: 1})

    def test_filter(self):
        new_resource = factories.ResourceFactory()
        new_resource.offering.category = self.fixture.offering.category
        new_resource.offering.save()
        response = self.client.get("/api/marketplace-global-categories/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {self.fixture.offering.category.uuid.hex: 2})

        response = self.client.get(
            "/api/marketplace-global-categories/",
            {"project_uuid": new_resource.project.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {self.fixture.offering.category.uuid.hex: 1})

        response = self.client.get(
            "/api/marketplace-global-categories/",
            {"customer_uuid": new_resource.project.customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {self.fixture.offering.category.uuid.hex: 1})
