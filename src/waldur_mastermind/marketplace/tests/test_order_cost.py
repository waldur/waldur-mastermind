"""Tests for order.cost calculation across billing types.

Verifies that order.cost reflects the actual invoice total, including
the duration multiplier for prepaid components.
"""

import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from waldur_core.core.utils import calculate_duration_months
from waldur_mastermind.marketplace.enums import BillingTypes, OrderTypes
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
        """ONE_TIME (non-prepaid): an activation fee, charged once at its price.

        The plan's amount is not a multiplier here. The invoice bills a
        one-time component at quantity 1 whatever the amount says, and the
        estimate has to quote what the invoice will charge.
        """
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
        # non_prepaid_init_price: the price, once, whatever the amount
        self.assertEqual(order.cost, Decimal("10"))

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

        # init_price still measures per unit: 100*1 + 50*2 = 200
        self.assertEqual(plan.init_price, 200)
        # non_prepaid_init_price excludes the prepaid one and charges the
        # other once, as the invoice does: 50
        self.assertEqual(plan.non_prepaid_init_price, 50)
