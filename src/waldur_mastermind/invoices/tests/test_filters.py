from rest_framework import test

from waldur_mastermind.invoices.tests import factories, fixtures


class InvoiceItemFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.invoice_item = self.fixture.invoice_item
        self.customer_credit = self.fixture.customer_credit
        self.invoice_item.credit = self.customer_credit
        self.invoice_item.save()
        factories.InvoiceItemFactory()

    def test_filters(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = factories.InvoiceItemFactory.get_list_url()

        response = self.client.get(url)
        self.assertEqual(len(response.data), 2)

        response = self.client.get(url, {"project_uuid": self.fixture.project.uuid.hex})
        self.assertEqual(len(response.data), 1)

        response = self.client.get(
            url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)

        response = self.client.get(
            url, {"credit_uuid": self.fixture.customer_credit.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)
