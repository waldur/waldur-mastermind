"""Organization-aggregated volume discounts.

Volume discounts are computed on the total billed quantity of one offering
component, summed across every resource of that offering in the customer's
invoice, and distributed back as paired negative line items. These tests build
the invoice items directly and exercise the finalization pass in isolation.
"""

from decimal import Decimal

from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace import billing_discount
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing_limit import LimitPeriodProcessor
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    DiscountAggregations,
    LimitPeriods,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


@freeze_time("2024-01-15")
class OrgAggregatedVolumeDiscountTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            name="RAM",
            billing_type=BillingTypes.USAGE,
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component,
            price=Decimal("10"),
        )
        self.invoice = invoice_factories.InvoiceFactory(customer=self.customer)

    def _add_resource_item(self, usage, project=None, plan_component=None):
        plan_component = plan_component or self.plan_component
        project = project or structure_factories.ProjectFactory(customer=self.customer)
        resource = marketplace_factories.ResourceFactory(
            offering=plan_component.component.offering,
            plan=plan_component.plan,
            project=project,
        )
        now = timezone.now()
        return invoices_models.InvoiceItem.objects.create(
            invoice=self.invoice,
            resource=resource,
            plan_component=plan_component,
            project=project,
            unit_price=plan_component.price,
            quantity=Decimal(usage),
            unit=invoices_models.Units.QUANTITY,
            start=now,
            end=now,
            details={
                "offering_component_type": plan_component.component.type,
                billing_discount.DISCOUNT_USAGE_KEY: float(usage),
            },
        )

    def _discount_items(self):
        return invoices_models.InvoiceItem.objects.filter(
            invoice=self.invoice, details__is_discount=True
        )

    def test_usage_is_summed_across_projects_to_cross_the_tier(self):
        # Threshold 10; neither resource alone reaches it, together they do.
        self.plan_component.discount_formula = "20 if usage >= 10 else 0"
        self.plan_component.save()
        item_a = self._add_resource_item(usage=6)
        item_b = self._add_resource_item(usage=6)

        billing_discount.apply_aggregated_volume_discounts(self.invoice)

        discounts = self._discount_items()
        self.assertEqual(discounts.count(), 2)
        for item in discounts:
            self.assertEqual(item.details["aggregated_usage"], 12.0)
            self.assertEqual(item.details["discount_percent"], 20.0)

        # Each discount is 20% of its own resource's line charge -> attribution
        # is preserved per project.
        discount_a = discounts.get(resource=item_a.resource)
        discount_b = discounts.get(resource=item_b.resource)
        self.assertEqual(
            discount_a.unit_price,
            -(item_a.unit_price * item_a.quantity * Decimal("0.20")),
        )
        self.assertEqual(
            discount_b.unit_price,
            -(item_b.unit_price * item_b.quantity * Decimal("0.20")),
        )
        self.assertEqual(discount_a.project, item_a.project)

    def test_no_discount_when_aggregate_below_threshold(self):
        self.plan_component.discount_formula = "20 if usage >= 100 else 0"
        self.plan_component.save()
        self._add_resource_item(usage=6)
        self._add_resource_item(usage=6)

        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        self.assertEqual(self._discount_items().count(), 0)

    def test_different_offerings_are_not_aggregated_together(self):
        # Same component type "ram" on a second, separate offering. With
        # "same offering only" grouping, the two resources never combine.
        self.plan_component.discount_formula = "50 if usage >= 10 else 0"
        self.plan_component.save()

        other_offering = marketplace_factories.OfferingFactory(customer=self.customer)
        other_component = marketplace_factories.OfferingComponentFactory(
            offering=other_offering,
            type="ram",
            name="RAM",
            billing_type=BillingTypes.USAGE,
        )
        other_plan = marketplace_factories.PlanFactory(offering=other_offering)
        other_plan_component = marketplace_factories.PlanComponentFactory(
            plan=other_plan,
            component=other_component,
            price=Decimal("10"),
            discount_formula="50 if usage >= 10 else 0",
        )

        self._add_resource_item(usage=6)
        self._add_resource_item(usage=6, plan_component=other_plan_component)

        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        # 6 per offering, each below its own threshold of 10.
        self.assertEqual(self._discount_items().count(), 0)

    def test_invoice_price_is_reduced_for_downstream_steps(self):
        # The pass runs before compensation and affiliate accrual, so the
        # post-discount invoice.price is what those steps see.
        self.plan_component.discount_formula = "20 if usage >= 10 else 0"
        self.plan_component.save()
        self._add_resource_item(usage=12)  # gross = 10 * 12 = 120

        gross = self.invoice.price
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        net = invoices_models.Invoice.objects.get(pk=self.invoice.pk).price

        self.assertEqual(gross, Decimal("120"))
        self.assertEqual(net, Decimal("96"))  # 120 - 20% = 96

    def test_per_resource_scope_does_not_sum_across_resources(self):
        # Threshold 10, per-resource scope: two resources of 6 each stay below
        # the threshold individually, so neither is discounted — unlike the
        # aggregated scope where 6 + 6 = 12 would cross it.
        self.plan_component.discount_formula = "20 if usage >= 10 else 0"
        self.plan_component.discount_aggregation = DiscountAggregations.PER_RESOURCE
        self.plan_component.save()
        self._add_resource_item(usage=6)
        self._add_resource_item(usage=6)

        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        self.assertEqual(self._discount_items().count(), 0)

    def test_per_resource_scope_uses_only_the_single_resource_usage(self):
        # One resource of 12 crosses the threshold on its own; a sibling of 6
        # does not and must be left undiscounted under the per-resource scope.
        self.plan_component.discount_formula = "20 if usage >= 10 else 0"
        self.plan_component.discount_aggregation = DiscountAggregations.PER_RESOURCE
        self.plan_component.save()
        big = self._add_resource_item(usage=12)
        small = self._add_resource_item(usage=6)

        billing_discount.apply_aggregated_volume_discounts(self.invoice)

        discounts = self._discount_items()
        self.assertEqual(discounts.count(), 1)
        discount = discounts.get()
        self.assertEqual(discount.resource, big.resource)
        self.assertEqual(discount.details["aggregated_usage"], 12.0)
        self.assertFalse(discounts.filter(resource=small.resource).exists())


