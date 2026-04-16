"""
Integration tests for current_usages derived from ComponentUsage records.

Verifies that:
- The post_save signal on ComponentUsage correctly updates resource.current_usages
- get_current_period_usage() always queries ComponentUsage (not current_usages)
- get_limit_usage serializer field returns correct values
- Stats endpoint aggregates correctly from ComponentUsage
- Period boundaries (monthly/quarterly/annual/total) are respected
"""

import datetime
from decimal import Decimal

from freezegun import freeze_time
from rest_framework import test

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
from waldur_mastermind.marketplace.utils import (
    get_components_usage_data,
    get_current_period_usage,
)


def _create_resource_with_components(
    fixture, billing_type, limit_period=LimitPeriods.MONTH
):
    """Create a resource with an offering that has a single LIMIT or USAGE component."""
    offering = factories.OfferingFactory(customer=fixture.customer)
    plan = factories.PlanFactory(offering=offering)
    component = factories.OfferingComponentFactory(
        offering=offering,
        billing_type=billing_type,
        type="node",
        name="Compute",
        measured_unit="node hours",
        limit_period=limit_period,
    )
    factories.PlanComponentFactory(plan=plan, component=component)
    resource = models.Resource.objects.create(
        offering=offering,
        plan=plan,
        project=fixture.project,
        state=ResourceStates.OK,
        limits={"node": 1000},
    )
    factories.OrderFactory(
        resource=resource,
        type=OrderTypes.CREATE,
        state=OrderStates.EXECUTING,
        plan=plan,
    )
    callbacks.resource_creation_succeeded(resource)
    return resource, component


def _create_usage(resource, component, usage, billing_period=None, plan_period=None):
    """Create a ComponentUsage record."""
    if billing_period is None:
        billing_period = datetime.date.today().replace(day=1)
    return models.ComponentUsage.objects.create(
        resource=resource,
        component=component,
        usage=Decimal(str(usage)),
        date=datetime.datetime.now(tz=datetime.UTC),
        billing_period=billing_period,
        plan_period=plan_period,
    )


# ---------------------------------------------------------------------------
# 1. Signal updates current_usages per component
# ---------------------------------------------------------------------------


@freeze_time("2026-04-15")
class SignalUpdatesCurrentUsagesTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.resource, self.component = _create_resource_with_components(
            self.fixture, BillingTypes.LIMIT, LimitPeriods.TOTAL
        )

    def test_signal_updates_current_usages_on_component_usage_create(self):
        _create_usage(self.resource, self.component, 25)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["node"], 25.0)

    def test_signal_updates_to_latest_billing_period(self):
        _create_usage(
            self.resource,
            self.component,
            10,
            billing_period=datetime.date(2026, 3, 1),
        )
        _create_usage(
            self.resource,
            self.component,
            20,
            billing_period=datetime.date(2026, 4, 1),
        )
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["node"], 20.0)

    def test_signal_is_idempotent_on_resave(self):
        cu = _create_usage(self.resource, self.component, 25)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["node"], 25.0)

        cu.save()  # resave without changes
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["node"], 25.0)

    def test_updating_usage_amount_updates_current_usages(self):
        cu = _create_usage(self.resource, self.component, 10)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["node"], 10.0)

        cu.usage = Decimal("20")
        cu.save()
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["node"], 20.0)


# ---------------------------------------------------------------------------
# 2. Multiple components — signal isolation
# ---------------------------------------------------------------------------


@freeze_time("2026-04-15")
class MultipleComponentSignalTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        plan = factories.PlanFactory(offering=self.offering)
        self.cpu = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        self.ram = factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        factories.PlanComponentFactory(plan=plan, component=self.cpu)
        factories.PlanComponentFactory(plan=plan, component=self.ram)
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={"cpu": 100, "ram": 200},
        )

    def test_saving_cpu_does_not_affect_ram(self):
        _create_usage(self.resource, self.cpu, 10)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages.get("cpu"), 10.0)
        self.assertNotIn("ram", self.resource.current_usages)

    def test_each_component_updated_independently(self):
        _create_usage(self.resource, self.cpu, 10)
        _create_usage(self.resource, self.ram, 20)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["cpu"], 10.0)
        self.assertEqual(self.resource.current_usages["ram"], 20.0)

    def test_updating_one_preserves_other(self):
        _create_usage(self.resource, self.cpu, 10)
        _create_usage(self.resource, self.ram, 20)
        # Update CPU
        cu = models.ComponentUsage.objects.filter(
            resource=self.resource, component=self.cpu
        ).first()
        cu.usage = Decimal("15")
        cu.save()

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["cpu"], 15.0)
        self.assertEqual(self.resource.current_usages["ram"], 20.0)


