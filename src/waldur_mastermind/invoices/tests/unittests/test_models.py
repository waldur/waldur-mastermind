from django.test import TestCase

from waldur_mastermind.common.utils import parse_datetime

from .. import factories


class InvoiceItemTest(TestCase):
    def setUp(self):
        self.invoice = factories.InvoiceFactory()
        self.item = factories.GenericInvoiceItemFactory(
            start=parse_datetime('2016-11-17 14:00:00'),
            end=parse_datetime('2016-12-1 14:00:00'),
        )

    def test_usage_days_and_current_factor_cannot_be_larger_than_end_field(self):
        self.assertEqual(self.item.usage_days, 14)
        self.assertEqual(self.item.get_factor(current=True), 14)
