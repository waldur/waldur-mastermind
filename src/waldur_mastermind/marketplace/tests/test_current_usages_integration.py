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

from waldur_core.structure.tests import factories as structure_factories
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
    _resolve_period_bounds,
    get_components_usage_data,
    get_components_usage_data_per_offering,
    get_current_period_usage,
    get_offering_usage_by_project,
    get_offering_usage_timeseries,
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


# ---------------------------------------------------------------------------
# 8. Per-offering stats (HPCMP-472 fix)
# ---------------------------------------------------------------------------


@freeze_time("2026-05-15")
class PerOfferingStatsTest(test.APITestCase):
    """Verify get_components_usage_data_per_offering emits one row per
    (offering, component_type, billing_type) and that each row reports
    period-correct usage using the offering's own limit_period."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    def _create_offering_with_component(
        self, billing_type, limit_period, component_type="node", limits=None
    ):
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        plan = factories.PlanFactory(offering=offering)
        component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=billing_type,
            type=component_type,
            name="Compute",
            measured_unit="node hours",
            limit_period=limit_period,
        )
        factories.PlanComponentFactory(plan=plan, component=component)
        # Distinguish "no limits at all" (empty dict) from "no override"
        # (None). An empty dict must be preserved so we can test the
        # "limit is None when no resource sets it" path.
        effective_limits = {component_type: 1000} if limits is None else limits
        resource = models.Resource.objects.create(
            offering=offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits=effective_limits,
        )
        factories.OrderFactory(
            resource=resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=plan,
        )
        callbacks.resource_creation_succeeded(resource)
        return offering, component, resource

    def test_emits_one_row_per_offering(self):
        offering1, component1, resource1 = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.MONTH
        )
        offering2, component2, resource2 = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.QUARTERLY
        )

        _create_usage(resource1, component1, 100)
        _create_usage(resource2, component2, 500)

        rows = get_components_usage_data_per_offering(
            models.Resource.objects.filter(pk__in=[resource1.pk, resource2.pk])
        )

        self.assertEqual(len(rows), 2)
        offering_uuids = {row["offering_uuid"] for row in rows}
        self.assertEqual(offering_uuids, {offering1.uuid.hex, offering2.uuid.hex})

    def test_quarterly_offering_reports_quarter_to_date(self):
        # May 15 → Q2 starts Apr 1
        offering, component, resource = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.QUARTERLY
        )
        # April (in-quarter), May (in-quarter, current month), March (out-of-quarter)
        _create_usage(
            resource, component, 100, billing_period=datetime.date(2026, 3, 1)
        )
        _create_usage(
            resource, component, 200, billing_period=datetime.date(2026, 4, 1)
        )
        _create_usage(
            resource, component, 300, billing_period=datetime.date(2026, 5, 1)
        )

        rows = get_components_usage_data_per_offering(
            models.Resource.objects.filter(pk=resource.pk)
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["limit_usage"], 500.0)  # April + May only
        self.assertEqual(row["limit_period"], LimitPeriods.QUARTERLY)
        self.assertEqual(row["current_period_label"], "Q2 2026")
        self.assertEqual(row["current_period_start"], datetime.date(2026, 4, 1))
        # Quarter end is last day of June
        self.assertEqual(row["current_period_end"], datetime.date(2026, 6, 30))

    def test_annual_offering_reports_year_to_date(self):
        offering, component, resource = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.ANNUAL
        )
        _create_usage(
            resource, component, 100, billing_period=datetime.date(2025, 12, 1)
        )
        _create_usage(
            resource, component, 200, billing_period=datetime.date(2026, 1, 1)
        )
        _create_usage(
            resource, component, 300, billing_period=datetime.date(2026, 5, 1)
        )

        rows = get_components_usage_data_per_offering(
            models.Resource.objects.filter(pk=resource.pk)
        )

        row = rows[0]
        self.assertEqual(row["limit_usage"], 500.0)  # Jan + May only (2026)
        self.assertEqual(row["current_period_label"], "2026")
        self.assertEqual(row["current_period_start"], datetime.date(2026, 1, 1))
        self.assertEqual(row["current_period_end"], datetime.date(2026, 12, 31))

    def test_total_offering_reports_lifetime(self):
        offering, component, resource = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.TOTAL
        )
        _create_usage(
            resource, component, 100, billing_period=datetime.date(2024, 1, 1)
        )
        _create_usage(
            resource, component, 200, billing_period=datetime.date(2025, 6, 1)
        )
        _create_usage(
            resource, component, 300, billing_period=datetime.date(2026, 5, 1)
        )

        rows = get_components_usage_data_per_offering(
            models.Resource.objects.filter(pk=resource.pk)
        )

        row = rows[0]
        self.assertEqual(row["limit_usage"], 600.0)
        self.assertEqual(row["current_period_label"], "Total")
        self.assertIsNone(row["current_period_start"])
        self.assertIsNone(row["current_period_end"])

    def test_monthly_offering_reports_current_month(self):
        offering, component, resource = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.MONTH
        )
        _create_usage(
            resource, component, 100, billing_period=datetime.date(2026, 4, 1)
        )
        _create_usage(
            resource, component, 200, billing_period=datetime.date(2026, 5, 1)
        )

        rows = get_components_usage_data_per_offering(
            models.Resource.objects.filter(pk=resource.pk)
        )

        row = rows[0]
        self.assertEqual(row["limit_usage"], 200.0)
        self.assertEqual(row["current_period_label"], "May 2026")
        self.assertEqual(row["current_period_start"], datetime.date(2026, 5, 1))
        self.assertEqual(row["current_period_end"], datetime.date(2026, 5, 1))

    def test_usage_billing_separates_from_limit_billing(self):
        """A USAGE-billing row reports period_usage in `usage`; a
        LIMIT-billing row reports it in `limit_usage`."""
        offering1, comp1, res1 = self._create_offering_with_component(
            BillingTypes.USAGE, LimitPeriods.MONTH, component_type="cpu"
        )
        offering2, comp2, res2 = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.MONTH, component_type="ram"
        )
        _create_usage(res1, comp1, 10)
        _create_usage(res2, comp2, 20)

        rows = get_components_usage_data_per_offering(
            models.Resource.objects.filter(pk__in=[res1.pk, res2.pk])
        )

        usage_row = next(r for r in rows if r["billing_type"] == BillingTypes.USAGE)
        limit_row = next(r for r in rows if r["billing_type"] == BillingTypes.LIMIT)
        self.assertEqual(usage_row["usage"], 10.0)
        self.assertEqual(usage_row["limit_usage"], 0.0)
        self.assertEqual(limit_row["usage"], 0.0)
        self.assertEqual(limit_row["limit_usage"], 20.0)

    def test_offering_uuids_are_distinct_when_component_types_collide(self):
        """Two offerings exposing the same component type must each get
        their own row with the correct offering_uuid."""
        off1, comp1, res1 = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.MONTH, component_type="node"
        )
        off2, comp2, res2 = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.QUARTERLY, component_type="node"
        )
        _create_usage(res1, comp1, 100)
        _create_usage(res2, comp2, 500)

        rows = get_components_usage_data_per_offering(
            models.Resource.objects.filter(pk__in=[res1.pk, res2.pk])
        )

        node_rows = [r for r in rows if r["type"] == "node"]
        self.assertEqual(len(node_rows), 2)
        by_uuid = {r["offering_uuid"]: r for r in node_rows}
        self.assertEqual(by_uuid[off1.uuid.hex]["limit_usage"], 100.0)
        self.assertEqual(by_uuid[off2.uuid.hex]["limit_usage"], 500.0)
        self.assertEqual(by_uuid[off1.uuid.hex]["limit_period"], LimitPeriods.MONTH)
        self.assertEqual(by_uuid[off2.uuid.hex]["limit_period"], LimitPeriods.QUARTERLY)

    def test_returns_empty_list_for_no_resources(self):
        rows = get_components_usage_data_per_offering(models.Resource.objects.none())
        self.assertEqual(rows, [])

    def test_limit_is_summed_across_resources_of_same_offering(self):
        offering, component, resource1 = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.MONTH, limits={"node": 1000}
        )
        # second resource on the same offering, different limit
        plan = resource1.plan
        resource2 = models.Resource.objects.create(
            offering=offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={"node": 500},
        )
        factories.OrderFactory(
            resource=resource2,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=plan,
        )
        callbacks.resource_creation_succeeded(resource2)

        rows = get_components_usage_data_per_offering(
            models.Resource.objects.filter(pk__in=[resource1.pk, resource2.pk])
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["limit"], 1500.0)

    def test_limit_is_none_when_no_resource_sets_it(self):
        offering, component, resource = self._create_offering_with_component(
            BillingTypes.USAGE, LimitPeriods.MONTH, limits={}
        )
        rows = get_components_usage_data_per_offering(
            models.Resource.objects.filter(pk=resource.pk)
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["limit"])


# ---------------------------------------------------------------------------
# 9. Per-offering usage timeseries (HPCMP-472 Stage 2)
# ---------------------------------------------------------------------------


@freeze_time("2026-05-15")
class OfferingUsageTimeSeriesTest(test.APITestCase):
    """Verify get_offering_usage_timeseries returns monthly buckets within
    the offering's current limit_period."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    def _create_offering_with_component(
        self, billing_type, limit_period, component_type="node", limits=None
    ):
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        plan = factories.PlanFactory(offering=offering)
        component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=billing_type,
            type=component_type,
            name="Compute",
            measured_unit="node hours",
            limit_period=limit_period,
        )
        factories.PlanComponentFactory(plan=plan, component=component)
        resource = models.Resource.objects.create(
            offering=offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits=limits or {component_type: 1000},
        )
        factories.OrderFactory(
            resource=resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=plan,
        )
        callbacks.resource_creation_succeeded(resource)
        return offering, component, resource

    def test_quarterly_returns_in_period_monthly_buckets(self):
        offering, component, resource = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.QUARTERLY
        )
        # March (out of Q2) + April + May (in Q2)
        _create_usage(
            resource, component, 100, billing_period=datetime.date(2026, 3, 1)
        )
        _create_usage(
            resource, component, 200, billing_period=datetime.date(2026, 4, 1)
        )
        _create_usage(
            resource, component, 300, billing_period=datetime.date(2026, 5, 1)
        )

        data = get_offering_usage_timeseries(
            models.Resource.objects.filter(pk=resource.pk), offering
        )
        self.assertEqual(data["current_period_label"], "Q2 2026")
        self.assertEqual(len(data["buckets"]), 2)
        self.assertEqual(
            data["buckets"][0]["billing_period"], datetime.date(2026, 4, 1)
        )
        self.assertEqual(data["buckets"][0]["usage"], 200.0)
        self.assertEqual(
            data["buckets"][1]["billing_period"], datetime.date(2026, 5, 1)
        )
        self.assertEqual(data["buckets"][1]["usage"], 300.0)

    def test_annual_returns_year_to_date_buckets(self):
        offering, component, resource = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.ANNUAL
        )
        # Dec 2025 (out of year) + Jan-May 2026 (in year)
        for m, day in enumerate([12, 1, 2, 3, 4, 5]):
            year = 2025 if m == 0 else 2026
            _create_usage(
                resource, component, 50, billing_period=datetime.date(year, day, 1)
            )

        data = get_offering_usage_timeseries(
            models.Resource.objects.filter(pk=resource.pk), offering
        )
        self.assertEqual(data["current_period_label"], "2026")
        self.assertEqual(len(data["buckets"]), 5)
        # First bucket is Jan 2026, not Dec 2025
        self.assertEqual(
            data["buckets"][0]["billing_period"], datetime.date(2026, 1, 1)
        )

    def test_total_returns_all_buckets(self):
        offering, component, resource = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.TOTAL
        )
        _create_usage(
            resource, component, 50, billing_period=datetime.date(2025, 12, 1)
        )
        _create_usage(resource, component, 50, billing_period=datetime.date(2026, 5, 1))

        data = get_offering_usage_timeseries(
            models.Resource.objects.filter(pk=resource.pk), offering
        )
        self.assertEqual(data["current_period_label"], "Total")
        self.assertEqual(data["current_period_start"], None)
        self.assertEqual(len(data["buckets"]), 2)

    def test_aggregates_across_multiple_resources(self):
        # Two resources of the same offering, both in the same project
        offering, component, resource1 = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.QUARTERLY
        )
        plan = resource1.plan
        resource2 = models.Resource.objects.create(
            offering=offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={"node": 500},
        )
        factories.OrderFactory(
            resource=resource2,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=plan,
        )
        callbacks.resource_creation_succeeded(resource2)

        _create_usage(
            resource1, component, 200, billing_period=datetime.date(2026, 5, 1)
        )
        _create_usage(
            resource2, component, 300, billing_period=datetime.date(2026, 5, 1)
        )

        data = get_offering_usage_timeseries(
            models.Resource.objects.filter(offering=offering), offering
        )
        self.assertEqual(len(data["buckets"]), 1)
        self.assertEqual(data["buckets"][0]["usage"], 500.0)
        # Limits sum across resources (1000 + 500)
        self.assertEqual(data["limit"], 1500.0)

    def test_returns_none_when_offering_has_no_components(self):
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        data = get_offering_usage_timeseries(models.Resource.objects.none(), offering)
        self.assertIsNone(data)

    def test_monthly_returns_rolling_six_months(self):
        offering, component, resource = self._create_offering_with_component(
            BillingTypes.LIMIT, LimitPeriods.MONTH
        )
        # Eight months of usage; only the last 6 (Dec 2025 - May 2026) should
        # appear because the monthly cap resets each month.
        for year, month in [
            (2025, 10),
            (2025, 11),
            (2025, 12),
            (2026, 1),
            (2026, 2),
            (2026, 3),
            (2026, 4),
            (2026, 5),
        ]:
            _create_usage(
                resource, component, 50, billing_period=datetime.date(year, month, 1)
            )

        data = get_offering_usage_timeseries(
            models.Resource.objects.filter(pk=resource.pk), offering
        )
        self.assertEqual(data["current_period_label"], "Last 6 months")
        self.assertEqual(len(data["buckets"]), 6)
        # First bucket is Dec 2025 (today=2026-05-15 minus 5 months).
        self.assertEqual(
            data["buckets"][0]["billing_period"], datetime.date(2025, 12, 1)
        )
        self.assertEqual(
            data["buckets"][-1]["billing_period"], datetime.date(2026, 5, 1)
        )


