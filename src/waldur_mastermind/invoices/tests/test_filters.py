from ddt import ddt
from rest_framework import test

from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


@ddt
class InvoiceFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.fixture.invoice_item

    def test_filter_sum(self):
        resource_1 = marketplace_factories.ResourceFactory()
        resource_2 = marketplace_factories.ResourceFactory()
        resource_3 = marketplace_factories.ResourceFactory()
        factories.InvoiceItemFactory(
            name="OFFERING-002",
            project=self.fixture.project,
            invoice=self.fixture.invoice,
            unit_price=10,
            resource=resource_1,
            quantity=1,
        )
        factories.InvoiceItemFactory(
            name="OFFERING-003",
            project=self.fixture.project,
            invoice=self.fixture.invoice,
            unit_price=10,
            resource=resource_2,
            quantity=1,
        )

        factories.InvoiceFactory()
        invoice = factories.InvoiceFactory()
        factories.InvoiceItemFactory(
            name="OFFERING-004",
            invoice=invoice,
            unit_price=100,
            resource=resource_3,
            quantity=100,
        )

        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = factories.InvoiceFactory.get_list_url()

        response = self.client.get(url)
        self.assertEqual(len(response.data), 3)

        response = self.client.get(url, {"min_sum": 100, "max_sum": 500})
        self.assertEqual(len(response.data), 1)


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
