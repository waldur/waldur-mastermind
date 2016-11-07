import datetime

from django.test import TestCase

from ... import models


class OpenStackItemTest(TestCase):

    def test_calculate_price_for_period_return_day_if_one_hour_passed(self):
        start = datetime.datetime(year=2016, month=11, day=4, hour=12, minute=0, second=0)
        end = start + datetime.timedelta(hours=1)
        hourly_price = 13

        calculated_price = models.OpenStackItem.calculate_price_for_period(hourly_price, start, end)

        expected_price = hourly_price * 24
        self.assertEqual(calculated_price, expected_price)
