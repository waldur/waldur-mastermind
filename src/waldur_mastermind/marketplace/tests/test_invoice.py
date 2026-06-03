import datetime
from datetime import UTC, timedelta
from decimal import Decimal

from ddt import data, ddt
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test
from rest_framework.reverse import reverse

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tasks import create_monthly_invoices
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing import (
    LimitPeriodProcessor,
    MarketplaceBillingService,
)
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    OfferingStates,
    OrderStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.models import Order
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests.factories import ResourceFactory

from . import fixtures


@freeze_time("2020-11-01")
class InvoiceTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

    def test_handler_if_resource_has_been_created(self):
        self.resource.set_state_ok()
        self.resource.save()
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        self.assertEqual(
            invoice.items.filter(
                resource_id=self.resource.id,
            ).count(),
            1,
        )

    @freeze_time("2020-11-02")
    def test_handler_if_resource_has_been_terminated(self):
        self.resource.set_state_ok()
        self.resource.save()
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        item = invoice.items.get(
            resource_id=self.resource.id,
        )
        self.resource.set_state_terminating()
        self.resource.save()
        self.resource.set_state_terminated()
        self.resource.save()
        item.refresh_from_db()
        self.assertEqual(item.end, timezone.now())

    @freeze_time("2020-12-01")
    def test_create_monthly_invoices(self):
        self.resource.set_state_ok()
        self.resource.save()
        create_monthly_invoices()
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=12
        )
        self.assertEqual(
            invoice.items.filter(
                resource_id=self.resource.id,
            ).count(),
            1,
        )


@freeze_time("2020-11-01")
class TotalLimitTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.component = self.fixture.offering_component
        self.component.billing_type = BillingTypes.LIMIT
        self.component.limit_period = LimitPeriods.TOTAL
        self.component.save()
        self.resource = ResourceFactory(
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            project=self.fixture.project,
            limits={self.component.type: 10},
        )
        self.resource.set_state_ok()
        self.resource.save()

    def get_invoice_items(self, year=2020, month=11):
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer,
            year=year,
            month=month,
        )
        return invoice.items.filter(
            details__offering_component_type=self.component.type,
            resource_id=self.resource.id,
        )

    def test_when_resource_provisioning_is_completed_invoice_item_is_created(self):
        items = self.get_invoice_items()
        self.assertEqual(items.count(), 1)

    def test_when_monthly_invoice_is_created_for_provisioned_resource_invoice_item_is_not_created(
        self,
    ):
        with freeze_time("2020-12-01"):
            create_monthly_invoices()
        items = self.get_invoice_items(year=2020, month=12)
        self.assertEqual(items.count(), 0)

    def test_when_limit_is_increased_invoice_item_is_created(self):
        self.resource.limits[self.component.type] = 20
        self.resource.save()

        items = self.get_invoice_items()
        self.assertEqual(items.count(), 2)
        self.assertTrue(items.last().unit_price > 0)
        self.assertEqual(items.last().quantity, 10)

    def test_when_limit_is_decreased_compensation_invoice_item_is_created(self):
        self.resource.limits[self.component.type] = 5
        self.resource.save()

        items = self.get_invoice_items()
        self.assertEqual(items.count(), 2)
        self.assertTrue(items.last().unit_price < 0)
        self.assertEqual(items.last().quantity, 5)

    def test_total_billing_works_without_create_orders(self):
        """Test TOTAL billing behavior when CREATE orders are missing (simulating deleted orders scenario)."""
        # First, verify we have initial billing from resource creation
        items = self.get_invoice_items()
        self.assertEqual(items.count(), 1, "Initial TOTAL billing should exist")

        # Create and then delete a CREATE order (simulating the real-world scenario
        # where CREATE orders might be deleted but resources and billing remain)
        create_order = Order.objects.create(
            project=self.resource.project,
            resource=self.resource,
            offering=self.resource.offering,
            plan=self.resource.plan,
            type=OrderTypes.CREATE,
            state=OrderStates.DONE,
            created_by=self.fixture.owner,
        )

        # Verify CREATE order exists initially
        self.assertTrue(
            Order.objects.filter(
                resource=self.resource, type=OrderTypes.CREATE
            ).exists(),
            "CREATE order should exist",
        )

        # Delete the CREATE order (simulating cleanup/deletion scenarios)
        create_order.delete()

        # Verify CREATE order is gone
        self.assertFalse(
            Order.objects.filter(
                resource=self.resource, type=OrderTypes.CREATE
            ).exists(),
            "CREATE order should be deleted",
        )

        # Now test that TOTAL billing still works correctly for limit changes
        # even without CREATE orders present
        self.resource.limits[self.component.type] = 15
        self.resource.save()

        # Verify billing still works - should have initial + update billing
        items = self.get_invoice_items()
        self.assertEqual(
            items.count(),
            2,
            "Should have initial + update billing even without CREATE order",
        )

        # Verify the update item has correct properties
        update_item = items.last()
        self.assertTrue(update_item.unit_price > 0, "Update should have positive price")
        self.assertEqual(
            update_item.quantity, 5, "Update should bill the difference (15-10=5)"
        )

        # Test another limit change to ensure the system remains functional
        self.resource.limits[self.component.type] = 8
        self.resource.save()

        # Should now have 3 items (initial + 2 updates)
        items = self.get_invoice_items()
        self.assertEqual(items.count(), 3, "Should have initial + 2 updates")

        # Latest should be a compensation item (negative)
        latest_item = items.last()
        self.assertTrue(
            latest_item.unit_price < 0, "Decrease should have negative price"
        )
        self.assertEqual(
            latest_item.quantity,
            7,
            "Decrease should compensate for difference (15-8=7)",
        )

        # Critical test: Verify monthly invoice creation doesn't create duplicates
        # This is the main behavior we want to preserve - TOTAL components should
        # not be billed monthly regardless of order history
        with freeze_time("2020-12-01"):
            create_monthly_invoices()
        december_items = self.get_invoice_items(year=2020, month=12)
        self.assertEqual(
            december_items.count(),
            0,
            "Monthly invoices should never create TOTAL duplicates, even without CREATE orders",
        )

    def test_update_order_billing_logic_without_create_orders(self):
        """Test UPDATE order billing when CREATE orders are missing - first UPDATE bills full amount."""

        # Re-verify and enforce component is TOTAL before billing triggers.
        # This guards against potential cross-test state leaks in CI.
        self.component.refresh_from_db()
        if self.component.limit_period != LimitPeriods.TOTAL:
            self.component.limit_period = LimitPeriods.TOTAL
            self.component.save()

        # Create a fresh resource with 0 limit to test pure UPDATE scenarios
        fresh_resource = ResourceFactory(
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            project=self.fixture.project,
            limits={self.component.type: 0},  # Start with 0 limit
        )
        fresh_resource.set_state_ok()
        fresh_resource.save()

        # Get the November invoice (it will be created when the resource is set to OK)
        november_invoice = invoices_models.Invoice.objects.get(
            customer=fresh_resource.project.customer, year=2020, month=11
        )

        # Check initial state - should have no items for 0 limit
        initial_items = november_invoice.items.filter(
            details__offering_component_type=self.component.type,
            resource_id=fresh_resource.id,
        )
        self.assertEqual(
            initial_items.count(), 0, "Should have no initial items for 0 limit"
        )

        # Scenario 1: First UPDATE without CREATE order - should bill FULL amount
        # This is the expected behavior when CREATE order is missing
        fresh_resource.limits[self.component.type] = 10
        fresh_resource.save()

        # Verify first UPDATE billing occurs with full amount
        items = november_invoice.items.filter(
            details__offering_component_type=self.component.type,
            resource_id=fresh_resource.id,
        )
        self.assertEqual(
            items.count(),
            1,
            "First UPDATE should bill full amount when CREATE order is missing",
        )

        first_update = items.first()
        self.assertEqual(
            first_update.quantity, 10, "First UPDATE should bill full amount (0→10=10)"
        )
        self.assertTrue(
            first_update.price > 0, "First UPDATE should have positive price"
        )
        expected_price = 10 * self.fixture.plan_component.price
        self.assertEqual(
            first_update.price,
            expected_price,
            "First UPDATE should bill 10 × unit price",
        )

        # Scenario 2: Second UPDATE - should bill only the DIFFERENCE
        fresh_resource.limits[self.component.type] = 15
        fresh_resource.save()

        items = november_invoice.items.filter(
            details__offering_component_type=self.component.type,
            resource_id=fresh_resource.id,
        )
        self.assertEqual(
            items.count(),
            2,
            "Should have first UPDATE + second UPDATE",
        )

        second_update = items.last()
        self.assertEqual(
            second_update.quantity, 5, "Second UPDATE should bill difference (15-10=5)"
        )
        self.assertTrue(
            second_update.price > 0, "Second UPDATE should have positive price"
        )
        expected_diff_price = 5 * self.fixture.plan_component.price
        self.assertEqual(
            second_update.price,
            expected_diff_price,
            "Second UPDATE should bill 5 × unit price",
        )

        # Scenario 3: UPDATE decrease - should create compensation
        fresh_resource.limits[self.component.type] = 8
        fresh_resource.save()

        items = november_invoice.items.filter(
            details__offering_component_type=self.component.type,
            resource_id=fresh_resource.id,
        )
        self.assertEqual(items.count(), 3, "Should have 3 UPDATE items total")

        third_update = items.last()
        self.assertEqual(
            third_update.quantity, 7, "Third UPDATE should compensate (15-8=7)"
        )
        self.assertTrue(third_update.price < 0, "Decrease should have negative price")
        expected_comp_price = -7 * self.fixture.plan_component.price
        self.assertEqual(
            third_update.price,
            expected_comp_price,
            "Compensation should be -7 × unit price",
        )

        # Verify the final billing logic is correct
        all_items = list(items)
        self.assertEqual(len(all_items), 3, "Should have exactly 3 UPDATE items")

        # Verify total billing equals final limit amount
        total_price = sum(item.price for item in all_items)
        expected_total = 8 * self.fixture.plan_component.price
        self.assertEqual(
            total_price,
            expected_total,
            "Total billing should equal final limit × unit price",
        )

        # Critical: Verify monthly invoices don't duplicate this UPDATE-only billing
        with freeze_time("2020-12-01"):
            create_monthly_invoices()

        december_items = invoices_models.Invoice.objects.get(
            customer=fresh_resource.project.customer, year=2020, month=12
        ).items.filter(
            details__offering_component_type=self.component.type,
            resource_id=fresh_resource.id,
        )
        self.assertEqual(
            december_items.count(),
            0,
            "Monthly invoices should not create TOTAL duplicates for UPDATE-only resources",
        )


