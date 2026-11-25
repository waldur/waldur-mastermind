from decimal import Decimal

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
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


class PriceRenderingTest(test.APITestCase):
    """Prices render with the JSON types the schema declares.

    Waldur's renderer writes a Decimal as a string and an int or float as a number.
    The price properties sum Decimal prices, so a priced plan gave a Decimal and a free
    plan the int 0, and the JSON type depended on the data. Clients generated from the
    schema then failed to decode priced offerings and orders.
    """

    def setUp(self):
        self.offering = factories.OfferingFactory(shared=True)
        self.plan = factories.PlanFactory(offering=self.offering)
        prices = {
            BillingTypes.FIXED: ("ram", 10),
            BillingTypes.ONE_TIME: ("setup", 5),
            BillingTypes.ON_PLAN_SWITCH: ("switch", 3),
            BillingTypes.LIMIT: ("cpu", 2),
        }
        for billing_type, (component_type, price) in prices.items():
            component = models.OfferingComponent.objects.create(
                offering=self.offering,
                type=component_type,
                billing_type=billing_type,
                limit_period=LimitPeriods.MONTH,
            )
            models.PlanComponent.objects.create(
                plan=self.plan, component=component, price=price, amount=1
            )
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def _order_json(self, order):
        response = self.client.get(factories.OrderFactory.get_url(order))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def test_priced_plan(self):
        response = self.client.get(
            factories.OfferingFactory.get_public_url(self.offering)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan = response.json()["plans"][0]
        self.assertEqual(plan["init_price"], 5.0)
        self.assertIsInstance(plan["init_price"], float)
        self.assertEqual(plan["switch_price"], 3.0)
        self.assertIsInstance(plan["switch_price"], float)
        # LIMIT 2 + FIXED 10 + ONE_TIME 5, a decimal string as the schema declares.
        self.assertEqual(plan["minimal_price"], "17.0000000000")

    def test_priced_order(self):
        order = factories.OrderFactory(
            plan=self.plan, offering=self.offering, attributes={}
        )
        data = self._order_json(order)
        self.assertEqual(data["fixed_price"], 10.0)
        self.assertIsInstance(data["fixed_price"], float)
        self.assertIsInstance(data["activation_price"], float)

    def test_old_cost_snapshot(self):
        order = factories.OrderFactory(
            plan=self.plan, offering=self.offering, attributes={}
        )
        order.old_cost = Decimal("12.5")
        order.save(update_fields=["old_cost"])
        value = self._order_json(order)["old_cost_estimate"]
        self.assertIsInstance(value, float)
        self.assertEqual(value, 12.5)

    def test_zero_old_cost_snapshot(self):
        # The database returns Decimal("0E-10") for a zero snapshot.
        order = factories.OrderFactory(
            plan=self.plan, offering=self.offering, attributes={}
        )
        order.old_cost = Decimal("0E-10")
        order.save(update_fields=["old_cost"])
        value = self._order_json(order)["old_cost_estimate"]
        self.assertIsInstance(value, float)
        self.assertEqual(value, 0)

    def test_live_old_cost_estimate(self):
        # Without a snapshot the estimate is computed from old_limits, a Decimal.
        order = factories.OrderFactory(
            plan=self.plan,
            offering=self.offering,
            attributes={"old_limits": {"cpu": 5}},
        )
        order.old_cost = None
        order.save(update_fields=["old_cost"])
        self.assertIsInstance(order.old_cost_estimate, Decimal)
        value = self._order_json(order)["old_cost_estimate"]
        self.assertIsInstance(value, float)
        self.assertGreater(value, 0)

    def test_large_minimal_price(self):
        # 1e11 times 1e8 is 1e19, which needs 30 digits at 10 places: more than the
        # default decimal context allows, so quantizing in it raised InvalidOperation.
        component = self.plan.components.get(component__type="ram")
        component.price = Decimal(10) ** 11
        component.amount = 10**8
        component.save()
        response = self.client.get(
            factories.OfferingFactory.get_public_url(self.offering)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json()["plans"][0]["minimal_price"],
            "10000000000000000007.0000000000",
        )
