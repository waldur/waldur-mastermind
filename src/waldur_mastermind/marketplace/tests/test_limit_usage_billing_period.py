import datetime

from freezegun import freeze_time
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.serializers import ResourceSerializer
from waldur_mastermind.marketplace.tests import factories


def _create_limit_resource(fixture, limit_period, limit_amount=1000):
    """Helper to set up an offering with a LIMIT component, a plan, and a resource."""
    offering = factories.OfferingFactory(customer=fixture.customer)
    plan = factories.PlanFactory(offering=offering)
    offering_component = factories.OfferingComponentFactory(
        offering=offering,
        billing_type=BillingTypes.LIMIT,
        type="cpu",
        limit_period=limit_period,
        limit_amount=limit_amount,
    )
    factories.PlanComponentFactory(plan=plan, component=offering_component)
    resource = models.Resource.objects.create(
        offering=offering,
        plan=plan,
        project=fixture.project,
        state=ResourceStates.OK,
    )
    factories.OrderFactory(
        resource=resource,
        type=OrderTypes.CREATE,
        state=OrderStates.EXECUTING,
        plan=plan,
    )
    callbacks.resource_creation_succeeded(resource)
    plan_period = models.ResourcePlanPeriod.objects.create(resource=resource, plan=plan)
    return resource, offering_component, plan_period


# ---------------------------------------------------------------------------
# Bug 1: get_limit_usage must filter on billing_period, not date
# ---------------------------------------------------------------------------


@freeze_time("2026-01-15")
class QuarterlyLimitUsageBillingPeriodEdgeCaseTest(test.APITestCase):
    """
    Regression test: a ComponentUsage record whose ``date`` (sync timestamp)
    falls in Q4 2025 but whose ``billing_period`` is January 2026 (i.e. Q1)
    must be counted in Q1 limit usage.

    Concrete scenario: a CET/EET timezone causes the sync to happen at
    2025-12-31 23:00 UTC while the billing_period is 2026-01-01.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.resource, self.component, self.plan_period = _create_limit_resource(
            self.fixture, LimitPeriods.QUARTERLY
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)

    def test_usage_with_date_in_previous_quarter_but_billing_period_in_current_quarter_is_included(
        self,
    ):
        # date in Q4 2025, billing_period in Q1 2026
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=42,
            date=datetime.datetime(2025, 12, 31, 23, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 1, 1),
        )

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(limit_usage.get("cpu"), 42)

    def test_usage_with_date_and_billing_period_both_in_current_quarter_is_included(
        self,
    ):
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=10,
            date=datetime.datetime(2026, 1, 5, 12, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 1, 1),
        )

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(limit_usage.get("cpu"), 10)

    def test_usage_with_billing_period_in_previous_quarter_is_excluded(self):
        # Both date and billing_period in Q4 2025
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=99,
            date=datetime.datetime(2025, 12, 15, 10, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2025, 12, 1),
        )

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(limit_usage.get("cpu"), 0)

    def test_mixed_billing_periods_across_quarter_boundary_sums_only_current(self):
        # Previous quarter usage (should be excluded)
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=100,
            date=datetime.datetime(2025, 12, 15, 10, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2025, 12, 1),
        )
        # Timezone edge-case usage (should be included)
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=25,
            date=datetime.datetime(2025, 12, 31, 23, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 1, 1),
        )
        # Normal current quarter usage (should be included)
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=15,
            date=datetime.datetime(2026, 2, 10, 12, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 2, 1),
        )

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(limit_usage.get("cpu"), 40)  # 25 + 15


@freeze_time("2026-01-15")
class AnnualLimitUsageBillingPeriodEdgeCaseTest(test.APITestCase):
    """
    Regression test: a ComponentUsage record whose ``date`` is in the
    previous year (2025-12-31 23:00 UTC) but whose ``billing_period`` is in
    the current year (2026-01-01) must be counted in the annual limit usage.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.resource, self.component, self.plan_period = _create_limit_resource(
            self.fixture, LimitPeriods.ANNUAL
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)

    def test_usage_with_date_in_previous_year_but_billing_period_in_current_year_is_included(
        self,
    ):
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=77,
            date=datetime.datetime(2025, 12, 31, 23, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 1, 1),
        )

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(limit_usage.get("cpu"), 77)

    def test_usage_with_billing_period_in_previous_year_is_excluded(self):
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=50,
            date=datetime.datetime(2025, 12, 15, 10, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2025, 12, 1),
        )

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(limit_usage.get("cpu"), 0)

    def test_mixed_years_sums_only_current_year(self):
        # Previous year (excluded)
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=200,
            date=datetime.datetime(2025, 6, 15, 10, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2025, 6, 1),
        )
        # Timezone edge-case (included)
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=30,
            date=datetime.datetime(2025, 12, 31, 23, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 1, 1),
        )
        # Normal current year, different month (included)
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=20,
            date=datetime.datetime(2026, 2, 10, 12, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 2, 1),
        )

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(limit_usage.get("cpu"), 50)  # 30 + 20


