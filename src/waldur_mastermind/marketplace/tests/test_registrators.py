from decimal import Decimal

from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.billing import MarketplaceBillingService
from waldur_mastermind.marketplace.enums import OrderTypes
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


class PrepaidBillingTestBase(test.APITransactionTestCase):
    """Base class for prepaid billing tests with a complete offering setup."""

    def setUp(self):
        super().setUp()
        self.fixture = MarketplaceFixture()

        self.offering = self.fixture.offering
        self.plan = self.fixture.plan
        self.plan.components.all().delete()

        # Component for the upfront payment and quota tracking
        self.prepaid_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="storage-prepaid",
            name="Prepaid Storage Quota",
            is_prepaid=True,
            billing_type=models.BillingTypes.ONE_TIME,
            limit_period=models.LimitPeriods.TOTAL,  # Default for base tests
        )
        # Component for billing overage
        self.overage_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="storage-overage",
            name="Storage Overage",
            is_prepaid=False,
            billing_type=models.BillingTypes.USAGE,
        )
        self.prepaid_component.overage_component = self.overage_component
        self.prepaid_component.save()

        self.upfront_plan_component = factories.PlanComponentFactory(
            plan=self.plan,
            component=self.prepaid_component,
            price=Decimal("1000.0"),
        )
        self.overage_plan_component = factories.PlanComponentFactory(
            plan=self.plan,
            component=self.overage_component,
            price=Decimal("15.0"),
        )
        self.resource = self.fixture.resource
        self.resource.offering = self.offering
        self.resource.plan = self.plan
        self.resource.limits = {"storage-prepaid": 100}
        self.resource.save()

        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan, start=self.resource.created
        )

    def _create_or_update_usage(self, component, usage, date=None):
        """Helper to simulate a cumulative usage report for a given period."""
        date = date or timezone.now()
        billing_period = date.date().replace(day=1)
        record, _ = models.ComponentUsage.objects.update_or_create(
            resource=self.resource,
            component=component,
            plan_period=self.plan_period,
            billing_period=billing_period,
            defaults={"usage": usage, "date": date},
        )
        return record


@freeze_time("2024-06-15")
class TestPrepaidCreationBilling(PrepaidBillingTestBase):
    def test_upfront_fee_is_billed_on_resource_creation(self):
        # Arrange
        self.resource.delete()
        new_resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            limits={"storage-prepaid": 100},
            state=models.Resource.States.CREATING,
        )

        # Act
        new_resource.state = models.Resource.States.OK
        new_resource.save()

        # Assert
        self.assertEqual(invoice_models.InvoiceItem.objects.count(), 1)
        item = invoice_models.InvoiceItem.objects.first()
        self.assertEqual(item.resource, new_resource)
        self.assertEqual(
            item.details["offering_component_type"], self.prepaid_component.type
        )
        self.assertEqual(item.unit_price, self.upfront_plan_component.price)
        self.assertEqual(item.quantity, 1)

    def test_upfront_fee_is_not_billed_on_resource_update(self):
        MarketplaceBillingService._register(
            self.resource, timezone.now(), order_type=OrderTypes.UPDATE
        )
        self.assertFalse(invoice_models.InvoiceItem.objects.exists())


class TestPrepaidTotalUsageBilling(PrepaidBillingTestBase):
    def setUp(self):
        super().setUp()
        self.resource.created = timezone.make_aware(timezone.datetime(2024, 1, 1))
        self.resource.save()
        self.plan_period.start = self.resource.created
        self.plan_period.save()

        # Baseline usage of 70 GB. Remaining total balance is 30 GB.
        self._create_or_update_usage(
            self.prepaid_component,
            70,
            date=timezone.make_aware(timezone.datetime(2024, 5, 1)),
        )

    def test_usage_within_balance_creates_no_invoice_item(self):
        self._create_or_update_usage(
            self.prepaid_component,
            20,
            date=timezone.make_aware(timezone.datetime(2024, 6, 1)),
        )
        self.assertFalse(invoice_models.InvoiceItem.objects.exists())

    def test_usage_exceeding_balance_creates_overage_item(self):
        self._create_or_update_usage(
            self.prepaid_component,
            50,
            date=timezone.make_aware(timezone.datetime(2024, 6, 1)),
        )

        self.assertEqual(invoice_models.InvoiceItem.objects.count(), 1)
        item = invoice_models.InvoiceItem.objects.first()
        self.assertEqual(
            item.details["offering_component_type"], self.overage_component.type
        )
        self.assertEqual(item.quantity, 20)  # Total usage (70+50) - limit (100) = 20
        self.assertEqual(item.unit_price, self.overage_plan_component.price)
        self.assertIn("(Overage)", item.name)


