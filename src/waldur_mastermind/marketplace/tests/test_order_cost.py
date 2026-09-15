"""Tests for order.cost calculation across billing types.

Verifies that order.cost reflects the actual invoice total, including
the duration multiplier for prepaid components.
"""

import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.core.utils import calculate_duration_months
from waldur_mastermind.marketplace.enums import BillingTypes, OrderTypes
from waldur_mastermind.marketplace.models import Order
from waldur_mastermind.marketplace.tests import factories


class CalculateDurationMonthsTest(TestCase):
    def test_exact_months(self):
        self.assertEqual(
            calculate_duration_months(
                datetime.date(2026, 1, 1), datetime.date(2026, 7, 1)
            ),
            6,
        )

    def test_partial_month_rounds_up(self):
        self.assertEqual(
            calculate_duration_months(
                datetime.date(2026, 1, 1), datetime.date(2026, 7, 15)
            ),
            7,
        )

    def test_one_year(self):
        self.assertEqual(
            calculate_duration_months(
                datetime.date(2026, 1, 1), datetime.date(2027, 1, 1)
            ),
            12,
        )

    def test_minimum_one_month(self):
        self.assertEqual(
            calculate_duration_months(
                datetime.date(2026, 1, 1), datetime.date(2026, 1, 10)
            ),
            1,
        )


class OrderCostCalculationTest(TestCase):
    """Verify order.cost for each billing type."""

    def _make_offering_with_component(self, billing_type, is_prepaid=False):
        offering = factories.OfferingFactory()
        component = factories.OfferingComponentFactory(
            offering=offering,
            type="cpu",
            billing_type=billing_type,
            is_prepaid=is_prepaid,
        )
        plan = factories.PlanFactory(offering=offering, unit_price=0)
        plan_component = factories.PlanComponentFactory(
            plan=plan,
            component=component,
            price=Decimal("10"),
            amount=5,
        )
        return offering, plan, component, plan_component

    def test_limit_component_cost(self):
        """LIMIT: cost = price x limit."""
        offering, plan, _, _ = self._make_offering_with_component(BillingTypes.LIMIT)
        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            limits={"cpu": 100},
            type=OrderTypes.CREATE,
        )
        order.init_cost()

        # get_estimate: 10 * 100 = 1000
        # non_prepaid_init_price: 0 (no non-prepaid ONE_TIME)
        self.assertEqual(order.cost, Decimal("1000"))

    def test_one_time_non_prepaid_cost(self):
        """ONE_TIME (non-prepaid): cost = price x amount (activation fee)."""
        offering, plan, _, _ = self._make_offering_with_component(
            BillingTypes.ONE_TIME, is_prepaid=False
        )
        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            limits={"cpu": 100},
            type=OrderTypes.CREATE,
        )
        order.init_cost()

        # get_estimate: 0 (non-prepaid ONE_TIME not in get_limit_components)
        # non_prepaid_init_price: 10 * 5 = 50
        self.assertEqual(order.cost, Decimal("50"))

    def test_one_time_prepaid_without_end_date(self):
        """ONE_TIME + prepaid without end_date: single-period cost."""
        offering, plan, _, _ = self._make_offering_with_component(
            BillingTypes.ONE_TIME, is_prepaid=True
        )
        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            limits={"cpu": 100},
            type=OrderTypes.CREATE,
        )
        order.init_cost()

        # get_estimate: 10 * 100 = 1000 (no duration, no end_date)
        # non_prepaid_init_price: 0 (the ONE_TIME component IS prepaid)
        self.assertEqual(order.cost, Decimal("1000"))

    def test_one_time_prepaid_with_duration(self):
        """ONE_TIME + prepaid with end_date: cost includes duration multiplier."""
        offering, plan, _, _ = self._make_offering_with_component(
            BillingTypes.ONE_TIME, is_prepaid=True
        )

        end_date = (timezone.now().date() + datetime.timedelta(days=365)).isoformat()
        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            limits={"cpu": 100},
            attributes={"name": "prepaid-test", "end_date": end_date},
            type=OrderTypes.CREATE,
        )
        order.resource.end_date = datetime.date.fromisoformat(end_date)
        order.resource.save()
        order.init_cost()

        # ~12 months duration
        expected_months = calculate_duration_months(
            timezone.now().date(), datetime.date.fromisoformat(end_date)
        )
        # get_estimate: 10 * 100 * months
        # non_prepaid_init_price: 0
        self.assertEqual(order.cost, Decimal("1000") * expected_months)

    def test_fixed_component_cost(self):
        """FIXED: not included in order.cost (billed monthly via invoices)."""
        offering, plan, _, _ = self._make_offering_with_component(BillingTypes.FIXED)
        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            limits={},
            type=OrderTypes.CREATE,
        )
        order.init_cost()

        self.assertEqual(order.cost, Decimal("0"))

    def test_mixed_billing_types(self):
        """Order with LIMIT + ONE_TIME(prepaid) + FIXED components."""
        offering = factories.OfferingFactory()

        limit_comp = factories.OfferingComponentFactory(
            offering=offering, type="ram", billing_type=BillingTypes.LIMIT
        )
        prepaid_comp = factories.OfferingComponentFactory(
            offering=offering,
            type="cpu",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )
        fixed_comp = factories.OfferingComponentFactory(
            offering=offering, type="storage", billing_type=BillingTypes.FIXED
        )

        plan = factories.PlanFactory(offering=offering, unit_price=5)
        factories.PlanComponentFactory(
            plan=plan, component=limit_comp, price=Decimal("2"), amount=0
        )
        factories.PlanComponentFactory(
            plan=plan, component=prepaid_comp, price=Decimal("10"), amount=1
        )
        factories.PlanComponentFactory(
            plan=plan, component=fixed_comp, price=Decimal("20"), amount=1
        )

        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            limits={"ram": 50, "cpu": 100},
            type=OrderTypes.CREATE,
        )
        order.init_cost()

        # unit_price: 5
        # ram (LIMIT): 2 * 50 = 100
        # cpu (ONE_TIME+prepaid, no end_date): 10 * 100 = 1000
        # FIXED: not included
        # non_prepaid_init_price: 0 (only ONE_TIME component is prepaid)
        self.assertEqual(order.cost, Decimal("1105"))