# ---------------------------------------------------------------------------
# Bug: get_limit_usage must include records with plan_period=None
# ---------------------------------------------------------------------------


@freeze_time("2026-03-15")
class TotalLimitUsageWithNullPlanPeriodTest(test.APITestCase):
    """
    Regression test: ComponentUsage records with plan_period=None
    (created by site agent via set_usage) must be included in
    limit_usage calculation. Previously, .exclude(plan_period=None)
    filtered them out, causing the panel to show 0 usage.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.resource, self.component, self.plan_period = _create_limit_resource(
            self.fixture, LimitPeriods.TOTAL
        )

    def test_usage_with_null_plan_period_is_included_for_total(self):
        """Usage reported by site agent (plan_period=None) should be counted."""
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=None,
            component=self.component,
            usage=25,
            date=datetime.datetime(2026, 3, 1, 12, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 3, 1),
        )

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(limit_usage.get("cpu"), 25)

    def test_mixed_null_and_non_null_plan_periods_are_summed(self):
        """Both plan_period=None and plan_period=X records should be summed."""
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=None,
            component=self.component,
            usage=25,
            date=datetime.datetime(2026, 2, 1, 12, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 2, 1),
        )
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=10,
            date=datetime.datetime(2026, 3, 1, 12, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 3, 1),
        )

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(limit_usage.get("cpu"), 35)  # 25 + 10

    def test_quarterly_also_includes_null_plan_period(self):
        """Same fix applies to quarterly period."""
        self.component.limit_period = LimitPeriods.QUARTERLY
        self.component.save()

        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=None,
            component=self.component,
            usage=42,
            date=datetime.datetime(2026, 1, 15, 12, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 1, 1),
        )

        limit_usage = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(limit_usage.get("cpu"), 42)


# ---------------------------------------------------------------------------
# Bug 1 applied to validate_amount: must filter on billing_period, not date
# ---------------------------------------------------------------------------


@freeze_time("2026-01-15")
class ValidateAmountBillingPeriodEdgeCaseTest(test.APITestCase):
    """
    Regression test: ``OfferingComponent.validate_amount()`` must count
    usage by ``billing_period``, not by ``date``.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.resource, self.component, self.plan_period = _create_limit_resource(
            self.fixture, LimitPeriods.QUARTERLY, limit_amount=100
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)

    def test_quarterly_validate_amount_counts_by_billing_period_not_date(self):
        """
        Usage synced at 2025-12-31 23:00 UTC with billing_period 2026-01-01
        must be counted in Q1 2026 when validating the quarterly limit.
        """
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=60,
            date=datetime.datetime(2025, 12, 31, 23, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 1, 1),
        )

        # Adding 50 more should exceed the limit of 100 (60 + 50 = 110)
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            self.component.validate_amount(
                self.resource,
                50,
                datetime.date(2026, 1, 15),
            )

    def test_quarterly_validate_amount_excludes_previous_quarter_billing_period(self):
        """
        Usage with billing_period in Q4 2025 should not count toward the Q1
        2026 limit, even if date is late December.
        """
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=90,
            date=datetime.datetime(2025, 12, 15, 10, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2025, 12, 1),
        )

        # 90 is in Q4, so adding 50 in Q1 should succeed (50 <= 100)
        self.component.validate_amount(
            self.resource,
            50,
            datetime.date(2026, 1, 15),
        )

    def test_annual_validate_amount_counts_by_billing_period_not_date(self):
        self.component.limit_period = LimitPeriods.ANNUAL
        self.component.save()

        # date in 2025, billing_period in 2026
        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=80,
            date=datetime.datetime(2025, 12, 31, 23, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2026, 1, 1),
        )

        from rest_framework.exceptions import ValidationError

        # 80 + 30 = 110 > 100, should raise
        with self.assertRaises(ValidationError):
            self.component.validate_amount(
                self.resource,
                30,
                datetime.date(2026, 1, 15),
            )

    def test_annual_validate_amount_excludes_previous_year_billing_period(self):
        self.component.limit_period = LimitPeriods.ANNUAL
        self.component.save()

        models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.component,
            usage=90,
            date=datetime.datetime(2025, 12, 15, 10, 0, 0, tzinfo=datetime.UTC),
            billing_period=datetime.date(2025, 12, 1),
        )

        # 90 is in 2025, so adding 50 in 2026 should succeed (50 <= 100)
        self.component.validate_amount(
            self.resource,
            50,
            datetime.date(2026, 1, 15),
        )
