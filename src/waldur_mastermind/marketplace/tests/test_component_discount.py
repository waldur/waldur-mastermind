from decimal import Decimal

from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace import billing_discount
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes

from . import fixtures


@freeze_time("2024-01-15")
class AggregatedVolumeDiscountPassTest(test.APITestCase):
    """Directly exercise the finalization pass that materializes the
    organization-aggregated volume discount from main invoice items."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.plan_component = self.fixture.plan_component
        self.plan_component.price = Decimal("10")
        self.plan_component.discount_formula = "20 if usage >= 10 else 0"
        self.plan_component.save()
        self.offering_component = self.fixture.offering_component
        self.invoice = invoice_factories.InvoiceFactory(
            customer=self.resource.project.customer
        )

    def _create_main_item(
        self, usage, quantity=Decimal("10"), unit_price=Decimal("10")
    ):
        now = timezone.now()
        return invoices_models.InvoiceItem.objects.create(
            invoice=self.invoice,
            resource=self.resource,
            plan_component=self.plan_component,
            project=self.resource.project,
            unit_price=unit_price,
            quantity=quantity,
            unit=invoices_models.Units.QUANTITY,
            start=now,
            end=now,
            details={
                "offering_component_type": self.offering_component.type,
                billing_discount.DISCOUNT_USAGE_KEY: float(usage),
            },
        )

    def _discount_items(self):
        return invoices_models.InvoiceItem.objects.filter(
            invoice=self.invoice, details__is_discount=True
        )

    def test_creates_discount_item(self):
        self._create_main_item(usage=15, quantity=Decimal("10"))
        billing_discount.apply_aggregated_volume_discounts(self.invoice)

        items = self._discount_items()
        self.assertEqual(items.count(), 1)
        item = items.first()
        # 10 (price) * 10 (quantity) * 20% = 20
        self.assertEqual(item.unit_price, Decimal("-20"))
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.details["discount_percent"], 20.0)
        self.assertEqual(item.details["discount_type"], "org_volume_discount")
        self.assertEqual(item.details["aggregated_usage"], 15.0)

    def test_no_item_when_formula_yields_zero(self):
        self._create_main_item(usage=5)
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        self.assertEqual(self._discount_items().count(), 0)

    def test_item_without_discount_usage_is_ignored(self):
        now = timezone.now()
        invoices_models.InvoiceItem.objects.create(
            invoice=self.invoice,
            resource=self.resource,
            plan_component=self.plan_component,
            project=self.resource.project,
            unit_price=Decimal("10"),
            quantity=Decimal("10"),
            unit=invoices_models.Units.QUANTITY,
            start=now,
            end=now,
            details={"offering_component_type": self.offering_component.type},
        )
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        self.assertEqual(self._discount_items().count(), 0)

    def test_pass_is_idempotent(self):
        self._create_main_item(usage=15, quantity=Decimal("10"))
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        # Rebuilt, not duplicated.
        self.assertEqual(self._discount_items().count(), 1)
        self.assertEqual(self._discount_items().first().unit_price, Decimal("-20"))

    def test_recompute_reflects_updated_usage(self):
        main_item = self._create_main_item(usage=15, quantity=Decimal("10"))
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        self.assertEqual(self._discount_items().count(), 1)

        # Usage falls below the threshold: recompute drops the stale discount.
        main_item.details[billing_discount.DISCOUNT_USAGE_KEY] = 5.0
        main_item.save(update_fields=["details"])
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        self.assertEqual(self._discount_items().count(), 0)

    def test_percentage_is_clamped_to_100(self):
        self.plan_component.discount_formula = "usage"  # would exceed 100
        self.plan_component.save()
        self._create_main_item(usage=200, quantity=Decimal("10"))
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        # Clamped to 100%: 10 * 10 * 100% = 100
        self.assertEqual(self._discount_items().first().unit_price, Decimal("-100"))

    def test_broken_formula_is_skipped(self):
        self.plan_component.discount_formula = "LN(usage - 100)"  # domain error
        self.plan_component.save()
        self._create_main_item(usage=15)
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        self.assertEqual(self._discount_items().count(), 0)


@freeze_time("2024-01-15")
class FixedComponentDiscountBillingTest(test.APITestCase):
    """The discount applies to FIXED components too, scaling on the configured
    amount, and is materialized at finalization."""

    def setUp(self):
        super().setUp()
        self.fixture = fixtures.MarketplaceFixture()

        self.offering_component = self.fixture.offering_component
        self.offering_component.billing_type = BillingTypes.FIXED
        self.offering_component.save()

        self.plan_component = self.fixture.plan_component
        self.plan_component.price = Decimal("10.0")
        self.plan_component.amount = 12
        self.plan_component.save()

        self.resource = self.fixture.resource

    def _trigger_billing_and_get_items(self):
        self.resource.state = marketplace_models.ResourceStates.CREATING
        self.resource.save()
        self.resource.set_state_ok()
        self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2024, month=1
        )
        billing_discount.apply_aggregated_volume_discounts(invoice)
        return invoice.items.filter(
            resource_id=self.resource.id,
            details__offering_component_type=self.offering_component.type,
        ).order_by("unit_price")

    def test_discount_applied_to_fixed_component(self):
        # usage = amount = 12 -> 10% discount.
        self.plan_component.discount_formula = "10 if usage >= 8 else 0"
        self.plan_component.save()

        items = self._trigger_billing_and_get_items()
        self.assertEqual(items.count(), 2)

        discount_item = items.get(unit_price__lt=0)
        main_item = items.get(unit_price__gt=0)

        # The discount is 10% of the actual line charge.
        expected = main_item.unit_price * main_item.quantity * Decimal("0.10")
        self.assertEqual(discount_item.unit_price, -expected)
        self.assertEqual(discount_item.details["aggregated_usage"], 12.0)

    def test_no_discount_when_amount_below_threshold(self):
        self.plan_component.amount = 5
        self.plan_component.discount_formula = "10 if usage >= 8 else 0"
        self.plan_component.save()

        items = self._trigger_billing_and_get_items()
        self.assertEqual(items.count(), 1)
        self.assertNotIn("is_discount", items.first().details)
