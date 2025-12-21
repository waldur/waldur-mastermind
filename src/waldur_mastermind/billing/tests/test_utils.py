import decimal

from django.test import TransactionTestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_mastermind.billing import utils
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests.factories import (
    InvoiceFactory,
    InvoiceItemFactory,
)


class BillingUtilsTest(TransactionTestCase):
    def setUp(self):
        """Set up test data that will be used across multiple test methods."""
        self.now = timezone.now()
        self.invoice = InvoiceFactory(
            tax_percent=decimal.Decimal("20.00"),
            year=self.now.year,
            month=self.now.month,
        )
        self.items_qs = invoice_models.InvoiceItem.objects.filter(invoice=self.invoice)

    def test_aggregate_invoice_items_sum_empty_queryset(self):
        """Test aggregation with no invoice items returns zero."""
        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=False, tax=False
        )
        self.assertEqual(total, decimal.Decimal("0.00"))

        # Test with tax enabled on empty queryset
        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=False, tax=True
        )
        self.assertEqual(total, decimal.Decimal("0.00"))

    def test_aggregate_invoice_items_sum_fixed_quantity_no_tax(self):
        """Test aggregation with fixed quantity items without tax."""
        InvoiceItemFactory(
            invoice=self.invoice,
            unit_price=decimal.Decimal("10.00"),
            quantity=decimal.Decimal("2.50"),
        )
        InvoiceItemFactory(
            invoice=self.invoice,
            unit_price=decimal.Decimal("15.00"),
            quantity=decimal.Decimal("1.00"),
        )

        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=False, tax=False
        )
        # (10.00 * 2.50) + (15.00 * 1.00) = 25.00 + 15.00 = 40.00
        self.assertEqual(total, decimal.Decimal("40.00"))

    def test_aggregate_invoice_items_sum_fixed_quantity_with_tax(self):
        """Test aggregation with fixed quantity items with tax."""
        InvoiceItemFactory(
            invoice=self.invoice,
            unit_price=decimal.Decimal("10.00"),
            quantity=decimal.Decimal("1.00"),
        )
        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=False, tax=True
        )
        # 10.00 * 20% = 2.00
        self.assertEqual(total, decimal.Decimal("2.00"))

    def test_aggregate_invoice_items_sum_multiple_items_with_tax(self):
        """Test aggregation with multiple items and tax calculation."""
        InvoiceItemFactory(
            invoice=self.invoice,
            unit_price=decimal.Decimal("100.00"),
            quantity=decimal.Decimal("2.00"),
        )
        InvoiceItemFactory(
            invoice=self.invoice,
            unit_price=decimal.Decimal("50.00"),
            quantity=decimal.Decimal("3.00"),
        )

        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=False, tax=True
        )
        # Total before tax: (100.00 * 2.00) + (50.00 * 3.00) = 200.00 + 150.00 = 350.00
        # Tax: 350.00 * 20% = 70.00
        self.assertEqual(total, decimal.Decimal("70.00"))

    @freeze_time("2024-01-15 12:00:00")
    def test_aggregate_invoice_items_sum_current_per_hour_exact(self):
        """Test current calculation for PER_HOUR items with exact hour duration."""
        start_time = timezone.now() - timezone.timedelta(hours=2)
        end_time = timezone.now() + timezone.timedelta(hours=2)

        InvoiceItemFactory(
            invoice=self.invoice,
            start=start_time,
            end=end_time,
            unit=invoice_models.InvoiceItem.Units.PER_HOUR,
            unit_price=decimal.Decimal("10.00"),
        )

        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=True, tax=False
        )
        # Should be billed for 2 hours (from start to now)
        self.assertEqual(total, decimal.Decimal("20.00"))

    @freeze_time("2024-01-15 12:30:00")
    def test_aggregate_invoice_items_sum_current_per_hour_partial(self):
        """Test current calculation for PER_HOUR items with partial hour duration."""
        start_time = timezone.now() - timezone.timedelta(hours=1, minutes=30)
        end_time = timezone.now() + timezone.timedelta(hours=1)

        InvoiceItemFactory(
            invoice=self.invoice,
            start=start_time,
            end=end_time,
            unit=invoice_models.InvoiceItem.Units.PER_HOUR,
            unit_price=decimal.Decimal("10.00"),
        )

        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=True, tax=False
        )
        # Should be billed for 2 hours (1.5 hours rounded up using ceiling)
        self.assertEqual(total, decimal.Decimal("20.00"))

    @freeze_time("2024-01-15 12:00:00")
    def test_aggregate_invoice_items_sum_current_per_day_exact(self):
        """Test current calculation for PER_DAY items with exact day duration."""
        start_time = timezone.now() - timezone.timedelta(days=1)
        end_time = timezone.now() + timezone.timedelta(days=1)

        InvoiceItemFactory(
            invoice=self.invoice,
            start=start_time,
            end=end_time,
            unit=invoice_models.InvoiceItem.Units.PER_DAY,
            unit_price=decimal.Decimal("100.00"),
        )

        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=True, tax=False
        )
        # Should be billed for 1 day (from start to now)
        self.assertEqual(total, decimal.Decimal("100.00"))

    @freeze_time("2024-01-15 12:30:00")
    def test_aggregate_invoice_items_sum_current_per_day_partial(self):
        """Test current calculation for PER_DAY items with partial day duration."""
        start_time = timezone.now() - timezone.timedelta(hours=18)  # 0.75 days
        end_time = timezone.now() + timezone.timedelta(days=1)

        InvoiceItemFactory(
            invoice=self.invoice,
            start=start_time,
            end=end_time,
            unit=invoice_models.InvoiceItem.Units.PER_DAY,
            unit_price=decimal.Decimal("100.00"),
        )

        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=True, tax=False
        )
        # Should be billed for 1 day (0.75 days rounded up using ceiling)
        self.assertEqual(total, decimal.Decimal("100.00"))

    @freeze_time("2024-01-15 12:00:00")
    def test_aggregate_invoice_items_sum_current_past_end_date(self):
        """Test current calculation when current time is past the item's end date."""
        start_time = timezone.now() - timezone.timedelta(hours=3)
        end_time = timezone.now() - timezone.timedelta(hours=1)  # End is in the past

        InvoiceItemFactory(
            invoice=self.invoice,
            start=start_time,
            end=end_time,
            unit=invoice_models.InvoiceItem.Units.PER_HOUR,
            unit_price=decimal.Decimal("10.00"),
        )

        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=True, tax=False
        )
        # Should be billed for 2 hours (from start to end, not to now)
        self.assertEqual(total, decimal.Decimal("20.00"))

    @freeze_time("2024-01-15 12:00:00")
    def test_aggregate_invoice_items_sum_current_with_tax(self):
        """Test current calculation combined with tax calculation."""
        start_time = timezone.now() - timezone.timedelta(hours=1)

        InvoiceItemFactory(
            invoice=self.invoice,
            start=start_time,
            end=timezone.now() + timezone.timedelta(hours=1),
            unit=invoice_models.InvoiceItem.Units.PER_HOUR,
            unit_price=decimal.Decimal("50.00"),
        )

        total = utils.aggregate_invoice_items_sum(self.items_qs, current=True, tax=True)
        # 1 hour * 50.00 = 50.00
        # Tax: 50.00 * 20% = 10.00
        self.assertEqual(total, decimal.Decimal("10.00"))

    def test_aggregate_invoice_items_sum_different_tax_rates(self):
        """Test aggregation with different tax rates across invoices."""
        # Create another invoice with different tax rate
        invoice2 = InvoiceFactory(
            tax_percent=decimal.Decimal("10.00"),
            year=self.now.year,
            month=self.now.month,
        )

        InvoiceItemFactory(
            invoice=self.invoice,  # 20% tax
            unit_price=decimal.Decimal("100.00"),
            quantity=decimal.Decimal("1.00"),
        )

        InvoiceItemFactory(
            invoice=invoice2,  # 10% tax
            unit_price=decimal.Decimal("100.00"),
            quantity=decimal.Decimal("1.00"),
        )

        # Test only first invoice items
        total = utils.aggregate_invoice_items_sum(
            self.items_qs, current=False, tax=True
        )
        self.assertEqual(total, decimal.Decimal("20.00"))

        all_items = invoice_models.InvoiceItem.objects.all()
        total = utils.aggregate_invoice_items_sum(all_items, current=False, tax=True)
        # (100.00 * 20%) + (100.00 * 10%) = 20.00 + 10.00 = 30.00
        self.assertEqual(total, decimal.Decimal("30.00"))

    def test_get_current_expression_edge_cases(self):
        """Test get_current_expression with edge cases."""
        # This test would require more setup to test the expression directly,
        # but we can test it indirectly through the aggregate function

        with freeze_time("2024-01-15 12:00:00"):
            # Test with very short duration (should round up to 1)
            start_time = timezone.now() - timezone.timedelta(minutes=1)

            InvoiceItemFactory(
                invoice=self.invoice,
                start=start_time,
                end=timezone.now() + timezone.timedelta(hours=1),
                unit=invoice_models.InvoiceItem.Units.PER_HOUR,
                unit_price=decimal.Decimal("60.00"),
            )

            total = utils.aggregate_invoice_items_sum(
                self.items_qs, current=True, tax=False
            )
            # Even 1 minute should be rounded up to 1 hour
            self.assertEqual(total, decimal.Decimal("60.00"))
