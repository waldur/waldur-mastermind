from rest_framework import test, status

from . import factories, fixtures


class InvoiceRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def test_owner_can_retrieve_customer_invoice(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.InvoiceFactory.get_url(self.fixture.invoice))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_cannot_retrieve_customer_invoice(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(factories.InvoiceFactory.get_url(self.fixture.invoice))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
