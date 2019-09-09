import decimal

from django.test import TestCase

from waldur_mastermind.common.utils import parse_datetime, quantize_price
from waldur_mastermind.invoices import models

from .. import factories


class InvoiceItemTest(TestCase):
    def setUp(self):
        self.invoice = factories.InvoiceFactory()

    def test_usage_days_and_current_factor_cannot_be_larger_than_end_field(self):
        item = factories.InvoiceItemFactory(
            start=parse_datetime('2016-11-17 14:00:00'),
            end=parse_datetime('2016-12-1 14:00:00'),
        )
        self.assertEqual(item.usage_days, 14)
        self.assertEqual(item.get_factor(current=True), 14)

    def test_factor_for_month_is_equal_to_fraction_of_days(self):
        item = factories.InvoiceItemFactory(
            start=parse_datetime('2016-11-1 14:00:00'),
            end=parse_datetime('2016-11-8 14:00:00'),
            unit=models.InvoiceItem.Units.PER_MONTH,
        )
        self.assertEqual(item.get_factor(), quantize_price(decimal.Decimal(8.0 / 30)))

    def test_hourly_invoicing(self):
        item = factories.InvoiceItemFactory(
            start=parse_datetime('2019-08-09 10:00:00'),
            end=parse_datetime('2019-08-09 14:00:00'),
            unit_price=10,
            unit=models.InvoiceItem.Units.PER_HOUR,
        )
        self.assertEqual(item.get_factor(), 4)
        self.assertEqual(item.price, 4 * 10)
