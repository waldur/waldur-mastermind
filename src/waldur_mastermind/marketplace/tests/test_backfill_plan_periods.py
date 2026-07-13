"""Tests for the backfill_plan_periods management command, focused on the
offering / resource / billing-period filtering options."""

import datetime

from django.core.management import CommandError, call_command
from rest_framework import test

from waldur_mastermind.marketplace.models import ResourceStates
from waldur_mastermind.marketplace.tests import factories


class BackfillPlanPeriodsFilterTest(test.APITestCase):
    def setUp(self):
        # An open plan period is required for step 1 of the command to backfill.
        self.resource = factories.ResourceFactory(state=ResourceStates.OK)
        self.plan_period = factories.ResourcePlanPeriodFactory(
            resource=self.resource, plan=self.resource.plan, end=None
        )

    def _orphan(self, resource, billing_period):
        return factories.ComponentUsageFactory(
            resource=resource,
            plan_period=None,
            billing_period=billing_period,
        )

    def _fresh_open_period(self):
        resource = factories.ResourceFactory(state=ResourceStates.OK)
        factories.ResourcePlanPeriodFactory(
            resource=resource, plan=resource.plan, end=None
        )
        return resource

    def test_backfill_all_without_filters(self):
        orphan = self._orphan(self.resource, datetime.date(2026, 1, 1))
        call_command("backfill_plan_periods")
        orphan.refresh_from_db()
        self.assertEqual(orphan.plan_period, self.plan_period)

    def test_offering_filter_skips_other_offerings(self):
        other = self._fresh_open_period()
        target_orphan = self._orphan(self.resource, datetime.date(2026, 1, 1))
        other_orphan = self._orphan(other, datetime.date(2026, 1, 1))

        call_command("backfill_plan_periods", offering=self.resource.offering.uuid.hex)

        target_orphan.refresh_from_db()
        other_orphan.refresh_from_db()
        self.assertIsNotNone(target_orphan.plan_period)
        self.assertIsNone(other_orphan.plan_period)

    def test_resource_filter_skips_other_resources(self):
        other = self._fresh_open_period()
        target_orphan = self._orphan(self.resource, datetime.date(2026, 1, 1))
        other_orphan = self._orphan(other, datetime.date(2026, 1, 1))

        call_command("backfill_plan_periods", resource=self.resource.uuid.hex)

        target_orphan.refresh_from_db()
        other_orphan.refresh_from_db()
        self.assertIsNotNone(target_orphan.plan_period)
        self.assertIsNone(other_orphan.plan_period)

    def test_date_range_filters_billing_period(self):
        before = self._orphan(self.resource, datetime.date(2025, 12, 1))
        inside = self._orphan(self.resource, datetime.date(2026, 2, 1))
        after = self._orphan(self.resource, datetime.date(2026, 4, 1))

        call_command(
            "backfill_plan_periods",
            start_date="2026-01-01",
            end_date="2026-03-31",
        )

        before.refresh_from_db()
        inside.refresh_from_db()
        after.refresh_from_db()
        self.assertIsNone(before.plan_period)
        self.assertEqual(inside.plan_period, self.plan_period)
        self.assertIsNone(after.plan_period)

    def test_dry_run_makes_no_changes(self):
        orphan = self._orphan(self.resource, datetime.date(2026, 1, 1))
        call_command("backfill_plan_periods", dry_run=True)
        orphan.refresh_from_db()
        self.assertIsNone(orphan.plan_period)

    def test_invalid_date_raises(self):
        with self.assertRaises(CommandError):
            call_command("backfill_plan_periods", start_date="01-01-2026")

    def test_start_after_end_raises(self):
        with self.assertRaises(CommandError):
            call_command(
                "backfill_plan_periods",
                start_date="2026-03-01",
                end_date="2026-01-01",
            )

    def test_unknown_offering_raises(self):
        with self.assertRaises(CommandError):
            call_command(
                "backfill_plan_periods",
                offering="0" * 32,
            )

    def test_unknown_resource_raises(self):
        with self.assertRaises(CommandError):
            call_command(
                "backfill_plan_periods",
                resource="0" * 32,
            )
