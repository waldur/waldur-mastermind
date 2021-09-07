from rest_framework import status, test

from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class InvoiceItemCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.resource = marketplace_factories.ResourceFactory()
        self.payload = {
            'invoice': factories.InvoiceFactory.get_url(self.fixture.invoice),
            'name': 'First invoice item',
            'quantity': 10,
            'unit_price': 7,
            'resource': marketplace_factories.ResourceFactory.get_url(self.resource),
        }

    def test_staff_can_create_invoice_item(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.InvoiceItemFactory.get_list_url(), self.payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(1, self.fixture.invoice.items.count())
        self.assertEqual(self.resource, self.fixture.invoice.items.get().resource)

    def test_non_staff_can_not_create_invoice_item(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(
            factories.InvoiceItemFactory.get_list_url(), self.payload
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class InvoiceDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def test_staff_can_delete_invoice_item(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(
            factories.InvoiceItemFactory.get_url(self.fixture.invoice_item),
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_non_staff_can_not_delete_invoice_item(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.delete(
            factories.InvoiceItemFactory.get_url(self.fixture.invoice_item),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class InvoiceUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def test_staff_can_update_invoice_item(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.InvoiceItemFactory.get_url(self.fixture.invoice_item),
            {'name': 'Updated name'},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.invoice_item.refresh_from_db()
        self.assertEqual('Updated name', self.fixture.invoice_item.name)

    def test_non_staff_can_not_update_invoice_item(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.patch(
            factories.InvoiceItemFactory.get_url(self.fixture.invoice_item),
            {'name': 'Updated name'},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
