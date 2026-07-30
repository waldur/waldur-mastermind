from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
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


@ddt
class ResourceOrderingTest(test.APITestCase):
    """Ordering fields exposed by ResourceFilter.o."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = factories.ResourceFactory.get_list_url()

        # Two resources whose ordering keys are deliberately inverted relative to
        # each other, so an unsupported ordering field cannot accidentally pass.
        self.first = factories.ResourceFactory(
            project=structure_factories.ProjectFactory(
                customer=structure_factories.CustomerFactory(name="A organization")
            ),
            offering=factories.OfferingFactory(name="A offering"),
            backend_id="aaa-backend-id",
        )
        self.first.plan = factories.PlanFactory(
            offering=self.first.offering, name="A plan"
        )
        self.first.save()

        self.last = factories.ResourceFactory(
            project=structure_factories.ProjectFactory(
                customer=structure_factories.CustomerFactory(name="Z organization")
            ),
            offering=factories.OfferingFactory(name="Z offering"),
            backend_id="zzz-backend-id",
        )
        self.last.plan = factories.PlanFactory(
            offering=self.last.offering, name="Z plan"
        )
        self.last.save()

        self.client.force_authenticate(self.fixture.staff)

    def get_uuids(self, ordering):
        response = self.client.get(self.url, {"o": ordering})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [item["uuid"] for item in response.data]

    @data("customer_name", "offering_name", "plan_name", "backend_id")
    def test_ordering_is_ascending(self, ordering):
        uuids = self.get_uuids(ordering)
        self.assertLess(
            uuids.index(self.first.uuid.hex), uuids.index(self.last.uuid.hex)
        )

    @data("customer_name", "offering_name", "plan_name", "backend_id")
    def test_ordering_is_reversible(self, ordering):
        uuids = self.get_uuids(f"-{ordering}")
        self.assertGreater(
            uuids.index(self.first.uuid.hex), uuids.index(self.last.uuid.hex)
        )