# ---------------------------------------------------------------------------
# 10. Period offset (shifting the timeseries window backward)
# ---------------------------------------------------------------------------


@freeze_time("2026-05-15")
class PeriodOffsetTest(test.APITestCase):
    """Verify _resolve_period_bounds correctly shifts the window for negative
    period_offset across quarter / year / month rollovers."""

    def test_quarterly_offset_minus_one_returns_q1(self):
        start, end, label = _resolve_period_bounds(
            LimitPeriods.QUARTERLY, datetime.date(2026, 5, 15), period_offset=-1
        )
        self.assertEqual(start, datetime.date(2026, 1, 1))
        self.assertEqual(end, datetime.date(2026, 3, 31))
        self.assertEqual(label, "Q1 2026")

    def test_quarterly_offset_minus_two_rolls_year(self):
        start, end, label = _resolve_period_bounds(
            LimitPeriods.QUARTERLY, datetime.date(2026, 5, 15), period_offset=-2
        )
        self.assertEqual(start, datetime.date(2025, 10, 1))
        self.assertEqual(end, datetime.date(2025, 12, 31))
        self.assertEqual(label, "Q4 2025")

    def test_month_offset_minus_one_returns_april(self):
        start, end, label = _resolve_period_bounds(
            LimitPeriods.MONTH, datetime.date(2026, 5, 15), period_offset=-1
        )
        self.assertEqual(start, datetime.date(2026, 4, 1))
        self.assertEqual(end, datetime.date(2026, 4, 1))
        self.assertEqual(label, "Apr 2026")

    def test_month_offset_minus_five_rolls_year(self):
        start, _end, label = _resolve_period_bounds(
            LimitPeriods.MONTH, datetime.date(2026, 5, 15), period_offset=-5
        )
        self.assertEqual(start, datetime.date(2025, 12, 1))
        self.assertEqual(label, "Dec 2025")

    def test_annual_offset_minus_one_returns_previous_year(self):
        start, end, label = _resolve_period_bounds(
            LimitPeriods.ANNUAL, datetime.date(2026, 5, 15), period_offset=-1
        )
        self.assertEqual(start, datetime.date(2025, 1, 1))
        self.assertEqual(end, datetime.date(2025, 12, 31))
        self.assertEqual(label, "2025")

    def test_total_ignores_offset(self):
        start, end, label = _resolve_period_bounds(
            LimitPeriods.TOTAL, datetime.date(2026, 5, 15), period_offset=-3
        )
        self.assertIsNone(start)
        self.assertIsNone(end)
        self.assertEqual(label, "Total")


