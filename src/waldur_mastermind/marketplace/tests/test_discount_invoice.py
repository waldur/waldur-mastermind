from decimal import Decimal

from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import billing_discount
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods

from . import fixtures


@freeze_time("2024-01-15")  # A 31-day month
class VolumeDiscountTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.MarketplaceFixture()

        # Configure a limit-based component for testing discounts
        self.offering_component = self.fixture.offering_component
        self.offering_component.billing_type = BillingTypes.LIMIT
        self.offering_component.limit_period = LimitPeriods.MONTH
        self.offering_component.save()

        self.plan_component = self.fixture.plan_component
        self.plan_component.price = Decimal("10.0")
        self.plan_component.discount_formula = ""
        self.plan_component.save()

        self.resource = self.fixture.resource
        self.resource.limits = {self.offering_component.type: 5}
        self.resource.save()

    def _trigger_billing_and_get_items(self):
        self.resource.state = marketplace_models.ResourceStates.CREATING
        self.resource.save()
        self.resource.set_state_ok()
        self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2024, month=1
        )
        # Volume discounts are materialized at invoice finalization.
        billing_discount.apply_aggregated_volume_discounts(invoice)
        return invoice.items.filter(
            resource_id=self.resource.id,
            details__offering_component_type=self.offering_component.type,
        ).order_by("unit_price")

    def test_no_discount_item_is_created_if_not_configured(self):
        items = self._trigger_billing_and_get_items()

        self.assertEqual(items.count(), 1)
        main_item = items.first()
        self.assertEqual(main_item.quantity, 5)
        self.assertEqual(main_item.unit_price, self.plan_component.price)
        self.assertNotIn("is_discount", main_item.details)

    def test_no_discount_item_when_formula_yields_zero(self):
        self.plan_component.discount_formula = "20 if usage >= 10 else 0"
        self.plan_component.save()
        self.resource.limits = {self.offering_component.type: 9}  # Below threshold
        self.resource.save()

        items = self._trigger_billing_and_get_items()

        self.assertEqual(items.count(), 1)
        main_item = items.first()
        self.assertEqual(main_item.quantity, 9)
        self.assertNotIn("is_discount", main_item.details)
        self.assertEqual(main_item.price, self.plan_component.price * 9)

    def test_discount_is_applied_when_formula_yields_percent(self):
        self.plan_component.discount_formula = "20 if usage >= 10 else 0"
        self.plan_component.save()
        self.resource.limits = {self.offering_component.type: 10}
        self.resource.save()

        items = self._trigger_billing_and_get_items()

        self.assertEqual(items.count(), 2)
        discount_item = items.get(unit_price__lt=0)
        main_item = items.get(unit_price__gt=0)

        self.assertEqual(main_item.quantity, 10)
        self.assertEqual(main_item.unit_price, self.plan_component.price)

        self.assertTrue(discount_item.details["is_discount"])
        self.assertEqual(discount_item.details["discount_type"], "org_volume_discount")
        self.assertEqual(discount_item.details["discount_percent"], 20.0)
        self.assertEqual(discount_item.quantity, 1)

        # Discount applies to the whole line charge: 10 * 10 * 20% = 20.
        expected_discount = main_item.unit_price * main_item.quantity * Decimal("0.20")
        self.assertEqual(discount_item.unit_price, -expected_discount)

        total_price = main_item.price + discount_item.price
        self.assertEqual(
            total_price,
            main_item.unit_price * main_item.quantity - expected_discount,
        )

    def test_discount_applies_to_full_duration_multiplied_charge(self):
        """With PER_DAY billing the discount applies to the full line charge
        (price x duration-multiplied quantity); the formula still scales on the
        raw component value bound to `usage`."""
        plan = self.plan_component.plan
        plan.unit = marketplace_models.Plan.Units.PER_DAY
        plan.save()

        self.plan_component.discount_formula = "20 if usage >= 10 else 0"
        self.plan_component.save()
        self.resource.limits = {self.offering_component.type: 15}
        self.resource.save()

        items = self._trigger_billing_and_get_items()

        self.assertEqual(items.count(), 2)
        discount_item = items.get(unit_price__lt=0)
        main_item = items.get(unit_price__gt=0)

        # Main item quantity is multiplied by the number of days in the period.
        self.assertGreater(main_item.quantity, 15)
        self.assertEqual(main_item.unit_price, self.plan_component.price)

        # usage=15 -> 20% off the full line charge.
        expected_discount = main_item.unit_price * main_item.quantity * Decimal("0.20")
        self.assertEqual(discount_item.unit_price, -expected_discount)
        self.assertEqual(discount_item.details["aggregated_usage"], 15.0)

    def test_broken_formula_does_not_block_billing(self):
        # Domain error at the billed usage; billing must still produce the
        # main item and simply omit the discount.
        self.plan_component.discount_formula = "LN(usage - 100)"
        self.plan_component.save()
        self.resource.limits = {self.offering_component.type: 15}
        self.resource.save()

        items = self._trigger_billing_and_get_items()

        self.assertEqual(items.count(), 1)
        main_item = items.first()
        self.assertEqual(main_item.quantity, 15)
        self.assertNotIn("is_discount", main_item.details)
