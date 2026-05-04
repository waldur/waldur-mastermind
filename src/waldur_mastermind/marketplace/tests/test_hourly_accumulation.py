import datetime
from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.utils import import_current_usages


@freeze_time("2026-04-15 12:00:00")
class HourlyAccumulationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory()
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cores",
            billing_type=BillingTypes.USAGE,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(plan=self.plan, component=self.component)
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
        )
        models.ResourcePlanPeriod.objects.create(resource=self.resource, plan=self.plan)

    def test_first_poll_defaults_to_one_hour(self):
        import_current_usages(self.resource, {"cores": 4}, hourly_accumulation=True)
        usage = models.ComponentUsage.objects.get(
            resource=self.resource, component=self.component
        )
        # First poll: 4 cores × 1h default = 4
        self.assertEqual(usage.usage, Decimal("4.00"))

    def test_accumulation_over_multiple_polls(self):
        now = timezone.now()
        # Simulate first poll
        import_current_usages(self.resource, {"cores": 4}, hourly_accumulation=True)

        # Simulate second poll 2 hours later
        two_hours_later = now + datetime.timedelta(hours=2)
        with patch("waldur_mastermind.marketplace.utils.timezone") as mock_tz:
            mock_tz.now.return_value = two_hours_later
            import_current_usages(self.resource, {"cores": 8}, hourly_accumulation=True)

        usage = models.ComponentUsage.objects.get(
            resource=self.resource, component=self.component
        )
        # First poll: 4 × 1h = 4
        # Second poll: 8 × 2h = 16
        # Total: 20
        self.assertEqual(usage.usage, Decimal("20.00"))

    def test_elapsed_time_capped_at_24h(self):
        now = timezone.now()
        import_current_usages(self.resource, {"cores": 2}, hourly_accumulation=True)

        # Simulate poll 48 hours later
        later = now + datetime.timedelta(hours=48)
        with patch("waldur_mastermind.marketplace.utils.timezone") as mock_tz:
            mock_tz.now.return_value = later
            import_current_usages(self.resource, {"cores": 2}, hourly_accumulation=True)

        usage = models.ComponentUsage.objects.get(
            resource=self.resource, component=self.component
        )
        # First: 2 × 1h = 2
        # Second: 2 × 24h (capped) = 48
        # Total: 50
        self.assertEqual(usage.usage, Decimal("50.00"))

    def test_zero_usage_no_accumulation(self):
        import_current_usages(self.resource, {"cores": 0}, hourly_accumulation=True)
        usage = models.ComponentUsage.objects.get(
            resource=self.resource, component=self.component
        )
        self.assertEqual(usage.usage, Decimal("0.00"))

    def test_poll_record_created(self):
        import_current_usages(self.resource, {"cores": 4}, hourly_accumulation=True)
        record = models.ComponentUsagePollRecord.objects.get(
            resource=self.resource, component=self.component
        )
        self.assertEqual(record.raw_usage, Decimal("4.00"))
        self.assertEqual(record.elapsed_hours, Decimal("1.00"))
        self.assertEqual(record.increment, Decimal("4.00"))
        self.assertEqual(record.accumulated_total, Decimal("4.00"))

    def test_poll_record_updated_on_subsequent_poll(self):
        now = timezone.now()
        import_current_usages(self.resource, {"cores": 4}, hourly_accumulation=True)

        later = now + datetime.timedelta(hours=3)
        with patch("waldur_mastermind.marketplace.utils.timezone") as mock_tz:
            mock_tz.now.return_value = later
            import_current_usages(self.resource, {"cores": 6}, hourly_accumulation=True)

        records = models.ComponentUsagePollRecord.objects.filter(
            resource=self.resource, component=self.component
        )
        self.assertEqual(records.count(), 1)
        record = records.first()
        self.assertEqual(record.raw_usage, Decimal("6.00"))
        self.assertEqual(record.accumulated_total, Decimal("22.00"))


class HighWatermarkPreservedTest(test.APITransactionTestCase):
    """Ensure LIMIT components still use max() even when hourly_accumulation=True."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory()
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cores",
            billing_type=BillingTypes.LIMIT,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(plan=self.plan, component=self.component)
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
        )
        models.ResourcePlanPeriod.objects.create(resource=self.resource, plan=self.plan)

    @freeze_time("2026-04-15 12:00:00")
    def test_limit_component_uses_high_watermark(self):
        import_current_usages(self.resource, {"cores": 10}, hourly_accumulation=True)
        import_current_usages(self.resource, {"cores": 8}, hourly_accumulation=True)
        usage = models.ComponentUsage.objects.get(
            resource=self.resource, component=self.component
        )
        # High-watermark: max(10, 8) = 10
        self.assertEqual(usage.usage, 10)

    @freeze_time("2026-04-15 12:00:00")
    def test_default_behavior_unchanged(self):
        """Without hourly_accumulation flag, behavior is always high-watermark."""
        import_current_usages(self.resource, {"cores": 10})
        import_current_usages(self.resource, {"cores": 8})
        usage = models.ComponentUsage.objects.get(
            resource=self.resource, component=self.component
        )
        self.assertEqual(usage.usage, 10)

    @freeze_time("2026-04-15 12:00:00")
    def test_no_poll_record_for_limit_component(self):
        import_current_usages(self.resource, {"cores": 10}, hourly_accumulation=True)
        self.assertFalse(
            models.ComponentUsagePollRecord.objects.filter(
                resource=self.resource
            ).exists()
        )

    @freeze_time("2026-04-30 23:30:00+02:00")
    def test_billing_period_uses_timezone_aware_date(self):
        """Date must be derived from timezone.now(), not datetime.date.today().

        When the server local time is 2026-04-30 23:30 CEST (UTC+2),
        timezone.now() returns 2026-04-30 21:30 UTC, so the billing
        period should be April. datetime.date.today() would also return
        April 30 here, but if the offset were flipped (UTC ahead of
        local), today() could disagree with now(). We freeze a
        timezone-aware timestamp to verify the date is always derived
        from now.
        """
        import_current_usages(self.resource, {"cores": 4}, hourly_accumulation=True)
        usage = models.ComponentUsage.objects.get(
            resource=self.resource, component=self.component
        )
        self.assertEqual(usage.billing_period, datetime.date(2026, 4, 1))
