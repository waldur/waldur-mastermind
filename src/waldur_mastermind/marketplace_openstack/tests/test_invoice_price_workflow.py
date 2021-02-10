import decimal
from calendar import monthrange

from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices import models, utils
from waldur_mastermind.marketplace_support.tests import fixtures as fixtures


class InvoicePriceWorkflowTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceSupportApprovedFixture()

    def test_invoice_item_with_daily_price(self):
        start_date = timezone.datetime(2017, 7, 14)
        end_date = timezone.datetime(2017, 7, 31, 23, 59, 59)
        resource = self.fixture.resource
        self.fixture.plan.unit = common_mixins.UnitPriceMixin.Units.PER_DAY
        self.fixture.plan.save()

        with freeze_time(start_date):
            resource.set_state_ok()
            resource.save()

        expected_price = (
            utils.get_full_days(start_date, end_date)
            * self.fixture.plan_component.price
        )
        invoice_item = models.InvoiceItem.objects.get(scope=self.fixture.resource)
        self.assertEqual(invoice_item.price, expected_price)

    def test_invoice_item_with_monthly_price(self):
        start_date = timezone.datetime(2017, 7, 20)
        end_date = timezone.datetime(2017, 7, 31)
        resource = self.fixture.resource

        with freeze_time(start_date):
            resource.set_state_ok()
            resource.save()

        use_days = (end_date - start_date).days + 1
        month_days = monthrange(start_date.year, start_date.month)[1]
        factor = quantize_price(decimal.Decimal(use_days) / month_days)
        expected_price = self.fixture.plan_component.price * factor

        invoice_item = models.InvoiceItem.objects.get(scope=resource)
        self.assertEqual(invoice_item.price, expected_price)

    def test_invoice_item_with_half_monthly_price_with_start_in_first_half(self):
        start_date = timezone.datetime(2017, 7, 14)
        resource = self.fixture.resource
        self.fixture.plan.unit = common_mixins.UnitPriceMixin.Units.PER_HALF_MONTH
        self.fixture.plan.save()

        with freeze_time(start_date):
            resource.set_state_ok()
            resource.save()

        month_days = monthrange(2017, 7)[1]
        factor = quantize_price(
            1 + (16 - start_date.day) / decimal.Decimal(month_days / 2)
        )
        expected_price = self.fixture.plan_component.price * factor
        invoice_item = models.InvoiceItem.objects.get(scope=resource)
        self.assertEqual(invoice_item.price, expected_price)

    def test_invoice_item_with_half_monthly_price_with_start_in_second_half(self):
        start_date = timezone.datetime(2017, 7, 16)
        resource = self.fixture.resource
        self.fixture.plan.unit = common_mixins.UnitPriceMixin.Units.PER_HALF_MONTH
        self.fixture.plan.save()

        with freeze_time(start_date):
            resource.set_state_ok()
            resource.save()

        expected_price = self.fixture.plan_component.price
        invoice_item = models.InvoiceItem.objects.get(scope=resource)
        self.assertEqual(invoice_item.price, expected_price)

    def test_invoice_item_with_half_monthly_price_with_end_in_first_half(self):
        start_date = timezone.datetime(2017, 7, 10)
        end_date = timezone.datetime(2017, 7, 14)
        resource, invoice_item, expected_price = self._start_end_offering(
            start_date, end_date
        )
        self.assertEqual(invoice_item.price, expected_price)

    def test_invoice_item_with_half_monthly_price_with_end_in_second_half(self):
        start_date = timezone.datetime(2017, 7, 10)
        end_date = timezone.datetime(2017, 7, 20)
        resource, invoice_item, expected_price = self._start_end_offering(
            start_date, end_date
        )
        self.assertEqual(invoice_item.price, expected_price)

    def test_invoice_item_with_both_halves(self):
        start_date = timezone.datetime(2017, 7, 1)
        month_days = monthrange(2017, 7)[1]
        end_date = timezone.datetime(2017, 7, month_days)
        resource, invoice_item, expected_price = self._start_end_offering(
            start_date, end_date
        )
        self.assertEqual(invoice_item.price, expected_price)

    def test_invoice_item_with_start_in_first_day_and_end_in_second_half(self):
        start_date = timezone.datetime(2017, 7, 1)
        end_date = timezone.datetime(2017, 7, 20)
        resource, invoice_item, expected_price = self._start_end_offering(
            start_date, end_date
        )
        self.assertEqual(invoice_item.price, expected_price)

    def _start_end_offering(self, start_date, end_date):
        resource = self.fixture.resource

        use_days = (end_date - start_date).days + 1
        month_days = monthrange(start_date.year, start_date.month)[1]
        factor = quantize_price(decimal.Decimal(use_days) / month_days)
        expected_price = self.fixture.plan_component.price * factor

        with freeze_time(start_date):
            resource.set_state_ok()
            resource.save()
            invoice_item = models.InvoiceItem.objects.get(scope=resource)

        with freeze_time(end_date):
            resource.set_state_terminating()
            resource.save()
            resource.set_state_terminated()
            resource.save()
            invoice_item.refresh_from_db()

        return resource, invoice_item, expected_price