# ---------------------------------------------------------------------------
# 3. Period boundaries
# ---------------------------------------------------------------------------


@freeze_time("2026-05-15")
class PeriodBoundaryTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    def test_quarterly_shows_only_current_quarter(self):
        resource, component = _create_resource_with_components(
            self.fixture, BillingTypes.LIMIT, LimitPeriods.QUARTERLY
        )
        # Q1 usage (should be excluded — we're in Q2)
        _create_usage(resource, component, 100, datetime.date(2026, 3, 1))
        # Q2 usage
        _create_usage(resource, component, 25, datetime.date(2026, 4, 1))
        _create_usage(resource, component, 30, datetime.date(2026, 5, 1))

        result = get_current_period_usage(resource)
        self.assertEqual(result["node"], 55.0)  # 25 + 30 (Q2 only)

    def test_monthly_shows_only_current_month(self):
        resource, component = _create_resource_with_components(
            self.fixture, BillingTypes.LIMIT, LimitPeriods.MONTH
        )
        _create_usage(resource, component, 100, datetime.date(2026, 4, 1))
        _create_usage(resource, component, 25, datetime.date(2026, 5, 1))

        result = get_current_period_usage(resource)
        self.assertEqual(result["node"], 25.0)  # May only

    def test_annual_shows_current_year(self):
        resource, component = _create_resource_with_components(
            self.fixture, BillingTypes.LIMIT, LimitPeriods.ANNUAL
        )
        _create_usage(resource, component, 100, datetime.date(2025, 12, 1))
        _create_usage(resource, component, 25, datetime.date(2026, 1, 1))
        _create_usage(resource, component, 30, datetime.date(2026, 5, 1))

        result = get_current_period_usage(resource)
        self.assertEqual(result["node"], 55.0)  # 2026 only

    def test_total_sums_everything(self):
        resource, component = _create_resource_with_components(
            self.fixture, BillingTypes.LIMIT, LimitPeriods.TOTAL
        )
        _create_usage(resource, component, 100, datetime.date(2025, 6, 1))
        _create_usage(resource, component, 25, datetime.date(2026, 1, 1))
        _create_usage(resource, component, 30, datetime.date(2026, 5, 1))

        result = get_current_period_usage(resource)
        self.assertEqual(result["node"], 155.0)


# ---------------------------------------------------------------------------
# 4. Zero usage / empty state
# ---------------------------------------------------------------------------


@freeze_time("2026-04-15")
class ZeroUsageTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.resource, self.component = _create_resource_with_components(
            self.fixture, BillingTypes.LIMIT, LimitPeriods.TOTAL
        )

    def test_no_records_returns_zero(self):
        result = get_current_period_usage(self.resource)
        self.assertEqual(result["node"], 0.0)

    def test_current_usages_empty_without_records(self):
        self.resource.refresh_from_db()
        self.assertNotIn("node", self.resource.current_usages)

    def test_limit_usage_returns_zero_without_records(self):
        result = ResourceSerializer().get_limit_usage(self.resource)
        self.assertEqual(result.get("node"), 0.0)


# ---------------------------------------------------------------------------
# 5. Concurrent resources — isolation
# ---------------------------------------------------------------------------


@freeze_time("2026-04-15")
class ConcurrentResourcesTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        plan = factories.PlanFactory(offering=self.offering)
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        factories.PlanComponentFactory(plan=plan, component=self.component)

        self.resource1 = models.Resource.objects.create(
            offering=self.offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={"node": 500},
        )
        self.resource2 = models.Resource.objects.create(
            offering=self.offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={"node": 500},
        )

    def test_usage_is_per_resource(self):
        _create_usage(self.resource1, self.component, 10)
        _create_usage(self.resource2, self.component, 20)

        self.resource1.refresh_from_db()
        self.resource2.refresh_from_db()

        self.assertEqual(self.resource1.current_usages["node"], 10.0)
        self.assertEqual(self.resource2.current_usages["node"], 20.0)

    def test_get_current_period_usage_is_per_resource(self):
        _create_usage(self.resource1, self.component, 10)
        _create_usage(self.resource2, self.component, 20)

        self.assertEqual(get_current_period_usage(self.resource1)["node"], 10.0)
        self.assertEqual(get_current_period_usage(self.resource2)["node"], 20.0)