@freeze_time("2020-11-01")
class TotalLimitDailyPlanTest(test.APITestCase):
    """Test that limit_period=TOTAL with plan.unit=day does NOT multiply quantity by days."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.component = self.fixture.offering_component
        self.component.billing_type = BillingTypes.LIMIT
        self.component.limit_period = LimitPeriods.TOTAL
        self.component.save()
        # Override plan unit to PER_DAY
        self.fixture.plan.unit = marketplace_models.Plan.Units.PER_DAY
        self.fixture.plan.save()
        self.resource = ResourceFactory(
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            project=self.fixture.project,
            limits={self.component.type: 10},
        )
        self.resource.set_state_ok()
        self.resource.save()

    def get_invoice_items(self):
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer,
            year=2020,
            month=11,
        )
        return invoice.items.filter(
            details__offering_component_type=self.component.type,
            resource_id=self.resource.id,
        )

    def test_total_limit_with_daily_plan_does_not_multiply_by_days(self):
        """TOTAL limit period should produce quantity=limit, not quantity=limit*days."""
        items = self.get_invoice_items()
        self.assertEqual(items.count(), 1)
        item = items.first()
        # Quantity should be the raw limit (10), NOT 10 * 30 days
        self.assertEqual(item.quantity, 10)
        self.assertEqual(item.unit, invoices_models.InvoiceItem.Units.QUANTITY)

    def test_total_limit_with_daily_plan_termination_preserves_quantity(self):
        """Terminating a TOTAL+daily resource should NOT recalculate quantity by days."""
        items = self.get_invoice_items()
        item = items.first()
        original_quantity = item.quantity

        with freeze_time("2020-11-15"):
            item.terminate()
            item.refresh_from_db()

        self.assertEqual(item.quantity, original_quantity)


@ddt
@freeze_time("2020-11-01")
class InvoiceItemsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.set_state_ok()
        self.resource.save()
        self.invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )

        other_fixture = fixtures.MarketplaceFixture()
        other_resource = other_fixture.resource
        other_resource.set_state_ok()
        other_resource.save()

        self.url = reverse("provider-invoice-items-list")

    @data("staff")
    def test_user_can_get_all_invoice_items(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 2)

    @data("offering_owner", "service_manager")
    def test_user_can_get_his_invoice_items(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    @data(
        "admin",
        "manager",
        "member",
        "customer_support",
        "customer_support",
        "owner",
        "user",
        "global_support",
    )
    def test_user_can_not_get_invoice_items(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)


@freeze_time("2020-04-15")  # Middle of Q2 (April is a quarterly billing month)
class QuarterlyBillingTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # Create a quarterly limit-based component
        self.offering_component = self.fixture.offering.components.first()
        self.offering_component.billing_type = BillingTypes.LIMIT
        self.offering_component.limit_period = LimitPeriods.QUARTERLY
        self.offering_component.save()

        self.plan_component = self.fixture.plan.components.first()
        self.plan_component.component = self.offering_component
        self.plan_component.save()

        self.resource = self.fixture.resource
        self.resource.limits = {"cpu": 2}
        self.resource.save()

    def test_quarterly_limit_billing_creates_invoice_item(self):
        """Test that quarterly billing creates proper invoice items."""
        self.resource.set_state_ok()
        self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=4
        )

        # Should have an invoice item for the quarterly limit
        items = invoice.items.filter(resource_id=self.resource.id)
        self.assertEqual(items.count(), 1)

        item = items.first()
        # Check that the period spans the entire quarter (April 1 - June 30, 2020)
        self.assertEqual(item.start.month, 4)  # Q2 start
        self.assertEqual(item.start.day, 1)
        self.assertEqual(item.end.month, 6)  # Q2 end
        self.assertEqual(item.end.day, 30)

    def test_quarterly_limit_change_updates_invoice(self):
        """Test that changing limits mid-quarter updates the invoice properly."""
        self.resource.set_state_ok()
        self.resource.save()

        # Change limits mid-quarter
        self.resource.limits = {"cpu": 4}
        self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=4
        )

        items = invoice.items.filter(resource_id=self.resource.id)
        self.assertTrue(items.exists())

        # Verify the resource limit periods track the change
        item = items.first()
        self.assertIn("resource_limit_periods", item.details)
        limit_periods = item.details["resource_limit_periods"]
        # Should have multiple periods due to the limit change
        self.assertGreaterEqual(len(limit_periods), 2)


@freeze_time("2020-01-15")  # Middle of Q1
class QuarterlyVsMonthlyBillingTest(test.APITestCase):
    """Test quarterly billing behavior compared to monthly billing."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Create monthly limit-based component
        self.monthly_component = self.fixture.offering.components.first()
        self.monthly_component.billing_type = BillingTypes.LIMIT
        self.monthly_component.limit_period = LimitPeriods.MONTH
        self.monthly_component.type = "monthly_cpu"
        self.monthly_component.save()

        # Create quarterly limit-based component
        self.quarterly_component = self.fixture.offering.components.create(
            name="Quarterly CPU",
            type="quarterly_cpu",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.QUARTERLY,
            measured_unit="GB",
        )

        # Create plan components for both
        self.monthly_plan_component = self.fixture.plan.components.first()
        self.monthly_plan_component.component = self.monthly_component
        self.monthly_plan_component.save()

        self.quarterly_plan_component = self.fixture.plan.components.create(
            component=self.quarterly_component, amount=1, price=100
        )

        self.resource = self.fixture.resource
        self.resource.limits = {"monthly_cpu": 2, "quarterly_cpu": 4}
        self.resource.save()
        self.resource.set_state_ok()
        self.resource.save()

    def test_monthly_billing_in_quarterly_month(self):
        """Test that monthly components are billed even in quarterly months."""
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=1
        )

        # Should have items for both monthly and quarterly components in January (Q1 start)
        monthly_items = invoice.items.filter(
            details__offering_component_type="monthly_cpu"
        )
        quarterly_items = invoice.items.filter(
            details__offering_component_type="quarterly_cpu"
        )

        self.assertEqual(
            monthly_items.count(),
            1,
            "Monthly component should be billed in quarterly month",
        )
        self.assertEqual(
            quarterly_items.count(),
            1,
            "Quarterly component should be billed in quarterly month",
        )

    @freeze_time("2020-02-15")  # February - not a quarterly month
    def test_only_monthly_billing_in_non_quarterly_month(self):
        """Test that only monthly components are billed in non-quarterly months."""
        # Trigger monthly invoice creation for February
        create_monthly_invoices()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=2
        )

        monthly_items = invoice.items.filter(
            details__offering_component_type="monthly_cpu"
        )
        quarterly_items = invoice.items.filter(
            details__offering_component_type="quarterly_cpu"
        )

        self.assertEqual(
            monthly_items.count(),
            1,
            "Monthly component should be billed in non-quarterly month",
        )
        self.assertEqual(
            quarterly_items.count(),
            0,
            "Quarterly component should NOT be billed in non-quarterly month",
        )

    @freeze_time("2020-04-15")  # April - Q2 start
    def test_quarterly_billing_in_next_quarter(self):
        """Test that quarterly billing happens again in the next quarterly month."""
        # Trigger monthly invoice creation for April
        create_monthly_invoices()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=4
        )

        quarterly_items = invoice.items.filter(
            details__offering_component_type="quarterly_cpu"
        )

        self.assertEqual(
            quarterly_items.count(),
            1,
            "Quarterly component should be billed again in Q2",
        )

        # Verify the billing period is for the entire quarter
        item = quarterly_items.first()
        self.assertEqual(item.start.month, 4)  # Q2 start
        self.assertEqual(item.end.month, 6)  # Q2 end


