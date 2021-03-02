from unittest import mock

from django.test import TestCase

from waldur_mastermind.invoices import utils
from waldur_mastermind.invoices.tests import fixtures


class TestPdfCreation(TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.InvoiceFixture()
        self.invoice = self.fixture.invoice
        self.patcher = mock.patch('waldur_mastermind.invoices.utils.pdfkit')
        self.content = b'pdf content'
        mock_pdfkit = self.patcher.start()
        mock_pdfkit.from_string.return_value = self.content

    def tearDown(self):
        super(TestPdfCreation, self).tearDown()
        mock.patch.stopall()

    def test_create_pdf(self):
        pdf = utils.create_invoice_pdf(self.invoice)
        self.assertEqual(self.content, pdf)
