from unittest import mock

from django.test import TestCase

from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace.tests import fixtures


class TestPdfCreation(TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.order = self.fixture.order
        self.patcher = mock.patch('waldur_mastermind.marketplace.utils.pdfkit')
        self.content = b'pdf content'
        mock_pdfkit = self.patcher.start()
        mock_pdfkit.from_string.return_value = self.content

    def tearDown(self):
        super(TestPdfCreation, self).tearDown()
        mock.patch.stopall()

    def test_create_pdf(self):
        pdf = utils.create_order_pdf(self.order)
        self.assertEqual(self.content, pdf)
