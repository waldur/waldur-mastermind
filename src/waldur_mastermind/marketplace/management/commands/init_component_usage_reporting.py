import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.marketplace.models import (
    ComponentUsageMonthly,
    OfferingComponent,
)
from waldur_mastermind.marketplace.tasks import (
    calculate_allocated_for_month,
    calculate_consumed_for_month,
)


class Command(BaseCommand):
    help = (
        "Backfills the ComponentUsageMonthly reporting table with historical data. "
        "Safe to run multiple times (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--months",
            type=int,
            default=12,
            help="Number of months to look back (default: 12). Use 0 for current month only.",
        )
        parser.add_argument(
            "--all-time",
            action="store_true",
            help="Calculate from the oldest invoice in the database to the current month.",
        )

    def handle(self, *args, **options):
        now = timezone.now()

        # The maximum allowed value for max_digits=20, decimal_places=2
        MAX_DECIMAL = Decimal("999999999999999999.99")

        if options["all_time"]:
            # Find the oldest invoice item in the system to determine where to start
            oldest_item = InvoiceItem.objects.order_by(
                "invoice__year", "invoice__month"
            ).first()
            if oldest_item:
                start_date = datetime.date(
                    oldest_item.invoice.year, oldest_item.invoice.month, 1
                )
            else:
                start_date = datetime.date(now.year, now.month, 1)

            delta = relativedelta(datetime.date(now.year, now.month, 1), start_date)
            months_back = delta.years * 12 + delta.months
        else:
            months_back = options["months"]

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting historical backfill for the last {months_back} months..."
            )
        )

        # Pre-fetch components to avoid querying them inside the loop
        components = OfferingComponent.objects.select_related(
            "offering", "offering__customer", "offering__category"
        ).all()

        total_components = components.count()
        self.stdout.write(f"Found {total_components} offering components to process.")

        # Loop from the oldest month up to the current month
        for i in range(months_back, -1, -1):
            target_date = now - relativedelta(months=i)
            year, month = target_date.year, target_date.month
            billing_period = datetime.date(year, month, 1)

            self.stdout.write(
                f"\nProcessing billing period: {billing_period.strftime('%Y-%m')}..."
            )

            records_created_or_updated = 0

            # Wrap each month in a transaction for performance and safety
            with transaction.atomic():
                for component in components:
                    consumed = calculate_consumed_for_month(component, year, month)
                    allocated = calculate_allocated_for_month(component, year, month)

                    # Optimization: Skip saving rows where absolutely nothing happened
                    # to keep the reporting table lean and fast.
                    if consumed == Decimal("0") and allocated == Decimal("0"):
                        continue

                    usage_percent = None
                    if allocated > 0:
                        usage_percent = round((consumed * 100) / allocated, 2)

                    # Safety clamp to prevent DB overflow from corrupted JSONB data
                    consumed = min(consumed, MAX_DECIMAL)
                    allocated = min(allocated, MAX_DECIMAL)

                    # Update or Create the reporting snapshot
                    _, created = ComponentUsageMonthly.objects.update_or_create(
                        component=component,
                        billing_period=billing_period,
                        defaults={
                            "total_consumed": consumed,
                            "total_allocated": allocated,
                            "usage_percent": usage_percent,
                        },
                    )
                    records_created_or_updated += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Saved {records_created_or_updated} active component records for {billing_period.strftime('%Y-%m')}."
                )
            )

        self.stdout.write(
            self.style.SUCCESS("\nHistorical backfill completed successfully!")
        )
