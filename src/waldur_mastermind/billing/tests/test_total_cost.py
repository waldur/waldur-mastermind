from rest_framework import status, test

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.invoices.tests import fixtures as invoice_fixtures


class TotalCostTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture1 = invoice_fixtures.InvoiceFixture()
        self.fixture2 = invoice_fixtures.InvoiceFixture()

        invoice_factories.InvoiceItemFactory(
            invoice=self.fixture1.invoice,
            project=self.fixture1.project,
            unit=invoice_models.InvoiceItem.Units.QUANTITY,
            unit_price=10,
            quantity=10,
        )
        invoice_factories.InvoiceItemFactory(
            invoice=self.fixture2.invoice,
            project=self.fixture2.project,
            unit=invoice_models.InvoiceItem.Units.QUANTITY,
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
