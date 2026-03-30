from rest_framework import test

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories


class OrderEstimateTest(test.APITestCase):
    def test_old_cost_estimate(self):
        # 1. Setup offering, plan, and components
        offering = factories.OfferingFactory()
        plan = factories.PlanFactory(offering=offering)

        # Add a component with price
        component = models.OfferingComponent.objects.create(
            offering=offering,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
            limit_amount=100,
        )
        models.PlanComponent.objects.create(
            plan=plan,
            component=component,
            price=10,
        )

        # 2. Create order with old_limits
        old_limits = {"cpu": 5}
        order = factories.OrderFactory(plan=plan, attributes={"old_limits": old_limits})

        # 3. Verify property
        # Expected cost = 5 * 10 = 50
        self.assertEqual(order.old_cost_estimate, 50)

    def test_old_cost_estimate_with_float_limits(self):
        """Limits from JSON attributes are stored as floats.
        Ensure get_estimate handles Decimal * float without TypeError."""
        offering = factories.OfferingFactory()
        plan = factories.PlanFactory(offering=offering)

        component = models.OfferingComponent.objects.create(
            offering=offering,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
            limit_amount=100,
        )
        models.PlanComponent.objects.create(
            plan=plan,
            component=component,
            price=10,
        )

        # Simulate JSON-deserialized limits (floats, not ints)
        old_limits = {"cpu": 5.0}
        order = factories.OrderFactory(plan=plan, attributes={"old_limits": old_limits})

        self.assertEqual(order.old_cost_estimate, 50)

    def test_old_cost_estimate_without_limits(self):
        offering = factories.OfferingFactory()
        plan = factories.PlanFactory(offering=offering)
        order = factories.OrderFactory(plan=plan, attributes={})

        self.assertEqual(order.old_cost_estimate, 0)