# ---------------------------------------------------------------------------
# 11. Per-project usage breakdown
# ---------------------------------------------------------------------------


@freeze_time("2026-05-15")
class OfferingUsageByProjectTest(test.APITestCase):
    """Verify get_offering_usage_by_project groups, sorts, and bounds usage
    correctly for the customer-scope decomposition view."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            type="node",
            name="Compute",
            measured_unit="node hours",
            limit_period=LimitPeriods.QUARTERLY,
        )
        factories.PlanComponentFactory(plan=self.plan, component=self.component)

    def _create_resource(self, project, limit=1000):
        resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=project,
            state=ResourceStates.OK,
            limits={"node": limit},
        )
        factories.OrderFactory(
            resource=resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=self.plan,
        )
        callbacks.resource_creation_succeeded(resource)
        return resource

    def test_groups_and_sorts_projects_by_usage_desc(self):
        project_a = structure_factories.ProjectFactory(
            customer=self.customer, name="alpha"
        )
        project_b = structure_factories.ProjectFactory(
            customer=self.customer, name="beta"
        )
        resource_a = self._create_resource(project_a)
        resource_b = self._create_resource(project_b)
        # alpha has 100 + 200 = 300; beta has 500
        _create_usage(
            resource_a, self.component, 100, billing_period=datetime.date(2026, 4, 1)
        )
        _create_usage(
            resource_a, self.component, 200, billing_period=datetime.date(2026, 5, 1)
        )
        _create_usage(
            resource_b, self.component, 500, billing_period=datetime.date(2026, 5, 1)
        )

        data = get_offering_usage_by_project(
            models.Resource.objects.filter(offering=self.offering), self.offering
        )
        self.assertEqual(data["total_usage"], 800.0)
        self.assertEqual(len(data["projects"]), 2)
        self.assertEqual(data["projects"][0]["project_name"], "beta")
        self.assertEqual(data["projects"][0]["usage"], 500.0)
        self.assertEqual(data["projects"][1]["project_name"], "alpha")
        self.assertEqual(data["projects"][1]["usage"], 300.0)
        # alpha has two monthly buckets, beta has one
        self.assertEqual(len(data["projects"][1]["buckets"]), 2)
        self.assertEqual(len(data["projects"][0]["buckets"]), 1)
        # Limits sum across resources
        self.assertEqual(data["limit"], 2000.0)

    def test_excludes_usage_outside_period(self):
        project = structure_factories.ProjectFactory(customer=self.customer)
        resource = self._create_resource(project)
        # March is Q1, May is Q2
        _create_usage(
            resource, self.component, 100, billing_period=datetime.date(2026, 3, 1)
        )
        _create_usage(
            resource, self.component, 50, billing_period=datetime.date(2026, 5, 1)
        )

        data = get_offering_usage_by_project(
            models.Resource.objects.filter(offering=self.offering), self.offering
        )
        self.assertEqual(data["total_usage"], 50.0)
        self.assertEqual(data["projects"][0]["usage"], 50.0)
        self.assertEqual(len(data["projects"][0]["buckets"]), 1)

    def test_omits_projects_without_in_period_usage(self):
        project_a = structure_factories.ProjectFactory(
            customer=self.customer, name="alpha"
        )
        project_b = structure_factories.ProjectFactory(
            customer=self.customer, name="beta"
        )
        resource_a = self._create_resource(project_a)
        self._create_resource(project_b)  # no usage at all
        _create_usage(
            resource_a, self.component, 100, billing_period=datetime.date(2026, 5, 1)
        )

        data = get_offering_usage_by_project(
            models.Resource.objects.filter(offering=self.offering), self.offering
        )
        # Only alpha appears; beta has no ComponentUsage rows
        self.assertEqual(len(data["projects"]), 1)
        self.assertEqual(data["projects"][0]["project_name"], "alpha")

    def test_period_offset_returns_previous_quarter(self):
        project = structure_factories.ProjectFactory(customer=self.customer)
        resource = self._create_resource(project)
        # Q1: Feb usage; Q2: May usage
        _create_usage(
            resource, self.component, 80, billing_period=datetime.date(2026, 2, 1)
        )
        _create_usage(
            resource, self.component, 50, billing_period=datetime.date(2026, 5, 1)
        )

        data = get_offering_usage_by_project(
            models.Resource.objects.filter(offering=self.offering),
            self.offering,
            period_offset=-1,
        )
        self.assertEqual(data["current_period_label"], "Q1 2026")
        self.assertEqual(data["total_usage"], 80.0)
        self.assertEqual(data["projects"][0]["usage"], 80.0)

    def test_returns_none_when_offering_has_no_matching_component(self):
        offering = factories.OfferingFactory(customer=self.customer)
        data = get_offering_usage_by_project(models.Resource.objects.none(), offering)
        self.assertIsNone(data)