class TestPrepaidPeriodicUsageBilling(PrepaidBillingTestBase):
    @freeze_time("2024-06-15")
    def test_monthly_subscription_period_ignores_past_usage(self):
        # Arrange
        self.prepaid_component.limit_period = models.LimitPeriods.MONTH
        self.prepaid_component.save()
        self.resource.created = timezone.make_aware(timezone.datetime(2024, 1, 10))
        self.resource.save()
        self.plan_period.start = self.resource.created
        self.plan_period.save()

        # Report usage from a *previous* subscription month (May 10 - June 9)
        self._create_or_update_usage(
            self.prepaid_component,
            80,
            date=timezone.make_aware(timezone.datetime(2024, 5, 20)),
        )

        # Act: Report usage in the *current* subscription month (June 10 - July 9)
        # Overage should be 120 - 100 = 20.
        self._create_or_update_usage(
            self.prepaid_component,
            120,
            date=timezone.make_aware(timezone.datetime(2024, 6, 15)),
        )

        # Assert
        item = invoice_models.InvoiceItem.objects.first()
        self.assertIsNotNone(item)
        self.assertEqual(item.quantity, 20)
        self.assertEqual(
            item.details["offering_component_type"], self.overage_component.type
        )

    @freeze_time("2024-06-15")
    def test_quarterly_subscription_period_ignores_past_usage(self):
        # Arrange
        self.prepaid_component.limit_period = models.LimitPeriods.QUARTERLY
        self.prepaid_component.save()
        self.resource.created = timezone.make_aware(timezone.datetime(2024, 1, 10))
        self.resource.save()
        self.plan_period.start = self.resource.created
        self.plan_period.save()

        # Report usage from a *previous* subscription quarter (Jan 10 - Apr 9)
        self._create_or_update_usage(
            self.prepaid_component,
            80,
            date=timezone.make_aware(timezone.datetime(2024, 3, 20)),
        )

        # Act: Report usage in the *current* subscription quarter (Apr 10 - Jul 9)
        self._create_or_update_usage(
            self.prepaid_component,
            120,
            date=timezone.make_aware(timezone.datetime(2024, 6, 15)),
        )

        # Assert
        self.assertEqual(invoice_models.InvoiceItem.objects.count(), 1)
        item = invoice_models.InvoiceItem.objects.first()
        self.assertEqual(item.quantity, 20)

    @freeze_time("2024-06-15")
    def test_annual_subscription_period_ignores_past_usage(self):
        # Arrange
        self.prepaid_component.limit_period = models.LimitPeriods.ANNUAL
        self.prepaid_component.save()
        self.resource.created = timezone.make_aware(timezone.datetime(2023, 1, 10))
        self.resource.save()
        self.plan_period.start = self.resource.created
        self.plan_period.save()

        # Report usage from the *previous* subscription year (2023-01-10 to 2024-01-09)
        self._create_or_update_usage(
            self.prepaid_component,
            80,
            date=timezone.make_aware(timezone.datetime(2023, 12, 20)),
        )

        # Act: Report usage in the *current* subscription year (2024-01-10 to 2025-01-09)
        self._create_or_update_usage(
            self.prepaid_component,
            120,
            date=timezone.make_aware(timezone.datetime(2024, 6, 15)),
        )

        # Assert
        self.assertEqual(invoice_models.InvoiceItem.objects.count(), 1)
        item = invoice_models.InvoiceItem.objects.first()
        self.assertEqual(item.quantity, 20)
