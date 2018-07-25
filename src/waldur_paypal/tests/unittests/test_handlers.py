import datetime
import mock

from django.test import TestCase
from django.utils import timezone

from waldur_paypal import handlers, models

from .. import fixtures


class CreateInvoiceTest(TestCase):

    def setUp(self):
        self.fixture = fixtures.PayPalFixture()
        self.issuer_details = {
            'email': "example@domain.com",
            'first_name': "John",
            'last_name': "White",
            'business_name': "Corporation Professionals, LLC",
            'phone': {
                'country_code': '001',
                'national_number': '5032141716',
            },
            'address': {
                "line1": "1234 Main St.",
                "city": "Portland",
                "state": "OR",
                "postal_code": "97217",
                "country_code": "US"
            }
        }

    def _get_valid_invoice(self):
        invoice = mock.Mock()
        invoice.customer = self.fixture.customer
        invoice.year = 2017
        invoice.month = 1
        invoice.tax_percent = 15

        # when invoice is moved to created state
        invoice.invoice_date = timezone.now().date()
        invoice.due_date = invoice.invoice_date + datetime.timedelta(days=30)
        return invoice

    def _get_payment_details(self):
        payment_details = mock.Mock()
        payment_details.company = 'Company'
        payment_details.address = 'Address #1'
        payment_details.country = 'Wonderland'
        payment_details.email = 'test@example.com'
        payment_details.postal = '10110'
        payment_details.phone = '+372-555-55-55'
        payment_details.bank = 'Harry Potter Bank #2.5'
        return payment_details

    def _generate_invoice_items(self, count=1):
        items = []

        for i in range(0, count):
            item = mock.Mock()
            item.unit = 'day'
            item.unit_price = 10
            item.usage_days = 15
            item.price = item.unit_price * item.usage_days
            item.tax = 15
            item.start = timezone.now()
            item.end = item.start + timezone.timedelta(days=item.usage_days)
            item.name = 'invoice item #%s' % i
            items.append(item)

        return items

    def test_invoice_is_created(self):
        invoice = self._get_valid_invoice()
        invoice.items = self._generate_invoice_items(2)
        self.assertEqual(models.Invoice.objects.count(), 0)

        handlers.create_invoice(sender=None, invoice=invoice, issuer_details=self.issuer_details)

        self.assertEqual(models.Invoice.objects.count(), 1)
        new_invoice = models.Invoice.objects.get(invoice_date=invoice.invoice_date, customer=invoice.customer)
        self.assertEqual(new_invoice.year, invoice.year)
        self.assertEqual(new_invoice.month, invoice.month)
        self.assertEqual(new_invoice.items.count(), 2)

        for original_item in invoice.items:
            created_item = [item for item in new_invoice.items.iterator() if item.name == original_item.name][0]
            self.assertEqual(created_item.unit_price, original_item.unit_price)
            self.assertEqual(created_item.price, original_item.price)
            self.assertEqual(created_item.start, original_item.start)
            self.assertEqual(created_item.end, original_item.end)
            self.assertEqual(created_item.tax, original_item.tax)
            self.assertEqual(created_item.unit_of_measure, models.InvoiceItem.UnitsOfMeasure.AMOUNT)
            self.assertEqual(created_item.quantity, original_item.usage_days)
