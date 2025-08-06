from ddt import data, ddt
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test
from rest_framework.reverse import reverse

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tasks import create_monthly_invoices
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests.factories import ResourceFactory

from . import fixtures


@freeze_time("2020-11-01")
class InvoiceTest(test.APITransactionTestCase):
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
class TotalLimitTest(test.APITransactionTestCase):
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


@ddt
@freeze_time("2020-11-01")
class InvoiceItemsTest(test.APITransactionTestCase):
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
class QuarterlyBillingTest(test.APITransactionTestCase):
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
class QuarterlyVsMonthlyBillingTest(test.APITransactionTestCase):
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
class QuarterlyBillingMonthDetectionTest(test.APITransactionTestCase):
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

        # Import the registrator to test the method
        from waldur_mastermind.marketplace.registrators import MarketplaceRegistrator

        # Create a test date for the given month
        test_date = timezone.datetime(2020, month, 15)

        result = MarketplaceRegistrator.should_process_quarterly_billing(test_date)
        self.assertEqual(
            result, expected, f"Month {month} quarterly billing detection failed"
        )

    def test_quarterly_billing_period_calculation(self):
        """Test quarterly billing period calculation for each quarter."""
        from waldur_mastermind.marketplace.registrators import MarketplaceRegistrator

        # Test Q1 (January)
        q1_date = timezone.datetime(2020, 1, 15)
        q1_start, q1_end = MarketplaceRegistrator.get_quarterly_billing_period(q1_date)
        self.assertEqual(q1_start.month, 1)
        self.assertEqual(q1_start.day, 1)
        self.assertEqual(q1_end.month, 3)
        self.assertEqual(q1_end.day, 31)

        # Test Q2 (April)
        q2_date = timezone.datetime(2020, 4, 15)
        q2_start, q2_end = MarketplaceRegistrator.get_quarterly_billing_period(q2_date)
        self.assertEqual(q2_start.month, 4)
        self.assertEqual(q2_start.day, 1)
        self.assertEqual(q2_end.month, 6)
        self.assertEqual(q2_end.day, 30)

        # Test Q3 (July)
        q3_date = timezone.datetime(2020, 7, 15)
        q3_start, q3_end = MarketplaceRegistrator.get_quarterly_billing_period(q3_date)
        self.assertEqual(q3_start.month, 7)
        self.assertEqual(q3_start.day, 1)
        self.assertEqual(q3_end.month, 9)
        self.assertEqual(q3_end.day, 30)

        # Test Q4 (October)
        q4_date = timezone.datetime(2020, 10, 15)
        q4_start, q4_end = MarketplaceRegistrator.get_quarterly_billing_period(q4_date)
        self.assertEqual(q4_start.month, 10)
        self.assertEqual(q4_start.day, 1)
        self.assertEqual(q4_end.month, 12)
        self.assertEqual(q4_end.day, 31)


@freeze_time("2020-01-01")
class QuarterlyBillingIntegrationTest(test.APITransactionTestCase):
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
