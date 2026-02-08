from rest_framework import status, test

from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests import factories, fixtures


class ResourceOrderFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_list_url()

    def test_filter_order_state_executing(self):
        # Create an order in progress for the resource
        factories.OrderFactory(
            resource=self.resource,
            state=OrderStates.EXECUTING,
            project=self.fixture.project,
            offering=self.resource.offering,
        )

        # Create another resource with a different order state
        other_resource = factories.ResourceFactory(project=self.fixture.project)
        factories.OrderFactory(
            resource=other_resource,
            state=OrderStates.PENDING_CONSUMER,
            project=self.fixture.project,
            offering=other_resource.offering,
        )

        self.client.force_authenticate(self.fixture.staff)

        # Filter by executing
        response = self.client.get(self.url, {"order_state": "executing"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.resource.uuid.hex)

    def test_filter_order_state_done(self):
        # Create a DONE order for the resource
        factories.OrderFactory(
            resource=self.resource,
            state=OrderStates.DONE,
            project=self.fixture.project,
            offering=self.resource.offering,
        )

        self.client.force_authenticate(self.fixture.staff)

        # Filter by done
        response = self.client.get(self.url, {"order_state": "done"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.resource.uuid.hex)
