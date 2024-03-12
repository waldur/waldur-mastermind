from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import fixtures as structure_fixtures


class TestCustomerQuotas(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer

    def test_quotas(self):
        self.client.force_login(self.fixture.staff)
        self.customer.set_quota_usage("nc_resource_count", 10)
        response = self.client.get(
            reverse("customer-quotas-list"), {"quota_name": "nc_resource_count"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[-1]["value"], 10)
        self.assertEqual(response.data[-1]["customer_name"], self.customer.name)
