"""Closing a limit-based invoice item must honour the plan's billing unit."""

from datetime import timedelta
from decimal import Decimal

from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    LimitPeriods,
    OfferingStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories


@freeze_time("2026-04-01 00:00:00")
class LimitItemTerminationUnitTest(test.APITestCase):
    def make_resource(self, unit):
        offering = factories.OfferingFactory(
            type=BASIC_OFFERING, state=OfferingStates.ACTIVE
        )
        component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
            type="cpu",
            name="CPU",
        )
        plan = factories.PlanFactory(offering=offering, unit=unit, unit_price=0)
        factories.PlanComponentFactory(
            plan=plan, component=component, price=Decimal("10")
        )
        resource = factories.ResourceFactory(
            offering=offering,
            plan=plan,
            limits={"cpu": 4},
            state=ResourceStates.CREATING,
        )
        callbacks.resource_creation_succeeded(resource)
        return invoices_models.InvoiceItem.objects.get(
            resource=resource, details__offering_component_type="cpu"
        )

    def test_monthly_item_keeps_the_month_fee_when_closed_after_ten_days(self):
        item = self.make_resource(models.Plan.Units.PER_MONTH)
        self.assertEqual(item.quantity, Decimal("4"))
        item.terminate(end=item.start + timedelta(days=10))
        # The plan bills the month: closing early neither multiplies the
        # limit by days nor reduces it.
        self.assertEqual(item.quantity, Decimal("4"))
        self.assertEqual(item.price, Decimal("40"))

    def test_daily_item_counts_limit_times_days(self):
        item = self.make_resource(models.Plan.Units.PER_DAY)
        item.terminate(end=item.start + timedelta(days=10))
        self.assertEqual(item.quantity, Decimal("40"))
        self.assertEqual(item.price, Decimal("400"))

    def test_monthly_item_weights_limit_changes_by_days(self):
        item = self.make_resource(models.Plan.Units.PER_MONTH)
        # Simulate a limit raised to 8 after 10 days by appending a period.
        periods = item.details["resource_limit_periods"]
        periods[0]["end"] = (item.start + timedelta(days=10)).isoformat()
        periods[0]["billing_periods"] = 10
        periods[0]["total"] = "40"
        periods.append(
            {
                "start": (item.start + timedelta(days=10)).isoformat(),
                "end": (item.start + timedelta(days=20)).isoformat(),
                "quantity": 8,
                "billing_periods": 10,
                "total": "80",
            }
        )
        item.details["resource_limit_periods"] = periods
        item.save(update_fields=["details"])
        item.terminate(end=item.start + timedelta(days=20))
        # (4 × 10 + 8 × 10) / 20 days
        self.assertEqual(item.quantity, Decimal("6"))