@ddt
class QuarterlyBillingMonthDetectionTest(test.APITestCase):
    """Test quarterly billing month detection logic."""

    @data(
        (1, True),  # January - Q1
        (2, False),  # February - not quarterly
        (3, False),  # March - not quarterly
        (4, True),  # April - Q2
        (5, False),  # May - not quarterly
        (6, False),  # June - not quarterly
        (7, True),  # July - Q3
        (8, False),  # August - not quarterly
        (9, False),  # September - not quarterly
        (10, True),  # October - Q4
        (11, False),  # November - not quarterly
        (12, False),  # December - not quarterly
    )
    def test_quarterly_billing_month_detection(self, month_and_expected):
        """Test that quarterly billing only happens in months 1, 4, 7, 10."""
        month, expected = month_and_expected

        # Create a test date for the given month
        test_date = timezone.datetime(2020, month, 15)

        result = LimitPeriodProcessor._should_process_billing(
            LimitPeriods.QUARTERLY, test_date
        )
        self.assertEqual(
            result, expected, f"Month {month} quarterly billing detection failed"
        )

    def test_quarterly_billing_period_calculation(self):
        """Test quarterly billing period calculation for each quarter."""
        # Test Q1 (January)
        q1_date = timezone.datetime(2020, 1, 15)
        q1_start, q1_end = LimitPeriodProcessor._get_billing_period(
            LimitPeriods.QUARTERLY, q1_date
        )
        self.assertEqual(q1_start.month, 1)
        self.assertEqual(q1_start.day, 1)
        self.assertEqual(q1_end.month, 3)
        self.assertEqual(q1_end.day, 31)

        # Test Q2 (April)
        q2_date = timezone.datetime(2020, 4, 15)
        q2_start, q2_end = LimitPeriodProcessor._get_billing_period(
            LimitPeriods.QUARTERLY, q2_date
        )
        self.assertEqual(q2_start.month, 4)
        self.assertEqual(q2_start.day, 1)
        self.assertEqual(q2_end.month, 6)
        self.assertEqual(q2_end.day, 30)

        # Test Q3 (July)
        q3_date = timezone.datetime(2020, 7, 15)
        q3_start, q3_end = LimitPeriodProcessor._get_billing_period(
            LimitPeriods.QUARTERLY, q3_date
        )
        self.assertEqual(q3_start.month, 7)
        self.assertEqual(q3_start.day, 1)
        self.assertEqual(q3_end.month, 9)
        self.assertEqual(q3_end.day, 30)

        # Test Q4 (October)
        q4_date = timezone.datetime(2020, 10, 15)
        q4_start, q4_end = LimitPeriodProcessor._get_billing_period(
            LimitPeriods.QUARTERLY, q4_date
        )
        self.assertEqual(q4_start.month, 10)
        self.assertEqual(q4_start.day, 1)
        self.assertEqual(q4_end.month, 12)
        self.assertEqual(q4_end.day, 31)


