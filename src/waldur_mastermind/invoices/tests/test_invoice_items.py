from rest_framework import status, test

from waldur_mastermind.invoices.tests import factories, fixtures


class InvoiceItemDeleteTest(test.APITransactionTestCase):
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


class InvoiceItemUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def update_invoice_item(self, user):
        self.client.force_authenticate(user)
        return self.client.patch(
            factories.InvoiceItemFactory.get_url(self.fixture.invoice_item),
            {'article_code': 'AA11'},
        )

    def test_staff_can_update_invoice_item(self):
        response = self.update_invoice_item(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.invoice_item.refresh_from_db()
        self.assertEqual('AA11', self.fixture.invoice_item.article_code)

    def test_non_staff_can_not_update_invoice_item(self):
        response = self.update_invoice_item(self.fixture.user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class InvoiceItemCompensationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.item = self.fixture.invoice_item

    def create_compensation(self, user, offering_component_name='Compensation'):
        self.client.force_authenticate(user)
        url = factories.InvoiceItemFactory.get_url(self.item, 'create_compensation')
        return self.client.post(
            url, {'offering_component_name': offering_component_name}
        )

    def test_staff_can_create_compensation(self):
        response = self.create_compensation(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_new_invoice_item_has_valid_details(self):
        self.create_compensation(self.fixture.staff)
        new_invoice_item = self.fixture.invoice.items.last()
        self.assertEqual(
            str(new_invoice_item.details['original_invoice_item_uuid']),
            str(self.item.uuid),
        )
        self.assertEqual(
            new_invoice_item.details['offering_component_name'], 'Compensation'
        )

    def test_compensation_for_invoice_item_with_negative_price_is_invalid(self):
        self.item.unit_price *= -1
        self.item.save()
        response = self.create_compensation(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_staff_can_not_create_compensation(self):
        response = self.create_compensation(self.fixture.user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
