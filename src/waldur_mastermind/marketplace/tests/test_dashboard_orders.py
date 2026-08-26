from rest_framework import status, test

from waldur_core.user_actions.providers import DASHBOARD_LIST_LIMIT
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests import factories, fixtures


class DashboardMyOrdersTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = factories.OrderFactory.get_list_url(action="dashboard-my-orders")
        self.user = self.fixture.user

    def _create_order(self, state, user=None):
        return factories.OrderFactory(
            project=self.fixture.project,
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            created_by=user or self.user,
            state=state,
            attributes={"name": "my-resource"},
        )

    def test_anonymous_user_cannot_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_only_own_pending_orders(self):
        pending = self._create_order(OrderStates.PENDING_CONSUMER)
        executing = self._create_order(OrderStates.EXECUTING)
        self._create_order(OrderStates.DONE)
        self._create_order(OrderStates.CANCELED)
        # another user's pending order must not leak in
        self._create_order(OrderStates.PENDING_CONSUMER, user=self.fixture.owner)

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertEqual(uuids, {pending.uuid.hex, executing.uuid.hex})

    def test_visible_without_project_or_customer_permissions(self):
        # fixture.user holds no role in the project or customer, but still
        # sees orders they created themselves.
        order = self._create_order(OrderStates.PENDING_PROVIDER)
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], order.uuid.hex)

    def test_response_shape(self):
        order = self._create_order(OrderStates.PENDING_CONSUMER)
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        row = response.data[0]
        self.assertEqual(
            set(row.keys()),
            {
                "uuid",
                "offering_uuid",
                "offering_name",
                "resource_uuid",
                "resource_name",
                "project_uuid",
                "project_name",
                "customer_uuid",
                "customer_name",
                "state",
                "type",
                "created",
            },
        )
        self.assertEqual(row["offering_name"], self.fixture.offering.name)
        self.assertEqual(row["project_name"], self.fixture.project.name)
        self.assertEqual(row["customer_name"], self.fixture.customer.name)
        self.assertEqual(row["resource_name"], order.resource.name)
        # Both fields use the same machine-readable convention; the frontend
        # maps them to translated labels.
        self.assertEqual(row["state"], "pending-consumer")
        self.assertEqual(row["type"], "create")

    def test_caps_number_of_returned_orders(self):
        # Hit on every dashboard load, so a page is bounded.
        for _ in range(DASHBOARD_LIST_LIMIT + 3):
            self._create_order(OrderStates.PENDING_CONSUMER)

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), DASHBOARD_LIST_LIMIT)

    def test_reports_the_true_total_and_serves_further_pages(self):
        # The page is capped, so without the count a client renders the first
        # ten as if they were all of them.
        total = DASHBOARD_LIST_LIMIT + 3
        for _ in range(total):
            self._create_order(OrderStates.PENDING_CONSUMER)

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(int(response["X-Result-Count"]), total)

        second_page = self.client.get(self.url, {"page": 2})
        self.assertEqual(len(second_page.data), total - DASHBOARD_LIST_LIMIT)
        # Still a bare array, so the response shape the SDK sees is unchanged.
        self.assertIsInstance(second_page.data, list)

    def test_head_returns_the_count_without_a_body(self):
        # The `_count` companion: paginated, so the HEAD operation is real and
        # the SDK method it generates is worth having.
        total = DASHBOARD_LIST_LIMIT + 3
        for _ in range(total):
            self._create_order(OrderStates.PENDING_CONSUMER)

        self.client.force_authenticate(self.user)
        response = self.client.head(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response["X-Result-Count"]), total)

    def test_orders_are_newest_first(self):
        first = self._create_order(OrderStates.PENDING_CONSUMER)
        second = self._create_order(OrderStates.PENDING_CONSUMER)
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(
            [row["uuid"] for row in response.data],
            [second.uuid.hex, first.uuid.hex],
        )