@freeze_time("2020-01-01")
class QuarterlyBillingIntegrationTest(test.APITestCase):
    """Integration test for quarterly billing with create_monthly_invoices task."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Create quarterly limit-based component
        self.quarterly_component = self.fixture.offering.components.first()
        self.quarterly_component.billing_type = BillingTypes.LIMIT
        self.quarterly_component.limit_period = LimitPeriods.QUARTERLY
        self.quarterly_component.save()

        self.plan_component = self.fixture.plan.components.first()
        self.plan_component.component = self.quarterly_component
        self.plan_component.save()

        self.resource = self.fixture.resource
        self.resource.limits = {"cpu": 2}
        self.resource.save()
        self.resource.set_state_ok()
        self.resource.save()

    def test_quarterly_billing_through_monthly_task(self):
        """Test that quarterly billing works correctly through the monthly invoice creation task."""
        # Initial invoice should exist for January (Q1)
        january_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=1
        )
        january_items = january_invoice.items.filter(resource_id=self.resource.id)
        self.assertEqual(
            january_items.count(), 1, "January should have quarterly billing"
        )

        # Run monthly task for February - should not create quarterly items
        with freeze_time("2020-02-01"):
            create_monthly_invoices()

        february_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=2
        )
        february_items = february_invoice.items.filter(resource_id=self.resource.id)
        self.assertEqual(
            february_items.count(), 0, "February should not have quarterly billing"
        )

        # Run monthly task for March - should not create quarterly items
        with freeze_time("2020-03-01"):
            create_monthly_invoices()

        march_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=3
        )
        march_items = march_invoice.items.filter(resource_id=self.resource.id)
        self.assertEqual(
            march_items.count(), 0, "March should not have quarterly billing"
        )

        # Run monthly task for April - should create quarterly items for Q2
        with freeze_time("2020-04-01"):
            create_monthly_invoices()

        april_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=4
        )
        april_items = april_invoice.items.filter(resource_id=self.resource.id)
        self.assertEqual(
            april_items.count(), 1, "April should have quarterly billing for Q2"
        )

        # Verify the Q2 billing period
        item = april_items.first()
        self.assertEqual(item.start.month, 4)  # Q2 start
        self.assertEqual(item.end.month, 6)  # Q2 end
        self.assertEqual(item.end.day, 30)  # June 30th


@freeze_time("2020-01-01")
class QuarterlyLimitChangeInNonQuarterlyMonthTest(test.APITestCase):
    """Test that changing limits in a non-quarterly month updates the original
    quarterly invoice item rather than creating a duplicate on the new month's invoice."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        self.quarterly_component = self.fixture.offering.components.first()
        self.quarterly_component.billing_type = BillingTypes.LIMIT
        self.quarterly_component.limit_period = LimitPeriods.QUARTERLY
        self.quarterly_component.save()

        self.plan_component = self.fixture.plan.components.first()
        self.plan_component.component = self.quarterly_component
        self.plan_component.save()

        self.resource = self.fixture.resource
        self.resource.limits = {"cpu": 4}
        self.resource.save()
        self.resource.set_state_ok()
        self.resource.save()

        # Verify initial state: January invoice has 1 quarterly item
        self.january_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=1
        )
        self.assertEqual(
            self.january_invoice.items.filter(resource_id=self.resource.id).count(),
            1,
            "January should have exactly 1 quarterly billing item",
        )

    def test_limit_change_in_non_quarterly_month_should_not_create_new_invoice_item(
        self,
    ):
        """When limits change in February (non-quarterly month), a new invoice item
        should NOT be created on the February invoice because the quarterly item
        already exists on the January invoice."""
        with freeze_time("2020-02-15"):
            create_monthly_invoices()

            self.resource.limits = {"cpu": 5}
            self.resource.save()

        february_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=2
        )
        february_items = february_invoice.items.filter(resource_id=self.resource.id)
        self.assertEqual(
            february_items.count(),
            0,
            "February should NOT have a quarterly billing item; "
            "the limit change should update the January item instead",
        )

    def test_limit_change_in_non_quarterly_month_updates_original_invoice_item(self):
        """When limits change in February, the original January invoice item
        should be updated with new resource_limit_periods."""
        with freeze_time("2020-02-15"):
            create_monthly_invoices()

            self.resource.limits = {"cpu": 5}
            self.resource.save()

        self.january_invoice.refresh_from_db()
        january_items = self.january_invoice.items.filter(resource_id=self.resource.id)
        self.assertEqual(january_items.count(), 1)

        item = january_items.first()
        limit_periods = item.details.get("resource_limit_periods", [])
        self.assertGreaterEqual(
            len(limit_periods),
            2,
            "January invoice item should have multiple periods after limit change",
        )

    def test_limit_change_in_non_quarterly_month_does_not_double_bill(self):
        """Changing limits mid-quarter should not result in double billing
        across two invoices for the same quarterly period."""
        with freeze_time("2020-02-15"):
            create_monthly_invoices()

            self.resource.limits = {"cpu": 5}
            self.resource.save()

        # Count all invoice items across all invoices for this resource
        all_items = invoices_models.InvoiceItem.objects.filter(
            resource_id=self.resource.id,
            details__offering_component_type="cpu",
        )
        self.assertEqual(
            all_items.count(),
            1,
            "There should be exactly 1 invoice item for Q1, not duplicates across months",
        )

    def test_multiple_limit_changes_across_non_quarterly_months(self):
        """Multiple limit changes across February and March should all update
        the original January invoice item."""
        with freeze_time("2020-02-15"):
            create_monthly_invoices()
            self.resource.limits = {"cpu": 5}
            self.resource.save()

        with freeze_time("2020-03-10"):
            create_monthly_invoices()
            self.resource.limits = {"cpu": 6}
            self.resource.save()

        # January item should be updated with all changes
        self.january_invoice.refresh_from_db()
        january_items = self.january_invoice.items.filter(resource_id=self.resource.id)
        self.assertEqual(january_items.count(), 1)

        item = january_items.first()
        limit_periods = item.details.get("resource_limit_periods", [])
        self.assertGreaterEqual(
            len(limit_periods),
            3,
            "Should have at least 3 periods: original + Feb change + Mar change",
        )

        # No items should exist on February or March invoices
        for month in [2, 3]:
            invoice = invoices_models.Invoice.objects.get(
                customer=self.resource.project.customer, year=2020, month=month
            )
            items = invoice.items.filter(resource_id=self.resource.id)
            self.assertEqual(
                items.count(),
                0,
                f"Month {month} should NOT have quarterly billing items",
            )


@freeze_time("2020-01-01")
class QuarterlyLimitChangeQuantityProrationTest(test.APITestCase):
    """Test that changing limits mid-quarter prorates the invoice item quantity
    correctly instead of naively summing the old and new limit values.

    Bug scenario: With PER_MONTH unit and quarterly limit period, when a limit
    changes from 2 to 4 mid-quarter, the quantity should be prorated based on
    the fraction of the quarter each limit was active. Instead, the code sums
    the raw limits (2 + 4 = 6), effectively charging for both the old AND new
    limits simultaneously.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        self.quarterly_component = self.fixture.offering.components.first()
        self.quarterly_component.billing_type = BillingTypes.LIMIT
        self.quarterly_component.limit_period = LimitPeriods.QUARTERLY
        self.quarterly_component.save()

        self.plan_component = self.fixture.plan.components.first()
        self.plan_component.component = self.quarterly_component
        self.plan_component.price = 10
        self.plan_component.save()

        self.resource = self.fixture.resource
        self.resource.limits = {"cpu": 10}
        self.resource.save()
        self.resource.set_state_ok()
        self.resource.save()

    def test_initial_quarterly_quantity_equals_limit(self):
        """Before any changes, the invoice quantity should equal the limit value."""
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=1
        )
        item = invoice.items.get(resource_id=self.resource.id)
        self.assertEqual(item.quantity, 10)
        # Total cost = 10 * 10 = 100
        self.assertEqual(item.total, 100)

    def test_limit_change_mid_quarter_should_not_sum_old_and_new_limits(self):
        """When limit changes from 10 to 20 mid-quarter, the new quantity
        should NOT be 10 + 20 = 30 (the naive sum). It should be prorated
        so the total is between the old cost (10 * price) and new cost (20 * price).

        With Q1 being Jan 1 - Mar 31 (91 days), and the change happening on Feb 15
        (day 46 of the quarter):
        - Old limit (10) applies for 45 days (Jan 1 - Feb 14)
        - New limit (20) applies for 46 days (Feb 15 - Mar 31)
        - Prorated quantity = 10 * (45/91) + 20 * (46/91) ≈ 4.95 + 10.11 ≈ 15.05
        - The quantity must be less than 20 (the new full-quarter amount)
        """
        with freeze_time("2020-02-15"):
            self.resource.limits = {"cpu": 20}
            self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=1
        )
        item = invoice.items.get(resource_id=self.resource.id)

        # BUG: The current implementation gives quantity = 10 + 20 = 30
        # which means the customer pays 30 * 10 = 300 instead of ~150
        # The quantity should be at most 20 (the new limit for the full quarter)
        self.assertLessEqual(
            item.quantity,
            20,
            f"Quantity {item.quantity} exceeds the new limit of 20. "
            "The old and new limits are being summed instead of prorated. "
            f"Expected a value between 10 and 20, got {item.quantity}.",
        )

    def test_limit_increase_total_cost_is_between_old_and_new_full_quarter_costs(self):
        """When increasing the limit mid-quarter, the total cost should be
        prorated based on how long each limit was active.

        Q1 2020 = 91 days (Jan 31 + Feb 29 [leap year] + Mar 31).
        Old limit (10) active for 45 days (Jan 1 – Feb 14).
        New limit (20) active for 46 days (Feb 15 – Mar 31).
        Prorated quantity = 10*45/91 + 20*46/91 = 1370/91 ≈ 15.055.
        Total = quantize_price(15.055 * 10) = 150.55.
        """
        with freeze_time("2020-02-15"):
            self.resource.limits = {"cpu": 20}
            self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=1
        )
        item = invoice.items.get(resource_id=self.resource.id)

        self.assertEqual(item.total, Decimal("150.55"))

    def test_limit_decrease_mid_quarter_should_not_sum_limits(self):
        """When limit DECREASES from 20 to 5 mid-quarter, the quantity should
        be prorated, not summed to 25."""
        # Start with limit 20
        self.resource.limits = {"cpu": 20}
        self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=1
        )
        item = invoice.items.get(resource_id=self.resource.id)
        self.assertEqual(item.quantity, 20)

        with freeze_time("2020-02-15"):
            self.resource.limits = {"cpu": 5}
            self.resource.save()

        item.refresh_from_db()

        # BUG: Current implementation gives 20 + 5 = 25
        # Expected: prorated value between 5 and 20
        self.assertLessEqual(
            item.quantity,
            20,
            f"Quantity {item.quantity} exceeds the original limit of 20. "
            "Limits are being summed instead of prorated.",
        )
        self.assertGreaterEqual(
            item.quantity,
            5,
            f"Quantity {item.quantity} is less than the new limit of 5.",
        )

    def test_multiple_limit_changes_should_prorate_all_periods(self):
        """Multiple limit changes within the same quarter should each be
        prorated based on their active duration, not summed."""
        with freeze_time("2020-02-01"):
            self.resource.limits = {"cpu": 20}
            self.resource.save()

        with freeze_time("2020-03-01"):
            self.resource.limits = {"cpu": 30}
            self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=1
        )
        item = invoice.items.get(resource_id=self.resource.id)

        # BUG: Current implementation gives 10 + 20 + 30 = 60
        # Expected: prorated value ≤ 30 (the maximum limit)
        self.assertLessEqual(
            item.quantity,
            30,
            f"Quantity {item.quantity} exceeds the maximum limit of 30. "
            "Multiple limit values are being summed instead of prorated. "
            f"With 3 period changes, the naive sum would be 10+20+30=60.",
        )


@ddt
class AnnualBillingMonthDetectionTest(test.APITestCase):
    """Test anniversary-based annual billing month detection logic."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        # Resource created in March
        self.resource.created = timezone.datetime(2020, 3, 15, tzinfo=UTC)
        self.resource.save()

    @data(
        (1, False),
        (2, False),
        (3, True),  # March - resource creation month
        (4, False),
        (5, False),
        (6, False),
        (7, False),
        (8, False),
        (9, False),
        (10, False),
        (11, False),
        (12, False),
    )
    def test_annual_billing_triggers_on_creation_month(self, month_and_expected):
        """Test that annual billing triggers on the resource's creation month."""
        month, expected = month_and_expected

        test_date = timezone.datetime(2020, month, 15)

        result = LimitPeriodProcessor._should_process_billing(
            LimitPeriods.ANNUAL, test_date, self.resource
        )
        self.assertEqual(
            result, expected, f"Month {month} annual billing detection failed"
        )

    def test_annual_billing_without_resource_returns_false(self):
        """Test that annual billing returns False when no resource is provided."""
        test_date = timezone.datetime(2020, 3, 15)
        result = LimitPeriodProcessor._should_process_billing(
            LimitPeriods.ANNUAL, test_date
        )
        self.assertFalse(result)

    def test_annual_billing_period_from_creation_date(self):
        """Test annual billing period is based on resource creation anniversary."""
        test_date = timezone.datetime(2020, 3, 20, tzinfo=UTC)
        start, end = LimitPeriodProcessor._get_billing_period(
            LimitPeriods.ANNUAL, test_date, self.resource
        )
        self.assertEqual(start.month, 3)
        self.assertEqual(start.day, 15)
        self.assertEqual(start.year, 2020)
        self.assertEqual(end.month, 3)
        self.assertEqual(end.day, 14)
        self.assertEqual(end.year, 2021)

    def test_annual_billing_period_before_anniversary(self):
        """Test annual billing period when date is before this year's anniversary."""
        test_date = timezone.datetime(2021, 2, 10, tzinfo=UTC)
        start, end = LimitPeriodProcessor._get_billing_period(
            LimitPeriods.ANNUAL, test_date, self.resource
        )
        # Should use previous year's anniversary as start
        self.assertEqual(start.month, 3)
        self.assertEqual(start.day, 15)
        self.assertEqual(start.year, 2020)
        self.assertEqual(end.month, 3)
        self.assertEqual(end.day, 14)
        self.assertEqual(end.year, 2021)


