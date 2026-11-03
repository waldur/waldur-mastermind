from rest_framework import status, test

from waldur_mastermind.common.enums import Units
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.invoices.tests import fixtures as invoice_fixtures


class TotalCostTest(test.APITestCase):
    def setUp(self):
        self.fixture1 = invoice_fixtures.InvoiceFixture()
        self.fixture2 = invoice_fixtures.InvoiceFixture()

        invoice_factories.InvoiceItemFactory(
            invoice=self.fixture1.invoice,
            project=self.fixture1.project,
            unit=Units.QUANTITY,
            unit_price=10,
            quantity=10,
        )
        invoice_factories.InvoiceItemFactory(
            invoice=self.fixture2.invoice,
            project=self.fixture2.project,
            unit=Units.QUANTITY,
            unit_price=20,
            quantity=5,
        )

    def test_total_cost(self):
        self.assert_cost(200)

    def test_filter_by_period(self):
        invoice = self.fixture1.invoice
        invoice.year = 2017
        invoice.month = 9
        invoice.save()
        self.assert_cost(100)

    def assert_cost(self, value):
        self.client.force_authenticate(self.fixture1.staff)
        response = self.client.get("/api/billing-total-cost/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(value, response.data["total"])

    def test_support_may_read_total_cost(self):
        self.client.force_authenticate(self.fixture1.global_support)
        response = self.client.get("/api/billing-total-cost/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(200, response.data["total"])

    def test_owner_may_not_read_total_cost(self):
        self.client.force_authenticate(self.fixture1.owner)
        response = self.client.get("/api/billing-total-cost/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_may_not_read_total_cost(self):
        response = self.client.get("/api/billing-total-cost/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
