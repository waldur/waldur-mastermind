"""Tests for the rebill_historical_usage management command.

Reproduces the production scenario: a usage-based invoice item gets billed
and its invoice finalized, then the underlying ComponentUsage is later
corrected out-of-band (as waldur_site_load_historical_usage does) after the
invoice is already frozen -- leaving the invoice item, and any paired credit
compensation, stale. The command must fix both without disturbing unrelated
resources sharing the same invoice.
"""

import datetime
import decimal
import io
import logging
from unittest import mock

from django.core.management import call_command
from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.billing_usage import BillingUsageProcessor
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    DiscountAggregations,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories


@freeze_time("2024-07-15")
class RebillHistoricalUsageTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component,
            price=10,  # $10 per unit
        )
        self.resource = self._create_resource()
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan
        )

    def _create_resource(self, project=None):
        resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=project or self.fixture.project,
            state=ResourceStates.OK,
        )
        factories.OrderFactory(
            resource=resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=self.plan,
        )
        callbacks.resource_creation_succeeded(resource)
        return resource

    def _bill_usage(self, resource, plan_period, year, month, amount):
        self.client.force_authenticate(self.fixture.staff)
        date = datetime.datetime(year, month, 15, tzinfo=datetime.UTC)
        payload = {
            "plan_period": plan_period.uuid.hex,
            "date": date.isoformat(),
            "usages": [{"type": "cpu", "amount": amount}],
        }
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        assert response.status_code == 201, response.data

    def _freeze_invoice(self, year, month):
        invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer, year=year, month=month
        )
        invoice.set_created()
        return invoice

    def _corrupt_usage(self, resource, year, month, new_amount):
        """Silently correct ComponentUsage.usage out-of-band, mirroring what
        waldur_site_load_historical_usage does: it re-submits usage through
        the same API used for live reporting, which upserts the ComponentUsage
        row regardless of invoice state, but never touches a frozen invoice's
        items. `.update()` here reproduces exactly that end state without
        depending on the API's own frozen-invoice gate."""
        models.ComponentUsage.objects.filter(
            resource=resource,
            component=self.offering_component,
            billing_period=datetime.date(year, month, 1),
        ).update(usage=new_amount)

    def _create_stale_usage(self, year, month, old_amount, new_amount, resource=None):
        resource = resource or self.resource
        plan_period = (
            self.plan_period
            if resource is self.resource
            else models.ResourcePlanPeriod.objects.get(resource=resource)
        )
        self._bill_usage(resource, plan_period, year, month, old_amount)
        invoice = self._freeze_invoice(year, month)
        self._corrupt_usage(resource, year, month, new_amount)
        return invoice

    def test_stale_invoice_item_is_corrected(self):
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)
        item = invoice.items.get(
            resource=self.resource, details__offering_component_type="cpu"
        )
        self.assertEqual(item.quantity, 5)  # still stale before the command runs

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )

        invoice.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(invoice.state, invoice_models.Invoice.States.CREATED)
        self.assertEqual(item.quantity, 8)
        self.assertEqual(item.unit_price, 10)

    def test_rerun_is_a_noop(self):
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)
        item = invoice.items.get(
            resource=self.resource, details__offering_component_type="cpu"
        )

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )
        item.refresh_from_db()
        self.assertEqual(item.quantity, 8)

        # Running again must not change anything further.
        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )
        item.refresh_from_db()
        self.assertEqual(item.quantity, 8)

    def test_dry_run_is_the_default_and_makes_no_changes(self):
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)
        item = invoice.items.get(
            resource=self.resource, details__offering_component_type="cpu"
        )

        out = io.StringIO()
        # No --execute passed -- dry run is the default, not something that
        # has to be opted into.
        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            stdout=out,
        )

        invoice.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(invoice.state, invoice_models.Invoice.States.CREATED)
        self.assertEqual(item.quantity, 5)
        self.assertIn("[DRY RUN]", out.getvalue())
        self.assertIn("50.00 -> 80.00", out.getvalue())

    def test_mutable_invoice_is_left_alone(self):
        # Current month (frozen at 2024-07-15) -- invoice stays mutable.
        self._bill_usage(self.resource, self.plan_period, 2024, 7, amount=4)
        invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer, year=2024, month=7
        )
        self.assertIn(invoice.state, invoice_models.Invoice.States.MUTABLE_STATES)

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )

        invoice.refresh_from_db()
        self.assertIn(invoice.state, invoice_models.Invoice.States.MUTABLE_STATES)

    def test_canceled_invoice_is_left_alone(self):
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)
        item = invoice.items.get(
            resource=self.resource, details__offering_component_type="cpu"
        )
        invoice.state = invoice_models.Invoice.States.CANCELED
        invoice.save(update_fields=["state"])

        out = io.StringIO()
        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=out,
        )

        invoice.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(invoice.state, invoice_models.Invoice.States.CANCELED)
        self.assertEqual(item.quantity, 5)
        self.assertIn("skipping", out.getvalue())

    def test_paid_invoice_is_left_alone(self):
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)
        item = invoice.items.get(
            resource=self.resource, details__offering_component_type="cpu"
        )
        invoice.state = invoice_models.Invoice.States.PAID
        invoice.save(update_fields=["state"])

        out = io.StringIO()
        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=out,
        )

        invoice.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(invoice.state, invoice_models.Invoice.States.PAID)
        self.assertEqual(item.quantity, 5)
        self.assertIn("skipping", out.getvalue())

    def test_credit_delta_is_corrected(self):
        # Old, stale draw was for 5 units ($50); corrected usage is only 2
        # units ($20) -- the credit should be refunded the $30 difference.
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=2)
        credit = invoice_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=decimal.Decimal("1000")
        )
        old_item = invoice.items.get(
            resource=self.resource, details__offering_component_type="cpu"
        )
        # Reproduces a real production compensation item's shape: `details`
        # is a copy of the main item's `get_component_details()` output
        # (including `offering_component_type`), WITHOUT `is_compensation`/
        # `compensation_of_item` -- those tagging keys were added to
        # MonthlyCompensation after this row was created, so older
        # compensation items in production simply don't carry them. The
        # lookup must not depend on either key.
        compensation = invoice_factories.InvoiceItemFactory(
            invoice=invoice,
            resource=self.resource,
            project=self.fixture.project,
            credit=credit,
            unit_price=decimal.Decimal("-50"),
            quantity=1,
            details={"offering_component_type": "cpu"},
        )
        # Simulate that the (stale, too-high) draw already happened.
        credit.value = decimal.Decimal("1000") - decimal.Decimal("50")
        credit.save(update_fields=["value"])

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )

        old_item.refresh_from_db()
        compensation.refresh_from_db()
        credit.refresh_from_db()
        # The cost item -- not the compensation item -- must be the one
        # updated to reflect the corrected usage.
        self.assertEqual(old_item.quantity, decimal.Decimal("2"))
        self.assertEqual(old_item.unit_price, decimal.Decimal("10"))
        self.assertFalse(old_item.details.get("is_compensation"))
        self.assertEqual(compensation.unit_price, decimal.Decimal("-20"))
        self.assertEqual(credit.value, decimal.Decimal("980"))

        transaction = invoice_models.CreditTransaction.objects.filter(
            credit=credit,
            transaction_type=invoice_models.CreditTransaction.Types.ADJUSTMENT,
        ).first()
        self.assertIsNotNone(transaction)
        self.assertEqual(transaction.amount, decimal.Decimal("30"))

    def test_credit_correction_is_partial_when_credit_is_insufficient(self):
        # Corrected usage costs far more than the available credit. The
        # correction must draw only what's left, not refuse the whole
        # compensation and leave the entire cost uncompensated -- otherwise a
        # Cost Policy watching cost_this_window would jump by the FULL
        # period cost the moment credit runs out, instead of by just the
        # uncovered overage.
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=20)
        credit = invoice_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=decimal.Decimal("100")
        )

        out = io.StringIO()
        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=out,
        )

        old_item = invoice.items.get(
            resource=self.resource,
            details__offering_component_type="cpu",
            unit_price__gte=0,
        )
        compensation = invoice.items.get(
            resource=self.resource,
            details__offering_component_type="cpu",
            unit_price__lt=0,
        )
        credit.refresh_from_db()

        self.assertEqual(old_item.quantity, decimal.Decimal("20"))
        self.assertEqual(old_item.price, decimal.Decimal("200"))
        # Only the available 100 gets drawn, not the full 200 the corrected
        # cost would need -- the remaining 100 stays real, uncompensated cost.
        self.assertEqual(compensation.unit_price, decimal.Decimal("-100"))
        self.assertEqual(credit.value, decimal.Decimal("0"))
        self.assertIn("more credit than available", out.getvalue())
        self.assertIn("leaving 100", out.getvalue())

        transaction = invoice_models.CreditTransaction.objects.filter(
            credit=credit,
            transaction_type=invoice_models.CreditTransaction.Types.ADJUSTMENT,
        ).first()
        self.assertIsNotNone(transaction)
        # Ledger amount is the raw change in credit.value -- negative for a
        # draw (matches test_credit_delta_is_corrected's +30 for a refund).
        self.assertEqual(transaction.amount, decimal.Decimal("-100"))

    def test_credit_correction_is_a_noop_when_no_credit_remains(self):
        # Credit is already fully exhausted (e.g. an earlier resource-period
        # in the same run drew it all). No compensation item should be
        # created for a draw that can't happen at all -- the period's cost
        # simply stays uncompensated, silently, with nothing to write.
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=20)
        credit = invoice_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=decimal.Decimal("0")
        )

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )

        old_item = invoice.items.get(
            resource=self.resource,
            details__offering_component_type="cpu",
            unit_price__gte=0,
        )
        self.assertEqual(old_item.quantity, decimal.Decimal("20"))
        self.assertFalse(
            invoice.items.filter(
                resource=self.resource,
                details__offering_component_type="cpu",
                unit_price__lt=0,
            ).exists()
        )
        credit.refresh_from_db()
        self.assertEqual(credit.value, decimal.Decimal("0"))

    def test_new_compensation_item_gets_correct_billing_period(self):
        # No compensation item exists yet (e.g. the credit was configured
        # only after the invoice was already frozen), so the correction must
        # CREATE one. It must be dated to the invoice's own billing period
        # (2023-12), not to today's date (2024-07-15, per the class-level
        # freeze_time) -- InvoiceItem.start/end silently default to the
        # current month when left unset on create.
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=2)
        invoice_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=decimal.Decimal("1000")
        )

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )

        compensation = invoice.items.get(
            resource=self.resource,
            details__offering_component_type="cpu",
            unit_price__lt=0,
        )
        self.assertEqual(compensation.start.year, 2023)
        self.assertEqual(compensation.start.month, 12)

    def test_no_credit_configured_is_a_noop_for_credit_step(self):
        # No CustomerCredit exists for this customer at all -- the correction
        # must still fix the invoice item, but must not create any credit
        # ledger side effects.
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )

        self.assertFalse(invoice.items.filter(details__is_compensation=True).exists())
        self.assertFalse(invoice_models.CreditTransaction.objects.exists())

    def test_aggregated_discount_scope_requires_explicit_flag(self):
        other_resource = self._create_resource()
        other_plan_period = models.ResourcePlanPeriod.objects.create(
            resource=other_resource, plan=self.plan
        )

        # Bill both resources while the invoice is still mutable, then freeze
        # once, then corrupt only self.resource's usage -- so the sibling
        # resource's item is genuinely "already correct" before the command
        # ever runs.
        self._bill_usage(self.resource, self.plan_period, 2023, 12, amount=5)
        self._bill_usage(other_resource, other_plan_period, 2023, 12, amount=6)
        invoice = self._freeze_invoice(2023, 12)
        self._corrupt_usage(self.resource, 2023, 12, new_amount=8)

        # Aggregated (default) discount scope, configured only after both
        # resources' usage was billed -- mirrors a plan changing later.
        self.plan_component.discount_formula = "50"
        self.plan_component.discount_aggregation = DiscountAggregations.PER_CUSTOMER
        self.plan_component.save()

        other_item = invoice.items.get(
            resource=other_resource, details__offering_component_type="cpu"
        )
        # No discount items exist yet (formula was added after the fact).
        self.assertFalse(
            invoice.items.filter(details__discount_of_item=other_item.uuid.hex).exists()
        )

        out = io.StringIO()
        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=out,
        )

        self.assertIn(other_resource.name, out.getvalue())
        # Without the explicit flag, the other resource's items are untouched.
        self.assertFalse(
            invoice.items.filter(details__discount_of_item=other_item.uuid.hex).exists()
        )

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            allow_aggregated_discount_recompute=True,
            execute=True,
            stdout=io.StringIO(),
        )

        # With the flag, the discount pass now runs across the whole invoice,
        # including the sibling resource's bucket.
        self.assertTrue(
            invoice.items.filter(details__discount_of_item=other_item.uuid.hex).exists()
        )

    def test_per_resource_discount_scope_is_safe_automatically(self):
        other_resource = self._create_resource()
        other_plan_period = models.ResourcePlanPeriod.objects.create(
            resource=other_resource, plan=self.plan
        )

        self._bill_usage(self.resource, self.plan_period, 2023, 12, amount=5)
        self._bill_usage(other_resource, other_plan_period, 2023, 12, amount=6)
        invoice = self._freeze_invoice(2023, 12)
        self._corrupt_usage(self.resource, 2023, 12, new_amount=8)

        self.plan_component.discount_formula = "50"
        self.plan_component.discount_aggregation = DiscountAggregations.PER_RESOURCE
        self.plan_component.save()

        other_item = invoice.items.get(
            resource=other_resource, details__offering_component_type="cpu"
        )
        other_price_before = other_item.price

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )

        other_item.refresh_from_db()
        # PER_RESOURCE scope: the sibling resource's own usage/price is
        # unaffected by the correction on the other resource.
        self.assertEqual(other_item.price, other_price_before)

    def test_credit_correction_nets_out_paired_discount(self):
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)
        self.plan_component.discount_formula = "50"
        self.plan_component.discount_aggregation = DiscountAggregations.PER_RESOURCE
        self.plan_component.save()
        credit = invoice_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=decimal.Decimal("1000")
        )

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )

        item = invoice.items.get(
            resource=self.resource,
            details__offering_component_type="cpu",
            unit_price__gte=0,
        )
        discount_item = invoice.items.get(
            details__discount_of_item=item.uuid.hex, details__is_discount=True
        )
        compensation = invoice.items.get(
            resource=self.resource, credit=credit, unit_price__lt=0
        )
        # 8 units * $10 = $80 gross; 50% discount = -$40; net = $40 -- the
        # credit must be drawn against the net price, not the $80 gross.
        self.assertEqual(item.price, decimal.Decimal("80"))
        self.assertEqual(discount_item.price, decimal.Decimal("-40"))
        self.assertEqual(compensation.unit_price, decimal.Decimal("-40"))
        credit.refresh_from_db()
        self.assertEqual(credit.value, decimal.Decimal("1000") - decimal.Decimal("40"))

    def test_sibling_credit_draw_warns(self):
        other_resource = self._create_resource()
        other_plan_period = models.ResourcePlanPeriod.objects.create(
            resource=other_resource, plan=self.plan
        )
        self._bill_usage(self.resource, self.plan_period, 2023, 12, amount=5)
        self._bill_usage(other_resource, other_plan_period, 2023, 12, amount=6)
        invoice = self._freeze_invoice(2023, 12)
        self._corrupt_usage(self.resource, 2023, 12, new_amount=8)

        credit = invoice_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=decimal.Decimal("1000")
        )
        # The sibling resource already has its own compensation item drawing
        # from the same credit -- this correction must not silently ignore
        # that shared scarcity.
        invoice_factories.InvoiceItemFactory(
            invoice=invoice,
            resource=other_resource,
            project=self.fixture.project,
            credit=credit,
            unit_price=decimal.Decimal("-60"),
            quantity=1,
            details={"offering_component_type": "cpu"},
        )

        out = io.StringIO()
        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=out,
        )

        self.assertIn(other_resource.name, out.getvalue())
        self.assertIn("does NOT re-run cheapest-first", out.getvalue())

    def test_minimal_consumption_credit_warns(self):
        self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)
        invoice_factories.CustomerCreditFactory(
            customer=self.fixture.customer,
            value=decimal.Decimal("1000"),
            expected_consumption=decimal.Decimal("100"),
        )

        out = io.StringIO()
        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=out,
        )

        self.assertIn("minimal-consumption", out.getvalue())

    def test_affiliate_fee_not_recomputed_warns(self):
        invoice = self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)
        link = invoice_factories.CustomerAffiliateFactory(
            customer=self.fixture.customer
        )
        invoice_models.AffiliateFeeAccrual.objects.create(
            affiliate_link=link, invoice=invoice, amount=decimal.Decimal("5")
        )

        out = io.StringIO()
        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=out,
        )

        self.assertIn("affiliate fee", out.getvalue())

    def test_missing_plan_period_is_backfilled_with_warning_and_bounded(self):
        other_resource = self._create_resource()
        # Pin `created` before the historical month being corrected --
        # otherwise the fabricated period's start (clamped to
        # resource.created) would land after its own end, an unrelated,
        # pre-existing quirk this test isn't about.
        models.Resource.objects.filter(pk=other_resource.pk).update(
            created=datetime.datetime(2023, 1, 1, tzinfo=datetime.UTC)
        )
        # A later plan period already exists -- the backfilled one must be
        # bounded so it doesn't overlap it.
        later_period = models.ResourcePlanPeriod.objects.create(
            resource=other_resource,
            plan=self.plan,
            start=datetime.datetime(2024, 3, 1, tzinfo=datetime.UTC),
            end=None,
        )
        invoice_factories.InvoiceFactory(
            customer=self.fixture.customer,
            year=2023,
            month=12,
            state=invoice_models.Invoice.States.CREATED,
        )
        usage = models.ComponentUsage.objects.create(
            resource=other_resource,
            component=self.offering_component,
            plan_period=None,
            usage=decimal.Decimal("3"),
            date=datetime.datetime(2023, 12, 15, tzinfo=datetime.UTC),
            billing_period=datetime.date(2023, 12, 1),
        )

        out = io.StringIO()
        call_command(
            "rebill_historical_usage",
            resource=other_resource.uuid.hex,
            execute=True,
            stdout=out,
        )

        self.assertIn("no plan period covered this billing period", out.getvalue())
        usage.refresh_from_db()
        self.assertIsNotNone(usage.plan_period)
        self.assertEqual(usage.plan_period.end, later_period.start)

    def test_unexpected_error_does_not_abort_whole_run(self):
        other_resource = self._create_resource()
        other_plan_period = models.ResourcePlanPeriod.objects.create(
            resource=other_resource, plan=self.plan
        )
        self._bill_usage(self.resource, self.plan_period, 2023, 12, amount=5)
        self._bill_usage(other_resource, other_plan_period, 2023, 12, amount=6)
        invoice = self._freeze_invoice(2023, 12)
        self._corrupt_usage(self.resource, 2023, 12, new_amount=8)
        self._corrupt_usage(other_resource, 2023, 12, new_amount=9)

        real_create_or_update = (
            BillingUsageProcessor._create_or_update_usage_invoice_item.__func__
        )

        def flaky(cls, *, resource, **kwargs):
            if resource == self.resource:
                raise RuntimeError("boom")
            return real_create_or_update(cls, resource=resource, **kwargs)

        out = io.StringIO()
        with mock.patch.object(
            BillingUsageProcessor,
            "_create_or_update_usage_invoice_item",
            classmethod(flaky),
        ):
            call_command("rebill_historical_usage", execute=True, stdout=out)

        self.assertIn("unexpected error", out.getvalue())
        other_item = invoice.items.get(
            resource=other_resource, details__offering_component_type="cpu"
        )
        self.assertEqual(other_item.quantity, 9)
        invoice.refresh_from_db()
        self.assertEqual(invoice.state, invoice_models.Invoice.States.CREATED)

    def test_verbosity_2_enables_debug_logging(self):
        self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)
        logger_name = (
            "waldur_mastermind.marketplace.management.commands.rebill_historical_usage"
        )
        # logger.setLevel() mutates the shared, process-wide logger object --
        # restore it so this test can't leave DEBUG enabled for whatever
        # test happens to run next.
        self.addCleanup(logging.getLogger(logger_name).setLevel, logging.NOTSET)

        with self.assertLogs(logger_name, level="DEBUG") as logs:
            call_command(
                "rebill_historical_usage",
                resource=self.resource.uuid.hex,
                execute=True,
                verbosity=2,
                stdout=io.StringIO(),
            )

        self.assertTrue(
            any("billed item exists:" in message for message in logs.output)
        )

    def test_default_verbosity_does_not_enable_debug_logging(self):
        self._create_stale_usage(2023, 12, old_amount=5, new_amount=8)
        logger_name = (
            "waldur_mastermind.marketplace.management.commands.rebill_historical_usage"
        )
        self.addCleanup(logging.getLogger(logger_name).setLevel, logging.NOTSET)

        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            execute=True,
            stdout=io.StringIO(),
        )

        self.assertFalse(logging.getLogger(logger_name).isEnabledFor(logging.DEBUG))