@freeze_time("2020-03-01")
class AnnualBillingIntegrationTest(test.APITestCase):
    """Integration test for anniversary-based annual billing with create_monthly_invoices task."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        self.annual_component = self.fixture.offering.components.first()
        self.annual_component.billing_type = BillingTypes.LIMIT
        self.annual_component.limit_period = LimitPeriods.ANNUAL
        self.annual_component.save()

        self.plan_component = self.fixture.plan.components.first()
        self.plan_component.component = self.annual_component
        self.plan_component.save()

        self.resource = self.fixture.resource
        self.resource.limits = {"cpu": 2}
        self.resource.save()
        self.resource.set_state_ok()
        self.resource.save()
        # Resource created in March (via freeze_time)

    def test_annual_billing_on_creation_anniversary_month(self):
        """Test that annual billing creates items on the resource's creation month."""
        # Initial invoice should exist for March (resource creation month)
        march_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=3
        )
        march_items = march_invoice.items.filter(resource_id=self.resource.id)
        self.assertEqual(march_items.count(), 1, "March should have annual billing")

        # Verify the billing period spans 12 months from creation
        item = march_items.first()
        self.assertEqual(item.start.month, 3)
        self.assertEqual(item.start.day, 1)
        self.assertEqual(item.start.year, 2020)
        self.assertEqual(item.end.month, 2)
        self.assertEqual(item.end.year, 2021)

        # Run monthly task for April through February - none should create annual items
        for month in range(4, 13):
            with freeze_time(f"2020-{month:02d}-01"):
                create_monthly_invoices()

            invoice = invoices_models.Invoice.objects.get(
                customer=self.resource.project.customer, year=2020, month=month
            )
            items = invoice.items.filter(resource_id=self.resource.id)
            self.assertEqual(
                items.count(),
                0,
                f"Month {month}/2020 should not have annual billing",
            )

        # January and February of next year should also not have annual items
        for month in [1, 2]:
            with freeze_time(f"2021-{month:02d}-01"):
                create_monthly_invoices()

            invoice = invoices_models.Invoice.objects.get(
                customer=self.resource.project.customer, year=2021, month=month
            )
            items = invoice.items.filter(resource_id=self.resource.id)
            self.assertEqual(
                items.count(),
                0,
                f"Month {month}/2021 should not have annual billing",
            )

        # Next March (anniversary month) should create annual items again
        with freeze_time("2021-03-01"):
            create_monthly_invoices()

        next_march_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2021, month=3
        )
        next_march_items = next_march_invoice.items.filter(resource_id=self.resource.id)
        self.assertEqual(
            next_march_items.count(),
            1,
            "Next March should have annual billing",
        )