class OldCostEstimateTest(TestCase):
    """Verify old_cost_estimate scales like the new estimate for limit updates.

    A plain "Update resource limits" order carries no end_date/duration of
    its own -- only a renewal order does (via the old_end_date attribute).
    old_cost_estimate must fall back to the resource's real end_date, the
    same window init_cost() uses for the new estimate, so the delta between
    them reflects the actual one-time top-up charge for the changed limit,
    scaled by the real remaining months -- matching what
    MarketplaceBillingService._handle_prepaid_limits_change will invoice, in
    the common case. Billing and this estimate can still disagree: billing
    skips a component entirely when the old limit is 0 or there's no
    end_date, and charges nothing once end_date has passed, while this
    estimate has no "nothing to charge" case of its own (calculate_duration_
    months floors at 1 month, and an absent end_date just skips scaling
    rather than skipping the component). Pre-existing behavior, not
    introduced by this fix, and not addressed here.
    """

    @freeze_time("2026-09-15")
    def test_prepaid_component_delta_matches_remaining_months(self):
        offering = factories.OfferingFactory()
        component = factories.OfferingComponentFactory(
            offering=offering,
            type="storage",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )
        plan = factories.PlanFactory(offering=offering, unit_price=0)
        factories.PlanComponentFactory(
            plan=plan,
            component=component,
            price=Decimal("2"),
            amount=1,
        )

        end_date = timezone.now().date() + datetime.timedelta(days=180)
        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            limits={"storage": 10},
            attributes={"name": "limits-update-test", "old_limits": {"storage": 1}},
            type=OrderTypes.UPDATE,
        )
        order.resource.end_date = end_date
        order.resource.save()
        order.init_cost()

        expected_months = calculate_duration_months(timezone.now().date(), end_date)

        # New estimate: price * new_limit * months = 2 * 10 * months
        self.assertEqual(order.cost, Decimal("2") * 10 * expected_months)
        # Old estimate must use the same window, not the undiluted price
        self.assertEqual(order.old_cost_estimate, Decimal("2") * 1 * expected_months)
        # Delta isolates just the limit change over the remaining months
        self.assertEqual(
            order.cost - order.old_cost_estimate,
            Decimal("2") * (10 - 1) * expected_months,
        )

    def test_renewal_still_uses_old_end_date(self):
        """Renewals keep pricing the old subscription over its own duration.

        Also confirms init_cost() snapshots the renewal branch's result into
        old_cost, not just that the live computation is right -- the earlier
        version of this test read old_cost_estimate straight off a factory-
        built order without ever calling init_cost(), so it only exercised
        the live-fallback path (old_cost still None) and would have passed
        even if init_cost() never snapshotted renewals at all.
        """
        offering = factories.OfferingFactory()
        component = factories.OfferingComponentFactory(
            offering=offering,
            type="storage",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )
        plan = factories.PlanFactory(offering=offering, unit_price=0)
        factories.PlanComponentFactory(
            plan=plan,
            component=component,
            price=Decimal("2"),
            amount=1,
        )

        old_end_date = timezone.now().date() + datetime.timedelta(days=30)
        new_end_date = timezone.now().date() + datetime.timedelta(days=395)
        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            limits={"storage": 10},
            attributes={
                "name": "renewal-test",
                "old_limits": {"storage": 10},
                "old_end_date": old_end_date.isoformat(),
            },
            type=OrderTypes.UPDATE,
        )
        order.resource.end_date = new_end_date
        order.resource.save()
        order.init_cost()

        expected_months = calculate_duration_months(
            order.resource.created.date()
            if hasattr(order.resource.created, "date")
            else order.resource.created,
            old_end_date,
        )
        expected = Decimal("2") * 10 * expected_months
        # The renewal branch's result was actually snapshotted, not left for
        # a live fallback to (coincidentally) get right.
        self.assertEqual(order.old_cost, expected)
        self.assertEqual(order.old_cost_estimate, expected)


