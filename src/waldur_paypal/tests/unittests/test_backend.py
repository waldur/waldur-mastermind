from django.test import TestCase
import mock

from waldur_paypal.backend import PaypalBackend, PayPalError

from .. import factories, fixtures


class BaseBackendTest(TestCase):

    def setUp(self):
        self.fixture = fixtures.PayPalFixture()
        self.backend = PaypalBackend()
        self.invoice = self.fixture.invoice


class CreateInvoiceTest(BaseBackendTest):

    def setUp(self):
        super(CreateInvoiceTest, self).setUp()
        self.invoice.backend_id = ''
        self.invoice.save()

    @mock.patch('waldur_paypal.backend.paypal.Invoice')
    def test_invoice_is_created(self, invoice_mock):
        self.invoice.items.add(factories.InvoiceItemFactory())
        backend_invoice = mock.Mock()
        backend_invoice.id = 'INV2-ETBW-Q5NB-VWLT-9RH1'
        backend_invoice.status = 'DRAFT'
        backend_invoice.number = '00001'
        invoice_mock.return_value = backend_invoice

        result = self.backend.create_invoice(self.invoice)

        self.assertEqual(result.backend_id, backend_invoice.id)
        self.assertEqual(result.state, backend_invoice.status)
        backend_invoice.create.assert_called_once()

    def test_invoice_is_not_created_if_phone_is_not_provided(self):
        self.invoice.items.add(factories.InvoiceItemFactory())
        del self.invoice.issuer_details['phone']

        self.assertRaises(PayPalError, self.backend.create_invoice, self.invoice)

    def test_invoice_is_not_created_if_phone_country_code_is_not_provided(self):
        self.invoice.items.add(factories.InvoiceItemFactory())
        del self.invoice.issuer_details['phone']['country_code']

        self.assertRaises(PayPalError, self.backend.create_invoice, self.invoice)

    def test_invoice_is_not_created_if_phone_national_number_is_not_provided(self):
        self.invoice.items.add(factories.InvoiceItemFactory())
        del self.invoice.issuer_details['phone']['national_number']

        self.assertRaises(PayPalError, self.backend.create_invoice, self.invoice)


class DownloadInvoicePDFTest(BaseBackendTest):

    def setUp(self):
        super(DownloadInvoicePDFTest, self).setUp()

    def test_pdf_is_not_downloaded_if_backend_id_is_missing(self):
        self.invoice.backend_id = ''
        self.invoice.save()

        self.assertRaises(PayPalError, self.backend.download_invoice_pdf, self.invoice)

    @mock.patch('waldur_paypal.backend.urllib2.urlopen')
    def test_pdf_is_downloaded(self, urlopen_mock):
        response = mock.Mock()
        response.read.return_value = 'PDF file content'
        urlopen_mock.return_value = response
        self.assertFalse(self.invoice.pdf)

        self.backend.download_invoice_pdf(self.invoice)

        self.assertTrue(self.invoice.pdf)
        urlopen_mock.assert_called_once()


class SendInvoiceTest(BaseBackendTest):

    @mock.patch('waldur_paypal.backend.paypal.Invoice')
    def test_draft_invoice_is_sent(self, invoice_mock):
        self.invoice.items.add(factories.InvoiceItemFactory())
        backend_invoice = mock.Mock(name='backend_invoice')
        invoice_mock.find.return_value = backend_invoice

        self.backend.send_invoice(self.invoice)

        backend_invoice.send.assert_called_once()

    def test_invoice_cannot_be_sent_if_it_is_not_in_draft_state(self):
        self.invoice.state = self.invoice.States.SENT
        self.invoice.save()

        self.assertRaises(PayPalError, self.backend.send_invoice, self.invoice)
