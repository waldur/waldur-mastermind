from ddt import ddt
from rest_framework import status, test

from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


@ddt
class InvoiceFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.fixture.invoice_item

    def test_filter_order_items_by_marketplace_resource_uuid(self):
        resource_1 = marketplace_factories.ResourceFactory()
        resource_2 = marketplace_factories.ResourceFactory()
        factories.InvoiceItemFactory(
            name='OFFERING-002',
            project=self.fixture.project,
            invoice=self.fixture.invoice,
            unit_price=10,
            resource=resource_1,
        )
        factories.InvoiceItemFactory(
            name='OFFERING-003',
            project=self.fixture.project,
            invoice=self.fixture.invoice,
            unit_price=10,
            resource=resource_2,
        )
        user = self.fixture.staff
        self.client.force_authenticate(user)

        response = self.client.get(
            factories.InvoiceFactory.get_url(self.fixture.invoice)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['items']), 3)

        response = self.client.get(factories.InvoiceFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data[0]['items']), 3)

        response = self.client.get(
            factories.InvoiceFactory.get_url(self.fixture.invoice),
            {'resource_uuid': resource_1.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['items']), 1)

        response = self.client.get(
            factories.InvoiceFactory.get_list_url(),
            {'resource_uuid': resource_1.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data[0]['items']), 1)

    def test_validate_marketplace_resource_uuid(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        response = self.client.get(
            factories.InvoiceFactory.get_list_url(), {'resource_uuid': 'INVALID'}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