class OldCostEstimateSnapshotTest(TestCase):
    """old_cost_estimate must not drift after the order was created.

    Before this fix, the property recomputed live on every read, pricing the
    old limits from "today" through resource.end_date. Since `cost` (the new
    estimate) is computed once at creation and never moves, but "today" keeps
    advancing every time old_cost_estimate is read, the same order's shown
    "cost change" grew the longer it sat before anyone looked at it -- e.g.
    review, or approval delayed a few months. init_cost() now snapshots
    old_cost_estimate into the old_cost field at creation, and the property
    reads that back instead of recomputing.
    """

    def test_reading_the_order_later_returns_the_same_figure(self):
        offering = factories.OfferingFactory()
        component = factories.OfferingComponentFactory(
            offering=offering,
            type="storage",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )
        plan = factories.PlanFactory(offering=offering, unit_price=0)
        factories.PlanComponentFactory(
            plan=plan,
            component=component,
            price=Decimal("2"),
            amount=1,
        )
        end_date = datetime.date(2027, 3, 11)

        with freeze_time("2026-09-15"):
            order = factories.OrderFactory(
                offering=offering,
                plan=plan,
                limits={"storage": 10},
                attributes={"name": "drift-test", "old_limits": {"storage": 1}},
                type=OrderTypes.UPDATE,
            )
            order.resource.end_date = end_date
            order.resource.save()
            order.init_cost()
            order.save()

            # 6 months from 2026-09-15 to 2027-03-11 (matches
            # calculate_duration_months's rounding).
            self.assertEqual(order.cost, Decimal("120"))
            self.assertEqual(order.old_cost_estimate, Decimal("12"))

        # Three months pass. Nobody touched the order. A different request
        # fetches a fresh instance from the database, the way a real API
        # read would.
        with freeze_time("2026-12-15"):
            reread = Order.objects.get(pk=order.pk)

            self.assertEqual(reread.cost, Decimal("120"))
            # Live recomputation from "today" (2026-12-15) would give only
            # 3 remaining months -- 2 * 1 * 3 = 6 -- and a growing cost
            # change. The snapshot must still read back 12.
            self.assertEqual(reread.old_cost_estimate, Decimal("12"))
            self.assertEqual(reread.cost - reread.old_cost_estimate, Decimal("108"))

    def test_legacy_order_without_a_snapshot_falls_back_to_live_computation(self):
        """Orders created before the old_cost field existed have no snapshot.

        They keep the old (live, drifting) behavior rather than silently
        showing zero -- there's no historical snapshot to read back for them.
        """
        offering = factories.OfferingFactory()
        component = factories.OfferingComponentFactory(
            offering=offering,
            type="storage",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )
        plan = factories.PlanFactory(offering=offering, unit_price=0)
        factories.PlanComponentFactory(
            plan=plan,
            component=component,
            price=Decimal("2"),
            amount=1,
        )
        end_date = timezone.now().date() + datetime.timedelta(days=180)
        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            limits={"storage": 10},
            attributes={"name": "legacy-test", "old_limits": {"storage": 1}},
            type=OrderTypes.UPDATE,
        )
        order.resource.end_date = end_date
        order.resource.save()
        # Simulate a pre-migration row: no snapshot was ever taken.
        Order.objects.filter(pk=order.pk).update(old_cost=None)
        order.refresh_from_db()

        expected_months = calculate_duration_months(timezone.now().date(), end_date)
        self.assertEqual(order.old_cost_estimate, Decimal("2") * 1 * expected_months)


class NonPrepaidInitPriceTest(TestCase):
    def test_excludes_prepaid_components(self):
        offering = factories.OfferingFactory()
        prepaid = factories.OfferingComponentFactory(
            offering=offering,
            type="cpu",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )
        non_prepaid = factories.OfferingComponentFactory(
            offering=offering,
            type="setup",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=False,
        )
        plan = factories.PlanFactory(offering=offering)
        factories.PlanComponentFactory(
            plan=plan, component=prepaid, price=Decimal("100"), amount=1
        )
        factories.PlanComponentFactory(
            plan=plan, component=non_prepaid, price=Decimal("50"), amount=2
        )

        # init_price includes both: 100*1 + 50*2 = 200
        self.assertEqual(plan.init_price, 200)
        # non_prepaid_init_price excludes prepaid: 50*2 = 100
        self.assertEqual(plan.non_prepaid_init_price, 100)