@freeze_time("2020-06-01")
class AnnualAndMonthlyMixedBillingTest(test.APITestCase):
    """Test that annual and monthly components are billed correctly together
    using anniversary-based annual billing."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

        # Create annual limit component
        self.annual_component = self.fixture.offering.components.first()
        self.annual_component.type = "annual_cpu"
        self.annual_component.billing_type = BillingTypes.LIMIT
        self.annual_component.limit_period = LimitPeriods.ANNUAL
        self.annual_component.save()

        # Create monthly limit component
        self.monthly_component = marketplace_models.OfferingComponent.objects.create(
            offering=self.fixture.offering,
            type="monthly_cpu",
            name="Monthly CPU",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )

        # Set up plan components
        annual_plan_component = self.fixture.plan.components.first()
        annual_plan_component.component = self.annual_component
        annual_plan_component.save()

        marketplace_models.PlanComponent.objects.create(
            plan=self.fixture.plan,
            component=self.monthly_component,
            price=5,
        )

        self.resource.limits = {"annual_cpu": 10, "monthly_cpu": 5}
        self.resource.save()
        self.resource.set_state_ok()
        self.resource.save()
        # Resource created in June (via freeze_time)

    def test_mixed_billing_on_creation_month(self):
        """Test that both annual and monthly components are billed on creation month."""
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=6
        )

        annual_items = invoice.items.filter(
            details__offering_component_type="annual_cpu"
        )
        monthly_items = invoice.items.filter(
            details__offering_component_type="monthly_cpu"
        )

        self.assertEqual(
            annual_items.count(),
            1,
            "Annual component should be billed in June (creation month)",
        )
        self.assertEqual(
            monthly_items.count(), 1, "Monthly component should be billed in June"
        )

    @freeze_time("2020-07-15")
    def test_only_monthly_billing_in_non_anniversary_month(self):
        """Test that only monthly components are billed in non-anniversary months."""
        create_monthly_invoices()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=7
        )

        annual_items = invoice.items.filter(
            details__offering_component_type="annual_cpu"
        )
        monthly_items = invoice.items.filter(
            details__offering_component_type="monthly_cpu"
        )

        self.assertEqual(
            annual_items.count(),
            0,
            "Annual component should NOT be billed in July",
        )
        self.assertEqual(
            monthly_items.count(),
            1,
            "Monthly component should be billed in July",
        )


@freeze_time("2024-10-03")
class LimitBillingDuplicateInvoiceTest(test.APITestCase):
    """Test that reproduces the issue where LIMIT components get incorrectly billed during monthly invoice creation."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Create LIMIT components with TOTAL period (like the real scenario)
        self.cpu_component = marketplace_factories.OfferingComponentFactory(
            offering=self.fixture.offering,
            type="cpu_k_hours",
            name="CPU allocation",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
            measured_unit="CPU k hours",
        )

        self.storage_component = marketplace_factories.OfferingComponentFactory(
            offering=self.fixture.offering,
            type="gb_k_hours",
            name="Storage allocation",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
            measured_unit="TB-hours",
        )

        # Create plan components with pricing
        self.cpu_plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.fixture.plan,
            component=self.cpu_component,
            price=8.0,  # Same as in real data
        )

        self.storage_plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.fixture.plan,
            component=self.storage_component,
            price=0.0106,  # Same as in real data
        )

        # Set up resource with initial limits (like October 2024 creation)
        self.resource = self.fixture.resource
        self.resource.limits = {
            "cpu_k_hours": 4,
            "gb_k_hours": 10,
        }
        self.resource.save()

        # Transition resource to OK state (triggers CREATE billing)
        self.resource.set_state_ok()
        self.resource.save()

    def test_update_orders_create_correct_invoice_items(self):
        """Test that UPDATE orders create incremental invoice items correctly."""
        # Verify initial CREATE invoice was created
        october_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2024, month=10
        )
        initial_items = october_invoice.items.filter(resource_id=self.resource.id)

        # Should have invoice items for our LIMIT components (and possibly others from fixture)
        cpu_items = initial_items.filter(details__offering_component_type="cpu_k_hours")
        storage_items = initial_items.filter(
            details__offering_component_type="gb_k_hours"
        )

        self.assertEqual(cpu_items.count(), 1, "Should have 1 CPU invoice item")
        self.assertEqual(storage_items.count(), 1, "Should have 1 storage invoice item")

        # Simulate UPDATE order in January 2025 (like the real scenario)
        with freeze_time("2025-01-24"):
            # Update limits (like first UPDATE order in real data)
            self.resource.limits.copy()
            self.resource.limits = {
                "cpu_k_hours": 500,  # Increase from 4 to 500
                "gb_k_hours": 25,  # Increase from 10 to 25
            }
            self.resource.save()

            # Verify January invoice items were created for incremental changes
            january_invoice = invoices_models.Invoice.objects.get(
                customer=self.resource.project.customer, year=2025, month=1
            )
            january_items = january_invoice.items.filter(resource_id=self.resource.id)

            # Should have invoice items for the incremental changes
            cpu_item = january_items.filter(
                details__offering_component_type="cpu_k_hours"
            ).first()
            storage_item = january_items.filter(
                details__offering_component_type="gb_k_hours"
            ).first()

            self.assertIsNotNone(cpu_item, "Should have CPU incremental invoice item")
            self.assertIsNotNone(
                storage_item, "Should have storage incremental invoice item"
            )

            # Verify the incremental quantities are correct
            # CPU: increased from 4 to 500, so increment is 496
            expected_cpu_increment = 500 - 4
            self.assertEqual(
                float(cpu_item.quantity),
                expected_cpu_increment,
                f"CPU invoice item should have incremental quantity {expected_cpu_increment}, "
                f"but got {cpu_item.quantity}",
            )

            # Storage: increased from 10 to 25, so increment is 15
            expected_storage_increment = 25 - 10
            self.assertEqual(
                float(storage_item.quantity),
                expected_storage_increment,
                f"Storage invoice item should have incremental quantity {expected_storage_increment}, "
                f"but got {storage_item.quantity}",
            )

            # Verify pricing is correct (incremental quantity × unit price)
            expected_cpu_price = expected_cpu_increment * 8.0  # 496 * 8.0 = 3968.0
            expected_storage_price = (
                expected_storage_increment * 0.0106
            )  # 15 * 0.0106 = 0.159

            self.assertEqual(
                float(cpu_item.price),
                expected_cpu_price,
                f"CPU invoice item price should be {expected_cpu_price}, got {cpu_item.price}",
            )

            # For storage, use appropriate decimal precision (rounded to 2 decimal places like currency)
            self.assertAlmostEqual(
                float(storage_item.price),
                expected_storage_price,
                places=2,
                msg=f"Storage invoice item price should be approximately {expected_storage_price}, got {storage_item.price}",
            )

    def test_monthly_invoice_task_reproduces_real_scenario_bug(self):
        """Test that reproduces the bug: monthly invoice creation incorrectly bills LIMIT components."""
        # First, simulate multiple UPDATE orders like in the real scenario
        update_dates_and_limits = [
            ("2024-11-14", {"cpu_k_hours": 220, "gb_k_hours": 10}),
            ("2024-12-22", {"cpu_k_hours": 220, "gb_k_hours": 25}),
            ("2025-01-24", {"cpu_k_hours": 500, "gb_k_hours": 25}),
            ("2025-02-06", {"cpu_k_hours": 1500, "gb_k_hours": 50}),
            ("2025-03-19", {"cpu_k_hours": 2000, "gb_k_hours": 50}),
            ("2025-04-08", {"cpu_k_hours": 2000, "gb_k_hours": 100}),
            ("2025-07-10", {"cpu_k_hours": 3000, "gb_k_hours": 100}),
        ]

        for update_date, new_limits in update_dates_and_limits:
            with freeze_time(update_date):
                self.resource.limits = new_limits
                self.resource.save()

                # Verify that invoice items are created for each UPDATE order
                update_datetime = timezone.datetime.strptime(update_date, "%Y-%m-%d")
                update_invoice = invoices_models.Invoice.objects.get(
                    customer=self.resource.project.customer,
                    year=update_datetime.year,
                    month=update_datetime.month,
                )

                update_items = update_invoice.items.filter(resource_id=self.resource.id)
                cpu_items = update_items.filter(
                    details__offering_component_type="cpu_k_hours"
                )
                storage_items = update_items.filter(
                    details__offering_component_type="gb_k_hours"
                )

                # Should have incremental invoice items for components that actually changed
                # Get previous limits to check what changed
                prev_limits = getattr(
                    self.resource, "_prev_limits", {"cpu_k_hours": 4, "gb_k_hours": 10}
                )

                if prev_limits.get("cpu_k_hours", 0) != new_limits.get(
                    "cpu_k_hours", 0
                ):
                    self.assertGreaterEqual(
                        cpu_items.count(),
                        1,
                        f"Should have CPU invoice items for UPDATE in {update_date} "
                        f"(changed from {prev_limits.get('cpu_k_hours', 0)} to {new_limits.get('cpu_k_hours', 0)})",
                    )

                if prev_limits.get("gb_k_hours", 0) != new_limits.get("gb_k_hours", 0):
                    self.assertGreaterEqual(
                        storage_items.count(),
                        1,
                        f"Should have storage invoice items for UPDATE in {update_date} "
                        f"(changed from {prev_limits.get('gb_k_hours', 0)} to {new_limits.get('gb_k_hours', 0)})",
                    )

                # Store current limits for next iteration
                self.resource._prev_limits = new_limits.copy()

        # Now run monthly invoice creation for September 2025 (when the bug occurred)
        with freeze_time("2025-09-01"):
            create_monthly_invoices()

        # Check September invoice
        september_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2025, month=9
        )
        september_items = september_invoice.items.filter(resource_id=self.resource.id)

        # BUG: The monthly task incorrectly creates invoice items for LIMIT components
        # that already have invoice items from UPDATE orders
        cpu_items = september_items.filter(
            details__offering_component_type="cpu_k_hours"
        )
        storage_items = september_items.filter(
            details__offering_component_type="gb_k_hours"
        )

        # This is the bug - these items should NOT exist because the resource
        # already has invoice items from previous CREATE and UPDATE orders

        # If items were incorrectly created, show the bug details
        if cpu_items.exists():
            cpu_item = cpu_items.first()
            # This is the bug - billing the full current limit (3000) instead of incremental
            self.fail(
                f"Bug reproduced: Monthly task created CPU invoice item with quantity {cpu_item.quantity}, "
                f"unit_price {cpu_item.unit_price}, total {cpu_item.price}. "
                f"This should not happen for resources with existing LIMIT billing."
            )

        if storage_items.exists():
            storage_item = storage_items.first()
            self.fail(
                f"Bug reproduced: Monthly task created storage invoice item with quantity {storage_item.quantity}, "
                f"unit_price {storage_item.unit_price}, total {storage_item.price}. "
                f"This should not happen for resources with existing LIMIT billing."
            )

        # With the fix, the monthly task should NOT create invoice items for TOTAL components
        # that already have existing billing
        self.assertEqual(
            cpu_items.count(),
            0,
            "Monthly invoice task should NOT create CPU items for TOTAL components with existing billing",
        )
        self.assertEqual(
            storage_items.count(),
            0,
            "Monthly invoice task should NOT create storage items for TOTAL components with existing billing",
        )

    def test_total_limit_update_without_initial_billing_creates_full_item(self):
        """Test UPDATE billing recovery: When initial billing is missing, UPDATE bills full amount."""

        # Delete the initial invoice items that were created during setup
        # This simulates the case where initial CREATE billing failed or was skipped
        initial_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2024, month=10
        )
        initial_invoice.items.filter(resource_id=self.resource.id).delete()

        # Verify no invoice items exist initially
        all_items = invoices_models.InvoiceItem.objects.filter(
            resource_id=self.resource.id
        )
        self.assertEqual(all_items.count(), 0, "Should have no initial invoice items")

        # Now simulate an UPDATE order that changes limits (like in July 2025)
        with freeze_time("2025-07-10"):
            self.resource.limits = {
                "cpu_k_hours": 3000,  # Increase from 4 to 3000
                "gb_k_hours": 100,  # Increase from 10 to 100
            }
            self.resource.save()

            # Check July invoice items
            july_invoice = invoices_models.Invoice.objects.get(
                customer=self.resource.project.customer, year=2025, month=7
            )
            july_items = july_invoice.items.filter(resource_id=self.resource.id)

            cpu_items = july_items.filter(
                details__offering_component_type="cpu_k_hours"
            )
            storage_items = july_items.filter(
                details__offering_component_type="gb_k_hours"
            )

            # This is now the EXPECTED behavior: UPDATE should create full billing
            # when no previous billing exists (recovery from missing CREATE billing)
            self.assertEqual(
                cpu_items.count(),
                1,
                "UPDATE should create CPU billing when no previous billing exists",
            )
            self.assertEqual(
                storage_items.count(),
                1,
                "UPDATE should create storage billing when no previous billing exists",
            )

            cpu_item = cpu_items.first()
            storage_item = storage_items.first()

            # Verify billing amounts are correct (full amounts, not incremental)
            self.assertEqual(
                float(cpu_item.quantity),
                3000,
                "CPU item should bill full amount (3000) when no previous billing exists",
            )
            self.assertEqual(
                float(storage_item.quantity),
                100,
                "Storage item should bill full amount (100) when no previous billing exists",
            )

            # Verify pricing is correct
            expected_cpu_price = 3000 * 8.0  # 3000 * cpu_plan_component.price
            expected_storage_price = 100 * 0.0106  # 100 * storage_plan_component.price

            self.assertEqual(
                float(cpu_item.price),
                expected_cpu_price,
                f"CPU item price should be {expected_cpu_price}",
            )
            self.assertAlmostEqual(
                float(storage_item.price),
                expected_storage_price,
                places=2,
                msg=f"Storage item price should be approximately {expected_storage_price}",
            )

    def test_direct_create_component_item_is_prevented_for_total_with_existing_billing(
        self,
    ):
        """Test that create_component_item prevents duplicate billing for TOTAL components."""
        # Verify initial invoice items exist
        initial_items = invoices_models.InvoiceItem.objects.filter(
            resource_id=self.resource.id
        )
        self.assertGreater(
            initial_items.count(), 0, "Should have initial invoice items"
        )

        # Try to directly call create_component_item for a TOTAL component that already has billing
        # This simulates whatever path caused the September 2025 bug
        with freeze_time("2025-09-01"):
            september_invoice = invoices_models.Invoice.objects.create(
                customer=self.resource.project.customer, year=2025, month=9
            )

            # This should be prevented by the safeguard
            LimitPeriodProcessor._create_invoice_item(
                source=self.resource,
                plan_component=self.cpu_plan_component,
                invoice=september_invoice,
                start=timezone.now(),
                end=timezone.now() + timedelta(days=30),
            )

            # Check that no duplicate invoice item was created
            september_items = september_invoice.items.filter(
                details__offering_component_type="cpu_k_hours"
            )
            self.assertEqual(
                september_items.count(),
                0,
                "create_component_item should prevent duplicate billing for TOTAL components",
            )

    def test_update_component_item_handles_multiple_plans(self):
        """
        Test that update_component_item does not fail with MultipleObjectsReturned error.
        """
        offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.fixture.offering,
            type="cpu_limit",
            name="CPU Limit",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )

        old_plan = marketplace_factories.PlanFactory(
            offering=self.fixture.offering,
            name="Old Plan",
            unit_price=0,
            unit=marketplace_models.Plan.Units.PER_MONTH,
        )
        new_plan = marketplace_factories.PlanFactory(
            offering=self.fixture.offering,
            name="New Plan",
            unit_price=0,
            unit=marketplace_models.Plan.Units.PER_MONTH,
        )

        old_plan_component = marketplace_factories.PlanComponentFactory(
            plan=old_plan,
            component=offering_component,
            price=2.0,
        )
        new_plan_component = marketplace_factories.PlanComponentFactory(
            plan=new_plan,
            component=offering_component,
            price=2.0,
        )

        # Create resource in CREATING state to avoid auto-billing,
        # then switch to OK so _update_invoice_item can work.
        test_resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.fixture.offering,
            plan=old_plan,
            limits={offering_component.type: 100},
            state=marketplace_models.ResourceStates.CREATING,
        )
        marketplace_models.Resource.objects.filter(pk=test_resource.pk).update(
            state=marketplace_models.ResourceStates.OK,
        )
        test_resource.refresh_from_db()

        # Use dates consistent with the frozen time (2024-10-03)
        october_invoice, _ = invoices_models.Invoice.objects.get_or_create(
            customer=test_resource.project.customer, year=2024, month=10
        )

        october_1st = timezone.datetime(2024, 10, 1, tzinfo=UTC)
        october_15th = timezone.datetime(2024, 10, 15, tzinfo=UTC)
        october_31st = timezone.datetime(2024, 10, 31, tzinfo=UTC)

        LimitPeriodProcessor._create_invoice_item(
            source=test_resource,
            plan_component=old_plan_component,
            invoice=october_invoice,
            start=october_1st,
            end=october_15th,
        )

        # Use raw update to avoid triggering plan change billing handlers
        marketplace_models.Resource.objects.filter(pk=test_resource.pk).update(
            plan=new_plan,
        )
        test_resource.refresh_from_db()

        LimitPeriodProcessor._create_invoice_item(
            source=test_resource,
            plan_component=new_plan_component,
            invoice=october_invoice,
            start=october_15th,
            end=october_31st,
        )

        items = invoices_models.InvoiceItem.objects.filter(
            resource=test_resource,
            details__offering_component_type=offering_component.type,
            invoice=october_invoice,
            unit_price__gte=0,
        )
        self.assertEqual(
            items.count(), 2, "Should have 2 invoice items due to plan switch"
        )

        LimitPeriodProcessor._update_invoice_item(
            resource=test_resource,
            component_type=offering_component.type,
            invoice=october_invoice,
            new_quantity=150,
        )

        updated_items = invoices_models.InvoiceItem.objects.filter(
            resource=test_resource,
            details__offering_component_type=offering_component.type,
            invoice=october_invoice,
            unit_price__gte=0,
        )

        total_quantity = sum(item.quantity for item in updated_items)
        self.assertGreater(total_quantity, 100, "Total quantity should be updated")


