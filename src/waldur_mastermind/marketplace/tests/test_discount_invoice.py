from decimal import Decimal

from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
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
        self.plan_component.discount_threshold = None
        self.plan_component.discount_rate = None
        self.plan_component.save()

        self.resource = self.fixture.resource
        self.resource.limits = {self.offering_component.type: 5}
        self.resource.save()

    def _trigger_billing_and_get_items(self):
        # Transition resource to OK state to trigger invoice creation
        self.resource.state = marketplace_models.ResourceStates.CREATING
        self.resource.save()
        self.resource.set_state_ok()
        self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2024, month=1
        )
        # Return all items related to this specific component
        return invoice.items.filter(
            resource_id=self.resource.id,
            details__offering_component_type=self.offering_component.type,
        ).order_by("unit_price")

    def test_no_discount_item_is_created_if_not_configured(self):
        # Act
        items = self._trigger_billing_and_get_items()

        # Assert
        self.assertEqual(items.count(), 1)
        main_item = items.first()
        self.assertEqual(main_item.quantity, 5)
        self.assertEqual(main_item.unit_price, self.plan_component.price)
        self.assertNotIn("is_discount", main_item.details)

    def test_no_discount_item_is_created_if_threshold_is_not_met(self):
        # Arrange
        self.plan_component.discount_threshold = 10
        self.plan_component.discount_rate = 20  # 20%
        self.plan_component.save()
        self.resource.limits = {self.offering_component.type: 9}  # Below threshold
        self.resource.save()

        # Act
        items = self._trigger_billing_and_get_items()

        # Assert
        self.assertEqual(items.count(), 1)
        main_item = items.first()
        self.assertEqual(main_item.quantity, 9)
        self.assertNotIn("is_discount", main_item.details)
        self.assertEqual(main_item.price, self.plan_component.price * 9)

    def test_discount_is_applied_when_threshold_is_exactly_met(self):
        # Arrange
        self.plan_component.discount_threshold = 10
        self.plan_component.discount_rate = 20  # 20%
        self.plan_component.save()
        self.resource.limits = {
            self.offering_component.type: 10
        }  # Exactly at threshold
        self.resource.save()

        # Act
        items = self._trigger_billing_and_get_items()

        # Assert
        self.assertEqual(items.count(), 2)

        discount_item = items.get(unit_price__lt=0)
        main_item = items.get(unit_price__gt=0)

        # Main item assertions
        self.assertEqual(main_item.quantity, 10)
        self.assertEqual(main_item.unit_price, self.plan_component.price)

        # Discount item assertions
        self.assertTrue(discount_item.details["is_discount"])
        self.assertEqual(discount_item.quantity, 1)

        total_before_discount = main_item.unit_price * main_item.quantity
        expected_discount = total_before_discount * (
            Decimal(self.plan_component.discount_rate) / 100
        )
        self.assertEqual(discount_item.unit_price, -expected_discount)

        # Total price assertions
        total_price = main_item.price + discount_item.price
        expected_total_price = total_before_discount - expected_discount
        self.assertEqual(total_price, expected_total_price)

    def test_discount_is_applied_when_threshold_is_exceeded(self):
        # Arrange
        self.plan_component.discount_threshold = 10
        self.plan_component.discount_rate = 20  # 20%
        self.plan_component.save()
        self.resource.limits = {self.offering_component.type: 15}  # Above threshold
        self.resource.save()

        # Act
        items = self._trigger_billing_and_get_items()

        # Assert
        self.assertEqual(items.count(), 2)

        discount_item = items.get(unit_price__lt=0)
        main_item = items.get(unit_price__gt=0)

        # Main item assertions
        self.assertEqual(main_item.quantity, 15)
        self.assertEqual(main_item.unit_price, self.plan_component.price)

        # Discount item assertions
        total_before_discount = main_item.unit_price * main_item.quantity
        expected_discount = total_before_discount * (
            Decimal(self.plan_component.discount_rate) / 100
        )
        self.assertEqual(discount_item.unit_price, -expected_discount)
        self.assertEqual(discount_item.details["original_quantity"], 15)
        self.assertEqual(discount_item.details["discount_rate"], 20)
        self.assertIn("Volume Discount", discount_item.name)

    def test_discount_uses_component_value_not_duration_multiplied(self):
        """Discount should be based on the raw component quantity (e.g. 15 CPUs),
        not the duration-multiplied total (e.g. 15 * 17 days = 255)."""
        # Arrange: switch to PER_DAY billing
        plan = self.plan_component.plan
        plan.unit = marketplace_models.Plan.Units.PER_DAY
        plan.save()

        self.plan_component.discount_threshold = 10
        self.plan_component.discount_rate = 20  # 20%
        self.plan_component.save()
        self.resource.limits = {self.offering_component.type: 15}
        self.resource.save()

        # Act
        items = self._trigger_billing_and_get_items()

        # Assert
        self.assertEqual(items.count(), 2)

        discount_item = items.get(unit_price__lt=0)
        main_item = items.get(unit_price__gt=0)

        # Main item: quantity is multiplied by days in the billing period
        self.assertEqual(main_item.unit_price, self.plan_component.price)
        # The main item quantity should be raw_quantity * number_of_days
        self.assertGreater(main_item.quantity, 15)

        # Discount must be based on raw component value (15), not multiplied.
        # discount = price * raw_quantity * rate% = 10 * 15 * 0.20 = 30
        raw_quantity = 15
        expected_discount = (
            self.plan_component.price
            * raw_quantity
            * Decimal(self.plan_component.discount_rate)
            / 100
        )
        self.assertEqual(discount_item.unit_price, -expected_discount)
        self.assertEqual(discount_item.details["original_quantity"], raw_quantity)
