from django.test import TestCase
from freezegun import freeze_time

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.common.utils import parse_date, parse_datetime
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    MissingUsagePolicies,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories

PREVIOUS_MONTH = "2024-05-15"
CURRENT_MONTH = "2024-06-01"


class CarriedOverUsageTest(TestCase):
    """Usage materialized for a new billing period by the invoice-creation hook.

    ``invoices.handlers.create_carried_over_usage_if_invoice_has_been_created``
    repeats the last reported value for REUSE rows and records an explicit zero
    for ZERO rows, leaving NONE rows unreported.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(
            unit=UnitPriceMixin.Units.PER_DAY, offering=self.offering
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.component, price=10
        )

        with freeze_time(PREVIOUS_MONTH):
            self.resource = models.Resource.objects.create(
                offering=self.offering,
                plan=self.plan,
                project=self.fixture.project,
            )
            factories.OrderFactory(
                resource=self.resource,
                type=OrderTypes.CREATE,
                state=OrderStates.EXECUTING,
                plan=self.plan,
            )
            callbacks.resource_creation_succeeded(self.resource)
            self.plan_period = models.ResourcePlanPeriod.objects.get(
                resource=self.resource
            )

    def create_usage(self, policy, usage=100):
        with freeze_time(PREVIOUS_MONTH):
            date = parse_date(PREVIOUS_MONTH)
            return models.ComponentUsage.objects.create(
                resource=self.resource,
                component=self.component,
                plan_period=self.plan_period,
                billing_period=core_utils.month_start(date),
                usage=usage,
                date=parse_datetime(PREVIOUS_MONTH),
                missing_usage_policy=policy,
            )

    def create_invoice(self):
        """Create the next month's invoice, which is what fires the handler.

        Billing may have already opened that invoice while usage was written,
        so drop it first: the handler only runs for a freshly created invoice.
        """
        with freeze_time(CURRENT_MONTH):
            date = parse_date(CURRENT_MONTH)
            invoice_models.Invoice.objects.filter(
                customer=self.fixture.customer, year=date.year, month=date.month
            ).delete()
            return invoice_factories.InvoiceFactory(
                customer=self.fixture.customer, year=date.year, month=date.month
            )

    def new_plan_period(self):
        with freeze_time(CURRENT_MONTH):
            return models.ResourcePlanPeriod.objects.create(
                resource=self.resource, plan=self.plan
            )

    def current_usages(self):
        return models.ComponentUsage.objects.filter(
            resource=self.resource,
            component=self.component,
            billing_period=core_utils.month_start(parse_date(CURRENT_MONTH)),
        )

    def get_current_usage(self):
        return models.ComponentUsage.objects.filter(
            resource=self.resource,
            component=self.component,
            billing_period=core_utils.month_start(parse_date(CURRENT_MONTH)),
        ).first()

    def test_reuse_policy_repeats_the_reported_value(self):
        self.create_usage(MissingUsagePolicies.REUSE, usage=100)
        self.create_invoice()

        usage = self.get_current_usage()
        self.assertIsNotNone(usage)
        self.assertEqual(usage.usage, 100)

    def test_zero_policy_records_an_explicit_zero(self):
        self.create_usage(MissingUsagePolicies.ZERO, usage=100)
        self.create_invoice()

        usage = self.get_current_usage()
        self.assertIsNotNone(usage)
        self.assertEqual(usage.usage, 0)

    def test_none_policy_leaves_the_period_unreported(self):
        self.create_usage(MissingUsagePolicies.NONE, usage=100)
        self.create_invoice()

        self.assertIsNone(self.get_current_usage())

    def test_reuse_policy_is_carried_onto_the_new_row(self):
        self.create_usage(MissingUsagePolicies.REUSE)
        self.create_invoice()

        self.assertEqual(
            self.get_current_usage().missing_usage_policy, MissingUsagePolicies.REUSE
        )

    def test_zero_policy_is_carried_onto_the_new_row(self):
        self.create_usage(MissingUsagePolicies.ZERO)
        self.create_invoice()

        self.assertEqual(
            self.get_current_usage().missing_usage_policy, MissingUsagePolicies.ZERO
        )

    def report_current_month(self, usage, policy, plan_period=None):
        """A usage report landing before the new month's invoice is created."""
        with freeze_time(CURRENT_MONTH):
            return models.ComponentUsage.objects.create(
                resource=self.resource,
                component=self.component,
                plan_period=plan_period or self.plan_period,
                billing_period=core_utils.month_start(parse_date(CURRENT_MONTH)),
                usage=usage,
                date=parse_datetime(CURRENT_MONTH),
                missing_usage_policy=policy,
            )

    def test_zero_policy_does_not_overwrite_an_already_reported_value(self):
        self.create_usage(MissingUsagePolicies.ZERO, usage=100)
        self.report_current_month(usage=42, policy=MissingUsagePolicies.ZERO)
        self.create_invoice()

        self.assertEqual(self.get_current_usage().usage, 42)

    def test_zero_policy_does_not_duplicate_a_row_reported_under_another_plan_period(
        self,
    ):
        """The new period's report may sit on a newer plan period.

        A usage row is identified by (resource, component, billing_period) —
        keying the gap check on plan_period too would miss that row and create
        a second one for the same period, the duplicate shape migration 0212
        had to clean up.
        """
        self.create_usage(MissingUsagePolicies.ZERO, usage=100)
        self.report_current_month(
            usage=42,
            policy=MissingUsagePolicies.ZERO,
            plan_period=self.new_plan_period(),
        )
        self.create_invoice()

        self.assertEqual(self.current_usages().count(), 1)
        self.assertEqual(self.current_usages().get().usage, 42)

    def test_reuse_policy_does_not_overwrite_an_already_reported_value(self):
        """A late-created invoice must not undo the month's real report."""
        self.create_usage(MissingUsagePolicies.REUSE, usage=100)
        self.report_current_month(usage=42, policy=MissingUsagePolicies.NONE)
        self.create_invoice()

        current = self.get_current_usage()
        self.assertEqual(current.usage, 42)
        self.assertEqual(current.missing_usage_policy, MissingUsagePolicies.NONE)

    def test_reuse_policy_does_not_duplicate_a_row_under_another_plan_period(self):
        self.create_usage(MissingUsagePolicies.REUSE, usage=100)
        self.report_current_month(
            usage=42,
            policy=MissingUsagePolicies.REUSE,
            plan_period=self.new_plan_period(),
        )
        self.create_invoice()

        self.assertEqual(self.current_usages().count(), 1)
        self.assertEqual(self.current_usages().get().usage, 42)

    def test_terminated_resources_are_skipped(self):
        self.create_usage(MissingUsagePolicies.ZERO)
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save(update_fields=["state"])
        self.create_invoice()

        self.assertIsNone(self.get_current_usage())

    def test_materialized_zero_produces_a_zero_invoice_item(self):
        self.create_usage(MissingUsagePolicies.ZERO, usage=100)
        invoice = self.create_invoice()

        item = invoice.items.get(
            resource=self.resource,
            details__offering_component_type=self.component.type,
        )
        self.assertEqual(item.quantity, 0)
        self.assertEqual(item.total, 0)
        self.assertEqual(
            invoice_models.InvoiceItem.objects.filter(invoice=invoice).count(), 1
        )