@freeze_time("2020-11-01")
class NonBillableOfferingTest(test.APITestCase):
    """
    Test that resources with non-billable offerings are not billed.

    This tests the fix for the billing regression introduced in commit 236599072
    where MarketplaceBillingService.get_or_create_invoice() did not filter
    by offering.billable=True, causing resources with billable=False offerings
    to be incorrectly billed.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # Make the offering non-billable
        self.fixture.offering.billable = False
        self.fixture.offering.save()
        self.resource = self.fixture.resource

    def test_non_billable_offering_does_not_create_invoice_item_on_resource_activation(
        self,
    ):
        """Test that activating a resource with non-billable offering doesn't create invoice items."""
        self.resource.set_state_ok()
        self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        items = invoice.items.filter(resource_id=self.resource.id)

        self.assertEqual(
            items.count(),
            0,
            "Non-billable offering should not create invoice items on resource activation",
        )

    def test_non_billable_offering_does_not_create_invoice_item_on_monthly_billing(
        self,
    ):
        """Test that monthly invoice creation doesn't bill non-billable offerings."""
        self.resource.set_state_ok()
        self.resource.save()

        # Advance to next month and run monthly invoices
        with freeze_time("2020-12-01"):
            create_monthly_invoices()

        december_invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=12
        )
        items = december_invoice.items.filter(resource_id=self.resource.id)

        self.assertEqual(
            items.count(),
            0,
            "Non-billable offering should not create invoice items on monthly billing",
        )

    def test_billable_offering_creates_invoice_item_regression_check(self):
        """Regression test: Ensure billable offerings still create invoice items correctly."""
        # Make the offering billable again
        self.fixture.offering.billable = True
        self.fixture.offering.save()

        self.resource.set_state_ok()
        self.resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        items = invoice.items.filter(resource_id=self.resource.id)

        self.assertEqual(
            items.count(),
            1,
            "Billable offering should create invoice items on resource activation",
        )