# ---------------------------------------------------------------------------
# 6. Mixed billing types
# ---------------------------------------------------------------------------


@freeze_time("2026-04-15")
class MixedBillingTypesTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        plan = factories.PlanFactory(offering=self.offering)
        self.usage_comp = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            billing_type=BillingTypes.USAGE,
        )
        self.limit_comp = factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        self.fixed_comp = factories.OfferingComponentFactory(
            offering=self.offering,
            type="storage",
            billing_type=BillingTypes.FIXED,
        )
        for comp in [self.usage_comp, self.limit_comp, self.fixed_comp]:
            factories.PlanComponentFactory(plan=plan, component=comp)

        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={"cpu": 100, "ram": 200, "storage": 50},
        )

    def test_signal_fires_for_all_billing_types(self):
        _create_usage(self.resource, self.usage_comp, 10)
        _create_usage(self.resource, self.limit_comp, 20)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["cpu"], 10.0)
        self.assertEqual(self.resource.current_usages["ram"], 20.0)

    def test_stats_separates_usage_and_limit(self):
        _create_usage(self.resource, self.usage_comp, 10)
        _create_usage(self.resource, self.limit_comp, 20)

        resources = models.Resource.objects.filter(pk=self.resource.pk)
        stats = get_components_usage_data(resources)

        cpu_stat = next(s for s in stats if s["type"] == "cpu")
        ram_stat = next(s for s in stats if s["type"] == "ram")

        self.assertEqual(cpu_stat["usage"], 10.0)
        self.assertEqual(cpu_stat["limit_usage"], 0)  # USAGE type → not in limit_usage
        self.assertEqual(ram_stat["usage"], 0)  # LIMIT type → not in usage
        self.assertEqual(ram_stat["limit_usage"], 20.0)

    def test_same_type_different_billing_type_across_offerings(self):
        """Components with same type but different billing_type across
        offerings must appear as separate entries in stats."""
        # Create a second offering with a "cpu" component but billing_type=LIMIT
        offering2 = factories.OfferingFactory(customer=self.fixture.customer)
        plan2 = factories.PlanFactory(offering=offering2)
        limit_cpu_comp = factories.OfferingComponentFactory(
            offering=offering2,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        factories.PlanComponentFactory(plan=plan2, component=limit_cpu_comp)
        resource2 = models.Resource.objects.create(
            offering=offering2,
            plan=plan2,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={"cpu": 5000},
        )

        _create_usage(self.resource, self.usage_comp, 10)
        _create_usage(resource2, limit_cpu_comp, 3000)

        resources = models.Resource.objects.filter(
            pk__in=[self.resource.pk, resource2.pk]
        )
        stats = get_components_usage_data(resources)

        cpu_stats = [s for s in stats if s["type"] == "cpu"]
        self.assertEqual(len(cpu_stats), 2, "Expected two separate cpu entries")

        usage_cpu = next(s for s in cpu_stats if s["billing_type"] == "usage")
        limit_cpu = next(s for s in cpu_stats if s["billing_type"] == "limit")

        self.assertEqual(usage_cpu["usage"], 10.0)
        self.assertEqual(usage_cpu["limit_usage"], 0)
        self.assertEqual(limit_cpu["limit_usage"], 3000.0)
        self.assertEqual(limit_cpu["usage"], 0)


# ---------------------------------------------------------------------------
# 7. Null plan_period (site agent records)
# ---------------------------------------------------------------------------


@freeze_time("2026-04-15")
class NullPlanPeriodTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.resource, self.component = _create_resource_with_components(
            self.fixture, BillingTypes.LIMIT, LimitPeriods.TOTAL
        )

    def test_null_plan_period_included_in_usage(self):
        _create_usage(self.resource, self.component, 25, plan_period=None)

        result = get_current_period_usage(self.resource)
        self.assertEqual(result["node"], 25.0)

    def test_null_plan_period_updates_current_usages_via_signal(self):
        _create_usage(self.resource, self.component, 25, plan_period=None)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["node"], 25.0)

    def test_mixed_null_and_non_null_plan_periods_summed(self):
        plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.resource.plan
        )
        _create_usage(
            self.resource,
            self.component,
            10,
            billing_period=datetime.date(2026, 3, 1),
            plan_period=plan_period,
        )
        _create_usage(
            self.resource,
            self.component,
            15,
            billing_period=datetime.date(2026, 4, 1),
            plan_period=None,
        )

        result = get_current_period_usage(self.resource)
        self.assertEqual(result["node"], 25.0)  # 10 + 15