@freeze_time("2024-01-15")
class TotalLimitVolumeDiscountTest(test.APITestCase):
    """TOTAL-period limit components are billed as incremental delta items.

    Every delta item must carry the signed limit change in ``discount_usage``
    so tier formulas evaluate on the resource's net limit after increases and
    decreases, and compensation (negative) items never receive discount lines.
    """

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="storage",
            name="Storage",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component,
            price=Decimal("10"),
            discount_formula="20 if usage >= 100 else 0",
            discount_aggregation=DiscountAggregations.PER_RESOURCE,
        )
        self.invoice = invoice_factories.InvoiceFactory(customer=self.customer)
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            state=ResourceStates.OK,
            limits={"storage": 50},
        )
        # The scenarios drive billing explicitly through LimitPeriodProcessor;
        # drop anything signal handlers may have produced along the way.
        invoices_models.InvoiceItem.objects.all().delete()

    def _bill_creation(self):
        now = timezone.now()
        LimitPeriodProcessor.process_creation(
            self.resource,
            self.plan_component,
            self.invoice,
            now,
            now,
            OrderTypes.CREATE,
        )

    def _set_limit(self, new_limit):
        # Queryset update: saving the model would fire limit-change billing
        # handlers, and these scenarios drive billing explicitly.
        marketplace_models.Resource.objects.filter(pk=self.resource.pk).update(
            limits={"storage": new_limit}
        )
        self.resource.refresh_from_db()

    def _update_limit(self, new_limit):
        self._set_limit(new_limit)
        LimitPeriodProcessor.process_update(
            self.resource, self.invoice, "storage", new_limit
        )

    def _main_items(self):
        # has_key: excluding on details__is_discount=True would also drop rows
        # where the key is absent (JSON NULL), i.e. every main item.
        return invoices_models.InvoiceItem.objects.filter(invoice=self.invoice).exclude(
            details__has_key="is_discount"
        )

    def _discount_items(self):
        return invoices_models.InvoiceItem.objects.filter(
            invoice=self.invoice, details__is_discount=True
        )

    def test_limit_increase_crosses_the_tier(self):
        self._bill_creation()
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        self.assertEqual(self._discount_items().count(), 0)

        self._update_limit(150)

        increment = self._main_items().get(quantity=100)
        self.assertEqual(increment.details[billing_discount.DISCOUNT_USAGE_KEY], 100.0)

        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        discounts = self._discount_items()
        # One discount line per positive charge item, both computed at the
        # 150-based tier percentage.
        self.assertEqual(discounts.count(), 2)
        for item in discounts:
            self.assertEqual(item.details["aggregated_usage"], 150.0)
            self.assertEqual(item.details["discount_percent"], 20.0)
        total_discount = sum(-item.unit_price for item in discounts)
        self.assertEqual(total_discount, Decimal("10") * 150 * Decimal("0.20"))

    def test_limit_decrease_below_tier_removes_discount(self):
        self._set_limit(150)
        self._bill_creation()
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        self.assertEqual(self._discount_items().count(), 1)

        self._update_limit(50)

        compensation = self._main_items().get(unit_price__lt=0)
        self.assertEqual(
            compensation.details[billing_discount.DISCOUNT_USAGE_KEY], -100.0
        )

        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        # Net limit is 50, below the 100 tier: no discount survives the rerun.
        self.assertEqual(self._discount_items().count(), 0)

    def test_limit_decrease_above_tier_keeps_discount_off_compensation(self):
        self._set_limit(300)
        self._bill_creation()

        self._update_limit(150)
        billing_discount.apply_aggregated_volume_discounts(self.invoice)

        discounts = self._discount_items()
        # The tier still matches (net 150 >= 100) but only the positive charge
        # item is discounted; the compensation item never gets a discount line.
        self.assertEqual(discounts.count(), 1)
        self.assertEqual(discounts.get().details["aggregated_usage"], 150.0)
        compensation = self._main_items().get(unit_price__lt=0)
        self.assertFalse(
            self._discount_items()
            .filter(details__discount_of_item=compensation.uuid.hex)
            .exists()
        )

    def test_discount_items_do_not_distort_already_billed_total(self):
        self._set_limit(150)
        self._bill_creation()
        billing_discount.apply_aggregated_volume_discounts(self.invoice)
        self.assertEqual(self._discount_items().count(), 1)

        # Re-reporting the same limit after discounts were materialized must be
        # a no-op: discount lines are excluded from the already-billed total.
        LimitPeriodProcessor.process_update(self.resource, self.invoice, "storage", 150)
        self.assertEqual(self._main_items().count(), 1)