@freeze_time("2020-11-01")
class NonBillableChildOfferingTest(test.APITestCase):
    """
    Test billing behavior for child offerings (like OpenStack.Instance)
    that are nested under parent offerings (like OpenStack.Tenant).

    Child offerings typically have billable=False because their costs
    are included in the parent offering's billing.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Create a parent offering (like OpenStack.Tenant) - billable
        self.parent_offering = marketplace_factories.OfferingFactory(
            type="OpenStack.Tenant",
            state=OfferingStates.ACTIVE,
            billable=True,
            customer=self.fixture.offering_customer,
        )
        self.parent_plan = marketplace_factories.PlanFactory(
            offering=self.parent_offering,
            unit=marketplace_models.Plan.Units.PER_MONTH,
        )
        self.parent_component = marketplace_factories.OfferingComponentFactory(
            offering=self.parent_offering,
            billing_type=BillingTypes.FIXED,
        )
        marketplace_factories.PlanComponentFactory(
            plan=self.parent_plan,
            component=self.parent_component,
            price=10,
            amount=1,
        )
        self.parent_resource = marketplace_factories.ResourceFactory(
            offering=self.parent_offering,
            plan=self.parent_plan,
            project=self.fixture.project,
        )

        # Create a child offering (like OpenStack.Instance) - NOT billable
        self.child_offering = marketplace_factories.OfferingFactory(
            type="OpenStack.Instance",
            state=OfferingStates.ACTIVE,
            billable=False,  # Key: child offerings are not billed separately
            parent=self.parent_offering,
            customer=self.fixture.offering_customer,
        )
        self.child_plan = marketplace_factories.PlanFactory(
            offering=self.child_offering,
            unit=marketplace_models.Plan.Units.PER_MONTH,
        )
        self.child_component = marketplace_factories.OfferingComponentFactory(
            offering=self.child_offering,
            billing_type=BillingTypes.FIXED,
        )
        marketplace_factories.PlanComponentFactory(
            plan=self.child_plan,
            component=self.child_component,
            price=5,
            amount=1,
        )
        self.child_resource = marketplace_factories.ResourceFactory(
            offering=self.child_offering,
            plan=self.child_plan,
            project=self.fixture.project,
            parent=self.parent_resource,
        )

    def test_parent_billable_offering_creates_invoice_items(self):
        """Parent offering with billable=True should create invoice items."""
        self.parent_resource.set_state_ok()
        self.parent_resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.parent_resource.project.customer, year=2020, month=11
        )
        items = invoice.items.filter(resource_id=self.parent_resource.id)

        self.assertEqual(
            items.count(),
            1,
            "Parent billable offering should create invoice items",
        )

    def test_child_non_billable_offering_does_not_create_invoice_items(self):
        """Child offering with billable=False should NOT create invoice items."""
        self.child_resource.set_state_ok()
        self.child_resource.save()

        invoice = invoices_models.Invoice.objects.get(
            customer=self.child_resource.project.customer, year=2020, month=11
        )
        items = invoice.items.filter(resource_id=self.child_resource.id)

        self.assertEqual(
            items.count(),
            0,
            "Child non-billable offering should NOT create invoice items",
        )

    def test_monthly_billing_only_bills_parent_not_child(self):
        """Monthly invoice should only bill parent, not child offerings."""
        self.parent_resource.set_state_ok()
        self.parent_resource.save()
        self.child_resource.set_state_ok()
        self.child_resource.save()

        with freeze_time("2020-12-01"):
            create_monthly_invoices()

        december_invoice = invoices_models.Invoice.objects.get(
            customer=self.parent_resource.project.customer, year=2020, month=12
        )

        parent_items = december_invoice.items.filter(
            resource_id=self.parent_resource.id
        )
        child_items = december_invoice.items.filter(resource_id=self.child_resource.id)

        self.assertEqual(
            parent_items.count(),
            1,
            "Parent offering should be billed monthly",
        )
        self.assertEqual(
            child_items.count(),
            0,
            "Child non-billable offering should NOT be billed monthly",
        )


@freeze_time("2026-06-15")
class GetOrCreateInvoiceWithDateInputTest(test.APITestCase):
    """
    Regression test for CSCS-5AK.

    `process_component_usage_billing` passes `ComponentUsage.billing_period`
    (a `datetime.date`) into `MarketplaceBillingService.get_or_create_invoice`.
    When that call creates a new invoice and bulk-processes the customer's
    existing LIMIT-billed resources, the downstream period arithmetic in
    `serialize_resource_limit_period` must not blow up with
    `TypeError: unsupported operand type(s) for -: 'datetime.datetime' and 'datetime.date'`.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.component = self.fixture.offering_component
        self.component.billing_type = BillingTypes.LIMIT
        self.component.limit_period = LimitPeriods.MONTH
        self.component.save()

        self.resource = ResourceFactory(
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            project=self.fixture.project,
            limits={self.component.type: 10},
        )
        self.resource.set_state_ok()
        self.resource.save()

    def test_get_or_create_invoice_accepts_date_for_new_month(self):
        future_month_date = datetime.date(2026, 7, 1)

        invoice, created = MarketplaceBillingService.get_or_create_invoice(
            self.resource.project.customer, future_month_date
        )

        self.assertTrue(created)
        self.assertEqual(invoice.year, 2026)
        self.assertEqual(invoice.month, 7)
        # Bulk-processing must have produced an item for the LIMIT resource
        # without raising on the date/datetime subtraction.
        self.assertEqual(
            invoice.items.filter(
                resource_id=self.resource.id,
                details__offering_component_type=self.component.type,
            ).count(),
            1,
        )
