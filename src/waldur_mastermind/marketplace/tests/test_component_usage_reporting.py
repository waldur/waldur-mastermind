import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    ResourceStates,
)
from waldur_mastermind.marketplace.tasks import calculate_allocated_for_month
from waldur_mastermind.marketplace.tests import factories


class ComponentUsageReportingTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.staff = self.fixture.staff
        self.owner = self.fixture.owner
        self.user = self.fixture.user  # Regular user with no roles

        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
            limit_amount=100,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(plan=self.plan, component=self.component)
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

    # --- CONSUMPTION CALCULATOR TESTS ---

    def test_calculate_consumed_for_month(self):
        from waldur_mastermind.marketplace.tasks import calculate_consumed_for_month

        # Create usage for October 2023
        factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=10,
            billing_period=datetime.date(2023, 10, 1),
        )
        factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=25,
            billing_period=datetime.date(2023, 10, 1),
        )

        # Create usage for November 2023 (should be ignored)
        factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=50,
            billing_period=datetime.date(2023, 11, 1),
        )

        consumed = calculate_consumed_for_month(self.component, 2023, 10)
        self.assertEqual(consumed, Decimal("35"))

    def test_calculate_consumed_invalid_jsonb_data_safely_ignored(self):
        from waldur_mastermind.marketplace.tasks import calculate_consumed_for_month

        now = timezone.now()

        # Inject invalid string data instead of numbers
        self.resource.current_usages = {self.component.type: "unlimited"}
        self.resource.save()

        # Should catch ValueError/TypeError and return 0 (or fallback appropriately)
        consumed = calculate_consumed_for_month(self.component, now.year, now.month)
        self.assertEqual(consumed, Decimal("0"))

    # --- ALLOCATION CALCULATOR TESTS ---

    def test_calculate_allocated_current_month_live_limits(self):
        now = timezone.now()
        self.resource.limits = {self.component.type: 75}
        self.resource.save()

        # Another resource for the same component
        factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={self.component.type: 25},
        )

        allocated = calculate_allocated_for_month(self.component, now.year, now.month)
        self.assertEqual(allocated, Decimal("100"))

    def test_calculate_allocated_current_month_fallback(self):
        now = timezone.now()
        # No resources with custom limits
        self.resource.limits = {}
        self.resource.save()

        allocated = calculate_allocated_for_month(self.component, now.year, now.month)
        # Falls back to component.limit_amount
        self.assertEqual(allocated, Decimal("100"))

    def test_calculate_allocated_invalid_jsonb_limits_safely_ignored(self):
        now = timezone.now()
        self.resource.limits = {self.component.type: "invalid_limit"}
        self.resource.save()

        allocated = calculate_allocated_for_month(self.component, now.year, now.month)
        # Ignores the invalid limit and falls back to global
        self.assertEqual(allocated, Decimal("100"))

    def test_calculate_allocated_historical_non_limit(self):
        # Billing type is USAGE, not LIMIT
        allocated = calculate_allocated_for_month(self.component, 2023, 1)
        self.assertEqual(allocated, Decimal("100"))

    def test_calculate_allocated_historical_limit_total(self):
        self.component.billing_type = BillingTypes.LIMIT
        self.component.limit_period = LimitPeriods.TOTAL
        self.component.save()

        plan_component = models.PlanComponent.objects.get(
            plan=self.plan, component=self.component
        )

        # Invoice items representing limit changes
        invoice_factories.InvoiceItemFactory(
            plan_component=plan_component,
            quantity=50,
            invoice__year=2023,
            invoice__month=1,
            resource=self.resource,
        )
        invoice_factories.InvoiceItemFactory(
            plan_component=plan_component,
            quantity=25,
            invoice__year=2023,
            invoice__month=2,
            resource=self.resource,
        )
        invoice_factories.InvoiceItemFactory(
            plan_component=plan_component,
            quantity=-10,
            invoice__year=2023,
            invoice__month=3,
            resource=self.resource,
        )

        # Limit in Feb should be 50 + 25 = 75
        allocated = calculate_allocated_for_month(self.component, 2023, 2)
        self.assertEqual(allocated, Decimal("75"))

        # Limit in Mar should be 50 + 25 - 10 = 65
        allocated = calculate_allocated_for_month(self.component, 2023, 3)
        self.assertEqual(allocated, Decimal("65"))

    def test_calculate_allocated_historical_limit_month(self):
        self.component.billing_type = BillingTypes.LIMIT
        self.component.limit_period = LimitPeriods.MONTH
        self.component.save()

        plan_component = models.PlanComponent.objects.get(
            plan=self.plan, component=self.component
        )

        invoice_factories.InvoiceItemFactory(
            plan_component=plan_component,
            quantity=50,
            invoice__year=2023,
            invoice__month=1,
            resource=self.resource,
        )
        invoice_factories.InvoiceItemFactory(
            plan_component=plan_component,
            quantity=75,
            invoice__year=2023,
            invoice__month=2,
            resource=self.resource,
        )

        allocated = calculate_allocated_for_month(self.component, 2023, 2)
        self.assertEqual(allocated, Decimal("75"))

    def test_calculate_allocated_historical_limit_quarterly(self):
        self.component.billing_type = BillingTypes.LIMIT
        self.component.limit_period = LimitPeriods.QUARTERLY
        self.component.save()

        plan_component = models.PlanComponent.objects.get(
            plan=self.plan, component=self.component
        )

        # Q2 spans April(4), May(5), June(6).
        # Base Q2 invoice
        invoice_factories.InvoiceItemFactory(
            plan_component=plan_component,
            quantity=100,
            invoice__year=2023,
            invoice__month=4,
            resource=self.resource,
        )
        # Limit increased mid-quarter
        invoice_factories.InvoiceItemFactory(
            plan_component=plan_component,
            quantity=150,
            invoice__year=2023,
            invoice__month=5,
            resource=self.resource,
        )

        # Target month June (6) should look back and find the May (5) update as the latest
        allocated = calculate_allocated_for_month(self.component, 2023, 6)
        self.assertEqual(allocated, Decimal("150"))

        # Target month April (4) should only see the initial April invoice
        allocated = calculate_allocated_for_month(self.component, 2023, 4)
        self.assertEqual(allocated, Decimal("100"))

    # --- MANAGEMENT COMMAND TESTS ---

    def test_management_command_backfill_and_idempotency(self):
        # Pick the month relative to today rather than hardcoding one: the
        # command walks back from `now`, so a fixed date silently drops out of
        # the window once enough time passes.
        past_period = (timezone.now() - relativedelta(months=3)).date().replace(day=1)

        # Create usage for a past month
        factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=10,
            billing_period=past_period,
        )

        # 1. Run command for 6 months back
        with test.override_settings(CELERY_TASK_ALWAYS_EAGER=True):
            call_command("init_component_usage_reporting", months=6)

        # Check if record was created
        record = models.ComponentUsageMonthly.objects.filter(
            component=self.component,
            billing_period=past_period,
        ).first()

        self.assertIsNotNone(record)
        self.assertEqual(record.total_consumed, Decimal("10"))

        initial_count = models.ComponentUsageMonthly.objects.count()

        # 2. Run command again (Idempotency check)
        with test.override_settings(CELERY_TASK_ALWAYS_EAGER=True):
            call_command("init_component_usage_reporting", months=6)

        # Count should not increase, and record should remain intact
        self.assertEqual(models.ComponentUsageMonthly.objects.count(), initial_count)

    def test_management_command_skips_zero_zero_records(self):
        # Delete existing usage to ensure 0 consumed/0 allocated historically
        models.ComponentUsage.objects.all().delete()
        self.component.limit_amount = 0
        self.component.save()

        initial_count = models.ComponentUsageMonthly.objects.count()

        with test.override_settings(CELERY_TASK_ALWAYS_EAGER=True):
            call_command("init_component_usage_reporting", months=3)

        # It should skip creating rows for months where both consumed and allocated are 0
        self.assertEqual(models.ComponentUsageMonthly.objects.count(), initial_count)

    # --- API ENDPOINT TESTS ---

    def test_api_list_success(self):
        billing_period = datetime.date(2023, 10, 1)
        factories.ComponentUsageMonthlyFactory(
            component=self.component,
            billing_period=billing_period,
            total_consumed=30,
            total_allocated=100,
            usage_percent=30.0,
        )

        self.client.force_authenticate(self.staff)
        url = reverse("marketplace-component-usage-monthly-list")
        response = self.client.get(url, {"billing_period": "2023-10"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["total_consumed"], "30.00")
        self.assertEqual(response.data[0]["total_allocated"], "100.00")
        self.assertEqual(response.data[0]["usage_percent"], "30.00")

    def test_api_zero_allocation_handles_usage_percent_gracefully(self):
        billing_period = datetime.date(2023, 10, 1)
        factories.ComponentUsageMonthlyFactory(
            component=self.component,
            billing_period=billing_period,
            total_consumed=30,
            total_allocated=0,  # 0 Allocation
        )

        self.client.force_authenticate(self.staff)
        url = reverse("marketplace-component-usage-monthly-list")
        response = self.client.get(url, {"billing_period": "2023-10"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # usage_percent should be null instead of raising ZeroDivisionError
        self.assertIsNone(response.data[0]["usage_percent"])

    def test_api_filtering_offering(self):
        billing_period = datetime.date(2023, 10, 1)
        factories.ComponentUsageMonthlyFactory(
            component=self.component,
            billing_period=billing_period,
        )

        offering2 = factories.OfferingFactory(customer=self.fixture.customer)
        comp2 = factories.OfferingComponentFactory(offering=offering2)
        factories.ComponentUsageMonthlyFactory(
            component=comp2,
            billing_period=billing_period,
        )

        self.client.force_authenticate(self.staff)
        url = reverse("marketplace-component-usage-monthly-list")
        response = self.client.get(
            url, {"offering_uuid": offering2.uuid.hex, "billing_period": "2023-10"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["offering_uuid"], offering2.uuid.hex)

    def test_api_permissions_restrict_unauthorized_users(self):
        billing_period = datetime.date(2023, 10, 1)
        factories.ComponentUsageMonthlyFactory(
            component=self.component,
            billing_period=billing_period,
        )

        # Authenticate as a completely unrelated user
        self.client.force_authenticate(self.user)
        url = reverse("marketplace-component-usage-monthly-list")
        response = self.client.get(url, {"billing_period": "2023-10"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return an empty list due to GenericRoleFilter
        self.assertEqual(len(response.data), 0)

    def test_api_permissions_allow_provider_owner(self):
        billing_period = datetime.date(2023, 10, 1)
        factories.ComponentUsageMonthlyFactory(
            component=self.component,
            billing_period=billing_period,
        )

        # Authenticate as the owner of the Customer offering the service
        self.client.force_authenticate(self.owner)
        url = reverse("marketplace-component-usage-monthly-list")
        response = self.client.get(url, {"billing_period": "2023-10"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_api_filtering_offering_type(self):
        billing_period = datetime.date(2023, 10, 1)
        factories.ComponentUsageMonthlyFactory(
            component=self.component,
            billing_period=billing_period,
        )

        offering2 = factories.OfferingFactory(
            customer=self.fixture.customer, type="OpenStack.Admin"
        )
        comp2 = factories.OfferingComponentFactory(offering=offering2)
        factories.ComponentUsageMonthlyFactory(
            component=comp2,
            billing_period=billing_period,
        )

        self.client.force_authenticate(self.staff)
        url = reverse("marketplace-component-usage-monthly-list")
        response = self.client.get(
            url, {"offering_type": "OpenStack.Admin", "billing_period": "2023-10"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["offering_uuid"], offering2.uuid.hex)
