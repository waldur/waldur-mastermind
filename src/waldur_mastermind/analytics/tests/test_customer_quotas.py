from ddt import data, ddt
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests.factories import UserFactory


@ddt
class TestCustomerQuotas(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.customer.set_quota_usage("nc_resource_count", 10)
        self.user = UserFactory()

    @data("staff", "global_support", "owner")
    def test_connected_users_can_access_quotas(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.get(
            reverse("customer-quotas-list"), {"quota_name": "nc_resource_count"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[-1]["value"], 10)
        self.assertEqual(response.data[-1]["customer_name"], self.customer.name)

    def test_user_can_not_access_quotas_of_other_customer(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("customer-quotas-list"), {"quota_name": "nc_resource_count"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_filter_by_customer_uuid(self):
        other_customer = structure_fixtures.CustomerFixture().customer
        self.client.force_login(self.fixture.staff)
        response = self.client.get(
            reverse("customer-quotas-list"),
            {
                "quota_name": "nc_resource_count",
                "customer_uuid": other_customer.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["customer_name"] for row in response.data], [other_customer.name]
        )

    def test_filter_by_customer_uuid_is_limited_to_visible_customers(self):
        other_customer = structure_fixtures.CustomerFixture().customer
        self.client.force_login(self.fixture.owner)
        response = self.client.get(
            reverse("customer-quotas-list"),
            {
                "quota_name": "nc_resource_count",
                "customer_uuid": other_customer.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_invalid_customer_uuid(self):
        self.client.force_login(self.fixture.staff)
        response = self.client.get(
            reverse("customer-quotas-list"),
            {"quota_name": "nc_resource_count", "customer_uuid": "invalid"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
