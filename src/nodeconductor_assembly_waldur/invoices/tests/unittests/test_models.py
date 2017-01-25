import datetime

from django.test import TestCase
from freezegun import freeze_time

from .. import fixtures


class OpenStackItemTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def test_calculate_price_for_period_return_day_if_one_hour_passed(self):
        start = datetime.datetime(year=2016, month=11, day=4, hour=12, minute=0, second=0)
        end = start + datetime.timedelta(hours=1)
        daily_price = 13

        item = fixtures.factories.OpenStackItemFactory(price=daily_price)
        calculated_price = item.calculate_price_for_period(start, end)

        self.assertEqual(calculated_price, daily_price)

    def test_usage_days_cannot_be_larger_than_end_field(self):
        with freeze_time('2016-11-17 14:00:00'):
            items = self.fixture.invoice.openstack_items.all()

        with freeze_time('2016-12-1 14:00:00'):
            for item in items:
                self.assertEqual(item.usage_days, item.end)
