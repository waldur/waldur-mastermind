"""The billing simulation estimates what the invoice will bill.

Each equivalence test (``check``) makes an estimate on a day for a later
month, applies the same change for real the same day, lets the monthly task
invoice and close the months that follow, and checks that their invoices equal
the estimate: before and after credit, and their VAT. A cost billed once a
quarter or once a year is compared over its period, and the reference month's
own invoice with ``invoice_total``. One-off fees, and everything the orders
bill into the current month, are checked against the items the real change
adds to the current month's invoice.
"""

import datetime
import io
import json
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.db import IntegrityError, connection
from django.db.models.signals import post_save
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.logging import event_logger
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.billing.models import PriceEstimate
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import tasks as invoice_tasks
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace import billing_simulation
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing_simulation import Change
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    BillingTypes,
    DiscountAggregations,
    LimitPeriods,
    ResourceStates,
)
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.promotions import models as promotions_models
from waldur_mastermind.promotions.tests import factories as promotions_factories

Units = invoice_models.Units
JANUARY = datetime.date(2027, 1, 1)


class SimulationTestBase(test.APITestCase):
    def setUp(self):
        with freeze_time("2026-11-20"):
            self.customer = structure_factories.CustomerFactory()
            self.project = structure_factories.ProjectFactory(customer=self.customer)

    def make_plan(self, components, unit=Units.PER_MONTH, offering=None, **kwargs):
        """``components``: (type, billing_type, price, amount) tuples, or dicts
        with extra plan component fields."""
        offering = offering or factories.OfferingFactory(
            customer=self.customer, billable=True, **kwargs
        )
        plan = factories.PlanFactory(offering=offering, unit=unit)
        for spec in components:
            if not isinstance(spec, dict):
                type_, billing_type, price, amount = spec
                spec = {
                    "type": type_,
                    "billing_type": billing_type,
                    "price": price,
                    "amount": amount,
                }
            spec = dict(spec)
            offering_component = offering.components.filter(type=spec["type"]).first()
            if not offering_component:
                offering_component = factories.OfferingComponentFactory(
                    offering=offering,
                    type=spec["type"],
                    billing_type=spec["billing_type"],
                    limit_period=spec.get("limit_period"),
                    is_prepaid=spec.get("is_prepaid", False),
                )
            factories.PlanComponentFactory(
                plan=plan,
                component=offering_component,
                price=Decimal(spec["price"]),
                amount=spec.get("amount", 1),
                future_price=spec.get("future_price"),
                discount_formula=spec.get("discount_formula", ""),
                discount_aggregation=spec.get(
                    "discount_aggregation", DiscountAggregations.PER_CUSTOMER
                ),
            )
        return plan

    def activate(self, plan, limits=None, name="vm", project=None):
        resource = factories.ResourceFactory(
            offering=plan.offering,
            plan=plan,
            project=project or self.project,
            limits=limits or {},
            name=name,
            state=ResourceStates.CREATING,
        )
        resource.state = ResourceStates.OK
        resource.save()
        return resource

    def run_month(self, day):
        """Start the month as production does: open its invoices, then close
        the previous month's once the grace period is over."""
        with freeze_time(day):
            invoice_tasks.create_monthly_invoices()
        with freeze_time(f"{day} 23:00"):
            invoice_tasks.finalize_previous_invoices()

    def invoice(self, year, month, customer=None):
        return invoice_models.Invoice.objects.get(
            customer=customer or self.customer, year=year, month=month
        )

    @staticmethod
    def add_months(day, months):
        index = day.year * 12 + day.month - 1 + months
        return datetime.date(index // 12, index % 12 + 1, 1)

    def check(
        self,
        changes,
        apply,
        today="2026-12-10",
        month=JANUARY,
        period=None,
        events=(),
    ):
        """Estimate ``changes`` for ``month`` on ``today``, run ``apply`` the
        same day, let the monthly task bill and close every month up to the
        end of ``period`` and compare those real invoices with the estimate.

        ``period`` is (first month, number of months), the reference month
        alone by default. The invoices' gross (before credit) is compared with
        ``monthly_total`` per month, or ``yearly_total`` over twelve months;
        over one month their price net of credit and their VAT are compared
        too. ``events`` are (moment, callable) pairs run in time order between
        the monthly runs. One-off fees are compared with the items ``apply``
        adds to today's invoice."""
        today_date = datetime.date.fromisoformat(today[:10])
        first, count = period or (month, 1)
        self.run_month(today_date.replace(day=1).isoformat())
        with freeze_time(today):
            estimate = billing_simulation.estimate(self.customer, changes(), month)
            current = invoice_models.Invoice.objects.filter(
                customer=self.customer, year=today_date.year, month=today_date.month
            )
            before = set(
                invoice_models.InvoiceItem.objects.filter(
                    invoice__in=current
                ).values_list("pk", flat=True)
            )
            apply()
            new_items = invoice_models.InvoiceItem.objects.filter(
                invoice__customer=self.customer,
                invoice__year=today_date.year,
                invoice__month=today_date.month,
            ).exclude(pk__in=before)
            one_off = sum(
                (
                    item.price
                    for item in new_items
                    if item.unit == Units.QUANTITY
                    and not item.details.get("is_discount")
                ),
                Decimal(0),
            )
            billed_now = sum((item.price for item in new_items), Decimal(0))

        last = self.add_months(first, count)
        timeline = [(str(moment), fn) for moment, fn in events]
        day = self.add_months(today_date.replace(day=1), 1)
        while day <= last:
            timeline.append((day.isoformat(), None))
            day = self.add_months(day, 1)
        for moment, fn in sorted(timeline, key=lambda event: event[0]):
            if fn is None:
                self.run_month(moment)
            else:
                with freeze_time(moment):
                    fn()

        invoices = [
            self.invoice(day.year, day.month)
            for day in (self.add_months(first, i) for i in range(count))
        ]
        for invoice in invoices:
            self.assertEqual(invoice.state, invoice_models.Invoice.States.CREATED)
        gross = sum(
            (
                item.price
                for invoice in invoices
                for item in invoice.items.filter(credit__isnull=True)
            ),
            Decimal(0),
        )
        if count == 12:
            self.assertEqual(gross, estimate.changed.yearly_total)
        else:
            self.assertEqual(gross, estimate.changed.monthly_total * count)
        if count == 1:
            (invoice,) = invoices
            self.assertEqual(invoice.price, estimate.changed.monthly_total_net)
            self.assertEqual(invoice.tax, estimate.changed.monthly_tax)
        if first == month:
            reference_gross = sum(
                (item.price for item in invoices[0].items.filter(credit__isnull=True)),
                Decimal(0),
            )
            self.assertEqual(reference_gross, estimate.changed.invoice_total)
        self.assertEqual(estimate.one_off_total, one_off)
        self.assertEqual(estimate.current_month_total, billed_now)
        return estimate

    def terminate(self, resource):
        def run():
            resource.refresh_from_db()
            resource.state = ResourceStates.TERMINATING
            resource.save()
            resource.state = ResourceStates.TERMINATED
            resource.save()

        return run

    def order(self, plan, name="vm", limits=None, end_date=None, project=None):
        """Create a resource as an order would, as ``apply`` of a ``check``."""

        def run():
            resource = factories.ResourceFactory(
                offering=plan.offering,
                plan=plan,
                project=project or self.project,
                name=name,
                limits=limits or {},
                end_date=end_date,
                state=ResourceStates.CREATING,
            )
            factories.OrderFactory(
                resource=resource,
                offering=plan.offering,
                plan=plan,
                project=project or self.project,
                limits=limits or {},
            )
            resource.state = ResourceStates.OK
            resource.save()

        return run


class CurrentStateTest(SimulationTestBase):
    def test_current_state_equals_next_invoice(self):
        day_plan = self.make_plan(
            [("cores", BillingTypes.LIMIT, "0.10", 1)], unit=Units.PER_DAY
        )
        month_plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 2)])
        with freeze_time("2026-11-20"):
            self.activate(day_plan, {"cores": 14})
            self.activate(month_plan)

        estimate = self.check(lambda: [], lambda: None)

        # 14 cores x 0.10 x 31 days + 2 x 30
        self.assertEqual(estimate.current.monthly_total, Decimal("103.40"))
        self.assertEqual(estimate.monthly_difference, 0)
        # The yearly figure uses 365 days, not twelve Januaries.
        self.assertEqual(estimate.current.yearly_total, Decimal("1231.00"))


class PlanUnitTest(SimulationTestBase):
    def _resize(self, unit, fixed_price, expected_january):
        plan = self.make_plan(
            [
                ("fee", BillingTypes.FIXED, fixed_price, 1),
                ("cores", BillingTypes.LIMIT, "2", 1),
            ],
            unit=unit,
        )
        with freeze_time("2026-11-20"):
            resource = self.activate(plan, {"cores": 4})

        def apply():
            resource.refresh_from_db()
            resource.limits = {"cores": 8}
            resource.save()

        estimate = self.check(
            lambda: [Change("resize", resource=resource, limits={"cores": 8})], apply
        )
        self.assertEqual(estimate.changed.monthly_total, Decimal(expected_january))

    def test_per_day(self):
        # fee 31 x 1, cores 8 x 2 x 31
        self._resize(Units.PER_DAY, "1", "527.00")

    def test_per_month(self):
        # A limit on a monthly plan is billed as the bare value.
        self._resize(Units.PER_MONTH, "30", "46.00")

    def test_per_half_month(self):
        self._resize(Units.PER_HALF_MONTH, "15", "46.00")

    def test_per_hour(self):
        # The fixed fee is billed for 744 hours; the limit as the bare value.
        self._resize(Units.PER_HOUR, "0.05", "53.20")


class ChangeTest(SimulationTestBase):
    def setUp(self):
        super().setUp()
        self.single = self.make_plan(
            [
                ("db", BillingTypes.FIXED, "1.50", 1),
                ("setup", BillingTypes.ONE_TIME, "150", 1),
            ],
            unit=Units.PER_DAY,
        )
        self.ha = self.make_plan(
            [
                ("db", BillingTypes.FIXED, "3.20", 1),
                ("switch", BillingTypes.ON_PLAN_SWITCH, "50", 1),
            ],
            unit=Units.PER_DAY,
            offering=self.single.offering,
        )
        with freeze_time("2026-11-20"):
            self.db = self.activate(self.single, name="db")

    def test_add_bills_one_time_fee_once(self):
        def apply():
            resource = factories.ResourceFactory(
                offering=self.single.offering,
                plan=self.single,
                project=self.project,
                name="db-2",
                state=ResourceStates.CREATING,
            )
            resource.state = ResourceStates.OK
            resource.save()

        estimate = self.check(
            lambda: [
                Change("add", plan=self.single, project=self.project, name="db-2")
            ],
            apply,
        )
        self.assertEqual(estimate.monthly_difference, Decimal("46.50"))
        self.assertEqual(estimate.one_off_total, Decimal("150.00"))

    def test_remove(self):
        def apply():
            self.db.refresh_from_db()
            self.db.state = ResourceStates.TERMINATING
            self.db.save()
            self.db.state = ResourceStates.TERMINATED
            self.db.save()

        estimate = self.check(lambda: [Change("remove", resource=self.db)], apply)
        self.assertEqual(estimate.monthly_difference, Decimal("-46.50"))
        self.assertEqual(estimate.changed.monthly_total, 0)

    def test_switch_bills_switch_fee(self):
        def apply():
            self.db.refresh_from_db()
            self.db.plan = self.ha
            self.db.save()

        estimate = self.check(
            lambda: [Change("switch", resource=self.db, plan=self.ha)], apply
        )
        # (3.20 - 1.50) x 31 days
        self.assertEqual(estimate.monthly_difference, Decimal("52.70"))
        self.assertEqual(estimate.one_off_total, Decimal("50.00"))
        self.assertEqual(estimate.yearly_difference, Decimal("620.50"))


class DiscountTest(SimulationTestBase):
    def test_cut_below_tier_loses_discount(self):
        # Appendix D.2: 10 % off the whole amount above 12 cores.
        plan = self.make_plan(
            [
                {
                    "type": "cores",
                    "billing_type": BillingTypes.LIMIT,
                    "price": "12",
                    "discount_formula": "10 if usage > 12 else 0",
                }
            ]
        )
        with freeze_time("2026-11-20"):
            cluster = self.activate(plan, {"cores": 16})

        def apply():
            cluster.refresh_from_db()
            cluster.limits = {"cores": 12}
            cluster.save()

        estimate = self.check(
            lambda: [Change("resize", resource=cluster, limits={"cores": 12})], apply
        )
        self.assertEqual(estimate.current.monthly_total, Decimal("172.80"))
        self.assertEqual(estimate.monthly_difference, Decimal("-28.80"))

    def _two_resources(self, aggregation):
        plan = self.make_plan(
            [
                {
                    "type": "cores",
                    "billing_type": BillingTypes.LIMIT,
                    "price": "10",
                    "discount_formula": "20 if usage >= 16 else 0",
                    "discount_aggregation": aggregation,
                }
            ]
        )
        with freeze_time("2026-11-20"):
            first = self.activate(plan, {"cores": 8}, name="a")
            self.activate(plan, {"cores": 8}, name="b")
        return first

    def _cut(self, first):
        def apply():
            first.refresh_from_db()
            first.limits = {"cores": 6}
            first.save()

        return self.check(
            lambda: [Change("resize", resource=first, limits={"cores": 6})], apply
        )

    def test_per_customer_change_depends_on_other_resources(self):
        first = self._two_resources(DiscountAggregations.PER_CUSTOMER)
        estimate = self._cut(first)
        # 16 cores x 10 less 20 % = 128; 14 cores x 10 = 140: cutting costs more.
        self.assertEqual(estimate.current.monthly_total, Decimal("128.00"))
        self.assertEqual(estimate.monthly_difference, Decimal("12.00"))

    def test_per_resource_discounts_each_resource_alone(self):
        first = self._two_resources(DiscountAggregations.PER_RESOURCE)
        estimate = self._cut(first)
        self.assertEqual(estimate.current.monthly_total, Decimal("160.00"))
        self.assertEqual(estimate.monthly_difference, Decimal("-20.00"))


class OpenStackUnitsTest(SimulationTestBase):
    def test_ram_in_mb_is_billed_in_whole_gb(self):
        offering = factories.OfferingFactory(
            customer=self.customer, billable=True, type=OPENSTACK_TENANT_OFFERING
        )
        plan = self.make_plan(
            [
                ("cores", BillingTypes.LIMIT, "0.10", 1),
                ("ram", BillingTypes.LIMIT, "0.10", 1),
            ],
            unit=Units.PER_DAY,
            offering=offering,
        )
        with freeze_time("2026-11-20"):
            tenant = self.activate(plan, {"cores": 14, "ram": 44 * 1024})

        def apply():
            tenant.refresh_from_db()
            tenant.limits = {"cores": 14, "ram": 48 * 1024 + 500}
            tenant.save()

        estimate = self.check(
            lambda: [
                Change("resize", resource=tenant, limits={"ram": 48 * 1024 + 500})
            ],
            apply,
        )
        # 4 more GB; the extra 500 MB are not billed. 4 x 0.10 x 31 days.
        self.assertEqual(estimate.monthly_difference, Decimal("12.40"))


class SideEffectsTest(SimulationTestBase):
    def setUp(self):
        super().setUp()
        self.plan = self.make_plan(
            [
                ("cores", BillingTypes.LIMIT, "2", 1),
                ("usage", BillingTypes.USAGE, "0.5", 1),
            ]
        )
        with freeze_time("2026-11-20"):
            self.resource = self.activate(self.plan, {"cores": 4})

    def counts(self):
        return (
            invoice_models.Invoice.objects.count(),
            invoice_models.InvoiceItem.objects.count(),
            Event.objects.count(),
            PriceEstimate.objects.count(),
            Resource.objects.count(),
            marketplace_models.Order.objects.count(),
            promotions_models.DiscountedResource.objects.count(),
            marketplace_models.ComponentUsage.objects.count(),
            invoice_models.CreditTransaction.objects.count(),
            list(invoice_models.CustomerCredit.objects.values_list("value", flat=True)),
            list(
                marketplace_models.PlanComponent.objects.order_by("pk").values_list(
                    "price", flat=True
                )
            ),
        )

    def test_nothing_is_left_behind(self):
        # A scheduled price, a campaign and a credit make the simulation
        # write prices, discounted resources and compensations.
        priced = self.make_plan(
            [
                {
                    "type": "fee",
                    "billing_type": BillingTypes.FIXED,
                    "price": "30",
                    "future_price": "50",
                }
            ]
        )
        campaign = promotions_factories.CampaignFactory(
            start_date=datetime.date(2026, 11, 1),
            end_date=datetime.date(2027, 6, 30),
            months=3,
            state=promotions_models.Campaign.States.ACTIVE,
        )
        campaign.offerings.add(self.plan.offering)
        invoice_factories.CustomerCreditFactory(customer=self.customer, value=500)
        with freeze_time("2026-11-20"):
            self.activate(priced, name="priced")
        counts = self.counts()
        with (
            freeze_time("2026-11-25"),
            self.captureOnCommitCallbacks() as callbacks,
            CaptureQueriesContext(connection) as queries,
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async"
            ) as evaluate,
        ):
            estimate = billing_simulation.estimate(
                self.customer,
                [
                    Change("resize", resource=self.resource, limits={"cores": 8}),
                    Change("add", plan=self.plan, project=self.project, name="new"),
                ],
            )
        self.assertEqual(callbacks, [])
        evaluate.delay.assert_not_called()
        evaluate.apply_async.assert_not_called()
        self.assertEqual(counts, self.counts())
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits, {"cores": 4})
        # The scheduled price is applied without writing the plan component,
        # which other customers' billing shares.
        self.assertIn(
            Decimal("50"), [line.unit_price for line in estimate.current.lines]
        )
        self.assertFalse(
            [
                query["sql"]
                for query in queries.captured_queries
                if query["sql"].startswith('UPDATE "marketplace_plancomponent"')
            ]
        )
        # The simulation did bill the credit and the campaign.
        self.assertGreater(estimate.changed.credit_compensation, 0)

    def test_usage_components_are_listed_as_not_estimated(self):
        estimate = billing_simulation.estimate(self.customer, [])
        (row,) = estimate.current.not_estimated
        self.assertEqual(row["component_type"], "usage")
        self.assertEqual(row["resource_uuid"], self.resource.uuid.hex)

    def test_an_invoiced_month_is_refused(self):
        with freeze_time("2026-11-20"):
            with self.assertRaises(billing_simulation.SimulationError):
                billing_simulation.estimate(
                    self.customer, [], datetime.date(2026, 11, 1)
                )

    def test_another_customers_resource_is_refused(self):
        other = factories.ResourceFactory(state=ResourceStates.OK)
        with self.assertRaises(billing_simulation.SimulationError):
            billing_simulation.estimate(
                self.customer, [Change("remove", resource=other)]
            )


class EstimateChangeCommandTest(SimulationTestBase):
    def test_prints_the_difference_as_json(self):
        plan = self.make_plan([("cores", BillingTypes.LIMIT, "2", 1)])
        with freeze_time("2026-11-20"):
            resource = self.activate(plan, {"cores": 4})
            out = io.StringIO()
            call_command(
                "estimate_change",
                self.customer.uuid.hex,
                "--resize",
                resource.uuid.hex,
                "cores=8",
                "--json",
                stdout=out,
            )
        result = json.loads(out.getvalue())
        self.assertEqual((result["year"], result["month"]), (2026, 12))
        self.assertEqual(result["monthly"]["difference"], "8.00")
        self.assertEqual(result["monthly_net"]["difference"], "8.00")
        self.assertEqual(result["credit"]["changed"], "0")
        self.assertEqual(result["lines"][0]["component_type"], "cores")

    def test_says_the_figures_are_gross_and_prices_a_prepaid_add(self):
        plan = self.make_plan(
            [
                {
                    "type": "gpu",
                    "billing_type": BillingTypes.ONE_TIME,
                    "price": "10",
                    "is_prepaid": True,
                }
            ]
        )
        with freeze_time("2026-11-20"):
            out = io.StringIO()
            call_command(
                "estimate_change",
                self.customer.uuid.hex,
                "--add",
                plan.uuid.hex,
                self.project.uuid.hex,
                "gpu=2",
                "end_date=2027-11-19",
                stdout=out,
            )
        text = out.getvalue()
        self.assertIn("Before credit, excluding VAT:", text)
        self.assertIn("After credit, excluding VAT", text)
        self.assertIn("One-off: 240.00", text)


class ReviewFindingsTest(SimulationTestBase):
    def test_scheduled_price_applies_to_next_month(self):
        plan = self.make_plan(
            [
                {
                    "type": "fee",
                    "billing_type": BillingTypes.FIXED,
                    "price": "30",
                    "future_price": "50",
                }
            ]
        )
        with freeze_time("2026-11-20"):
            self.activate(plan)
            # Estimated before the monthly task copies the scheduled price.
            estimate = billing_simulation.estimate(self.customer, [], JANUARY)
        self.assertEqual(estimate.current.monthly_total, Decimal("50.00"))
        self.run_month("2026-12-01")
        self.run_month("2027-01-01")
        self.assertEqual(self.invoice(2027, 1).price, Decimal("50.00"))

    def test_quarterly_limit_is_spread_over_its_months(self):
        plan = self.make_plan(
            [
                {
                    "type": "cores",
                    "billing_type": BillingTypes.LIMIT,
                    "price": "10",
                    "limit_period": LimitPeriods.QUARTERLY,
                }
            ]
        )
        with freeze_time("2026-11-20"):
            self.activate(plan, {"cores": 3})
        # February's invoice bills nothing of it: January billed the quarter,
        # 3 x 10. The estimate carries a third of it in each month.
        estimate = self.check(
            lambda: [],
            lambda: None,
            today="2027-01-10",
            month=datetime.date(2027, 2, 1),
            period=(JANUARY, 3),
        )
        self.assertEqual(estimate.current.monthly_total, Decimal("10.00"))
        self.assertEqual(estimate.current.yearly_total, Decimal("120.00"))
        self.assertEqual(self.invoice(2027, 2).price, 0)

    def test_resize_of_total_limit_is_flagged(self):
        plan = self.make_plan(
            [
                {
                    "type": "storage",
                    "billing_type": BillingTypes.LIMIT,
                    "price": "5",
                    "limit_period": LimitPeriods.TOTAL,
                }
            ]
        )
        with freeze_time("2026-11-20"):
            resource = self.activate(plan, {"storage": 10})
            estimate = billing_simulation.estimate(
                self.customer,
                [Change("resize", resource=resource, limits={"storage": 30})],
            )
        (row,) = estimate.changed.not_estimated
        self.assertEqual(row["component_type"], "storage")
        self.assertIn("charged at once", row["reason"])

    def test_switch_to_the_same_plan_is_refused(self):
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        with freeze_time("2026-11-20"):
            resource = self.activate(plan)
            with self.assertRaises(billing_simulation.SimulationError):
                billing_simulation.estimate(
                    self.customer, [Change("switch", resource=resource, plan=plan)]
                )

    def test_add_to_another_customers_project_is_refused(self):
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        other = structure_factories.ProjectFactory()
        with self.assertRaises(billing_simulation.SimulationError):
            billing_simulation.estimate(
                self.customer, [Change("add", plan=plan, project=other)]
            )

    def test_add_on_archived_plan_is_refused(self):
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        plan.archived = True
        plan.save()
        with self.assertRaises(billing_simulation.SimulationError):
            billing_simulation.estimate(
                self.customer, [Change("add", plan=plan, project=self.project)]
            )

    def test_resource_ending_before_the_month_is_not_billed(self):
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        with freeze_time("2026-11-20"):
            resource = self.activate(plan)
            resource.end_date = datetime.date(2026, 12, 20)
            resource.save()
        # It is terminated on its end date, so January bills nothing.
        estimate = self.check(
            lambda: [],
            lambda: None,
            events=[("2026-12-20T12:00:00", self.terminate(resource))],
        )
        self.assertEqual(estimate.current.monthly_total, 0)


class LongPeriodDiscountTest(SimulationTestBase):
    def test_discount_on_quarterly_limit_is_spread_like_its_line(self):
        plan = self.make_plan(
            [
                {
                    "type": "cores",
                    "billing_type": BillingTypes.LIMIT,
                    "price": "10",
                    "limit_period": LimitPeriods.QUARTERLY,
                    "discount_formula": "10 if usage >= 16 else 0",
                }
            ]
        )
        with freeze_time("2026-11-20"):
            self.activate(plan, {"cores": 20})
            estimate = billing_simulation.estimate(self.customer, [], JANUARY)
        # The quarter: 20 x 10 less 10 % = 180, a third of it each month.
        self.assertEqual(estimate.current.monthly_total, Decimal("60.00"))
        self.assertEqual(estimate.current.yearly_total, Decimal("720.00"))
        self.run_month("2026-12-01")
        self.run_month("2027-01-01")
        self.run_month("2027-02-01")
        self.assertEqual(self.invoice(2027, 1).price, Decimal("180.00"))

    def test_discount_on_per_day_plan_is_scaled_by_days(self):
        plan = self.make_plan(
            [
                {
                    "type": "fee",
                    "billing_type": BillingTypes.FIXED,
                    "price": "1",
                    "amount": 20,
                    "discount_formula": "10 if usage >= 10 else 0",
                }
            ],
            unit=Units.PER_DAY,
        )
        with freeze_time("2026-11-20"):
            self.activate(plan)
        estimate = self.check(
            lambda: [],
            lambda: None,
            today="2027-01-10",
            month=datetime.date(2027, 2, 1),
        )
        # 20 x 28 days less 10 % = 504 in February, 18 a day.
        self.assertEqual(estimate.current.monthly_total, Decimal("504.00"))
        self.assertEqual(estimate.current.yearly_total, Decimal(18 * 365))


class CampaignTest(SimulationTestBase):
    def test_added_resource_gets_campaign_discount_like_an_order(self):
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        campaign = promotions_factories.CampaignFactory(
            start_date=datetime.date(2026, 11, 1),
            end_date=datetime.date(2026, 12, 31),
            months=2,
            discount=50,
            state=promotions_models.Campaign.States.ACTIVE,
        )
        campaign.offerings.add(plan.offering)
        self.run_month("2026-11-01")

        with freeze_time("2026-11-10"):
            estimate = billing_simulation.estimate(
                self.customer,
                [Change("add", plan=plan, project=self.project, name="vm")],
            )
            resource = factories.ResourceFactory(
                offering=plan.offering,
                plan=plan,
                project=self.project,
                name="vm",
                state=ResourceStates.CREATING,
            )
            factories.OrderFactory(
                resource=resource,
                offering=plan.offering,
                plan=plan,
                project=self.project,
            )
            resource.state = ResourceStates.OK
            resource.save()
        self.run_month("2026-12-01")
        self.run_month("2027-01-01")

        december = self.invoice(2026, 12)
        self.assertTrue(
            december.items.filter(details__campaign_uuid=campaign.uuid.hex).exists()
        )
        self.assertEqual(december.price, Decimal("15.00"))
        self.assertEqual((estimate.changed.year, estimate.changed.month), (2026, 12))
        self.assertEqual(estimate.changed.monthly_total, december.price)


class BillingModeOverrideTest(SimulationTestBase):
    def test_plan_billing_mode_is_estimated_as_invoiced(self):
        offering = factories.OfferingFactory(customer=self.customer, billable=True)
        cores = factories.OfferingComponentFactory(
            offering=offering,
            type="cores",
            billing_type=BillingTypes.USAGE,
            billed_per_plan=True,
        )
        ram = factories.OfferingComponentFactory(
            offering=offering,
            type="ram",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
            billed_per_plan=True,
        )
        by_limit = factories.PlanFactory(
            offering=offering, unit=Units.PER_MONTH, billing_mode="limit"
        )
        by_usage = factories.PlanFactory(
            offering=offering, unit=Units.PER_MONTH, billing_mode="usage"
        )
        for plan, prices in ((by_limit, ("2", "1")), (by_usage, ("3", "1"))):
            for component, price in zip((cores, ram), prices):
                factories.PlanComponentFactory(
                    plan=plan, component=component, price=Decimal(price)
                )
        with freeze_time("2026-11-20"):
            self.activate(by_limit, {"cores": 4, "ram": 10}, name="limit")
            on_usage = self.activate(by_usage, {"cores": 4, "ram": 10}, name="usage")

        estimate = self.check(lambda: [], lambda: None)

        # Usage-based cores are billed on the limit: 4 x 2 + 10 x 1.
        self.assertEqual(estimate.current.monthly_total, Decimal("18.00"))
        self.assertEqual(
            sorted(
                row["component_type"]
                for row in estimate.current.not_estimated
                if row["resource_uuid"] == on_usage.uuid.hex
            ),
            ["cores", "ram"],
        )


class ReferenceMonthTest(SimulationTestBase):
    def test_default_month_follows_local_midnight(self):
        plan = self.make_plan(
            [
                {
                    "type": "fee",
                    "billing_type": BillingTypes.FIXED,
                    "price": "30",
                    "future_price": "50",
                }
            ]
        )
        with freeze_time("2026-11-20"):
            self.activate(plan)
        with (
            self.settings(TIME_ZONE="Europe/Tallinn"),
            freeze_time("2026-12-31T22:30:00Z"),
        ):
            self.assertEqual(
                billing_simulation.default_month(), datetime.date(2027, 2, 1)
            )
            estimate = billing_simulation.estimate(self.customer, [])
        self.assertEqual((estimate.current.year, estimate.current.month), (2027, 2))
        self.assertEqual(estimate.current.monthly_total, Decimal("50.00"))

    def test_mid_month_reference_is_the_whole_month(self):
        plan = self.make_plan([("fee", BillingTypes.FIXED, "1", 1)], unit=Units.PER_DAY)
        with freeze_time("2026-11-20"):
            resource = self.activate(plan)
            resource.end_date = datetime.date(2027, 1, 10)
            resource.save()
            first = billing_simulation.estimate(self.customer, [], JANUARY)
        # Terminated on its end date: billed from 1 to 10 January.
        mid_month = self.check(
            lambda: [],
            lambda: None,
            month=datetime.date(2027, 1, 15),
            events=[("2027-01-10T09:00:00", self.terminate(resource))],
        )
        self.assertEqual(mid_month.current.monthly_total, Decimal("10.00"))
        self.assertEqual(mid_month.current.monthly_total, first.current.monthly_total)
        self.assertEqual((mid_month.current.year, mid_month.current.month), (2027, 1))
        self.assertIn("ends on", mid_month.current.not_estimated[0]["reason"])


class NoOutsideEffectsTest(ChangeTest):
    def test_add_and_switch_emit_no_events_or_tasks(self):
        # A credit makes the simulation close the current month too.
        invoice_factories.CustomerCreditFactory(customer=self.customer, value=500)

        # The invoice item handlers call emit(); it stands aside while side
        # effects are skipped, so no event is written or announced.
        with (
            self.captureOnCommitCallbacks() as callbacks,
            mock.patch.object(event_logger, "event_emitted") as emitted,
            mock.patch.object(Event.objects, "create") as create_event,
            mock.patch("celery.app.base.Celery.send_task") as send_task,
        ):
            billing_simulation.estimate(
                self.customer,
                [
                    Change("add", plan=self.single, project=self.project),
                    Change("switch", resource=self.db, plan=self.ha),
                ],
            )
        self.assertEqual(callbacks, [])
        emitted.send.assert_not_called()
        create_event.assert_not_called()
        send_task.assert_not_called()


class IntegrityErrorTest(SimulationTestBase):
    def setUp(self):
        super().setUp()
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        with freeze_time("2026-11-20"):
            self.activate(plan)

    def test_invoice_created_meanwhile_is_reported(self):
        # Another process creates the month's invoice after the first check.
        invoice_models.Invoice.objects.create(
            customer=self.customer, year=JANUARY.year, month=JANUARY.month
        )
        with mock.patch.object(billing_simulation, "_is_invoiced", return_value=False):
            with self.assertRaises(billing_simulation.SimulationError) as raised:
                billing_simulation.estimate(self.customer, [], JANUARY)
        self.assertIn("already exists", str(raised.exception))

    def test_integrity_error_of_an_invoice_handler_is_raised(self):
        def fail(sender, instance, created=False, **kwargs):
            if created:
                raise IntegrityError("some other constraint")

        post_save.connect(fail, sender=invoice_models.Invoice, dispatch_uid="fail")
        try:
            with self.assertRaises(IntegrityError) as raised:
                billing_simulation.estimate(self.customer, [], JANUARY)
        finally:
            post_save.disconnect(sender=invoice_models.Invoice, dispatch_uid="fail")
        self.assertNotIsInstance(raised.exception, billing_simulation.SimulationError)

    def test_other_integrity_error_is_raised(self):
        with mock.patch(
            "waldur_mastermind.marketplace.billing_discount."
            "apply_aggregated_volume_discounts",
            side_effect=IntegrityError("some other constraint"),
        ):
            with self.assertRaises(IntegrityError) as raised:
                billing_simulation.estimate(self.customer, [], JANUARY)
        self.assertNotIsInstance(raised.exception, billing_simulation.SimulationError)


class QuarterlyPlanTest(SimulationTestBase):
    """A plan priced per quarter bills its fixed fee in the first month of
    each quarter only."""

    def setUp(self):
        super().setUp()
        self.plan = self.make_plan(
            [("fee", BillingTypes.FIXED, "90", 1)], unit=Units.PER_QUARTER
        )
        with freeze_time("2026-11-20"):
            self.activate(self.plan)

    def test_january_reference(self):
        # January, April, July and October 2027 bill 90 each.
        estimate = self.check(lambda: [], lambda: None, period=(JANUARY, 12))
        self.assertEqual(estimate.current.monthly_total, Decimal("30.00"))
        self.assertEqual(estimate.current.yearly_total, Decimal("360.00"))

    def test_december_reference(self):
        december = datetime.date(2026, 12, 1)
        estimate = self.check(
            lambda: [],
            lambda: None,
            today="2026-11-25",
            month=december,
            period=(december, 12),
        )
        self.assertEqual(estimate.current.monthly_total, Decimal("30.00"))
        self.assertEqual(estimate.current.yearly_total, Decimal("360.00"))


class CampaignEligibilityTest(SimulationTestBase):
    """An added resource is eligible for the campaigns running when it is
    ordered, not those running on the first day of the reference month."""

    DECEMBER = datetime.date(2026, 12, 1)

    def campaign(self, offering, start, end, months=2):
        campaign = promotions_factories.CampaignFactory(
            start_date=start,
            end_date=end,
            months=months,
            discount=50,
            state=promotions_models.Campaign.States.ACTIVE,
        )
        campaign.offerings.add(offering)
        return campaign

    def test_campaign_ending_this_month(self):
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        self.campaign(
            plan.offering, datetime.date(2026, 11, 1), datetime.date(2026, 11, 27)
        )
        estimate = self.check(
            lambda: [Change("add", plan=plan, project=self.project, name="vm")],
            self.order(plan),
            today="2026-11-25",
            month=self.DECEMBER,
        )
        self.assertEqual(estimate.changed.monthly_total, Decimal("15.00"))

    def test_campaign_starting_next_month(self):
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        self.campaign(
            plan.offering, datetime.date(2026, 12, 1), datetime.date(2026, 12, 31)
        )
        estimate = self.check(
            lambda: [Change("add", plan=plan, project=self.project, name="vm")],
            self.order(plan),
            today="2026-11-25",
            month=self.DECEMBER,
        )
        self.assertEqual(estimate.changed.monthly_total, Decimal("30.00"))

    def test_one_off_fee_is_discounted_in_the_current_month(self):
        # The order bills its setup fee into November, while the campaign
        # (one month long) discounts November only.
        plan = self.make_plan(
            [
                ("fee", BillingTypes.FIXED, "30", 1),
                ("setup", BillingTypes.ONE_TIME, "150", 1),
            ]
        )
        self.campaign(
            plan.offering,
            datetime.date(2026, 11, 1),
            datetime.date(2026, 12, 31),
            months=1,
        )
        estimate = self.check(
            lambda: [Change("add", plan=plan, project=self.project, name="vm")],
            self.order(plan),
            today="2026-11-25",
            month=self.DECEMBER,
        )
        self.assertEqual(estimate.one_off_total, Decimal("75.00"))
        self.assertEqual(estimate.changed.monthly_total, Decimal("30.00"))

    def test_discount_period_across_year_end_is_reported(self):
        # Campaign.get_discount_for_resource cannot compute a period that
        # runs into the next year (issue #1280).
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        self.campaign(
            plan.offering, datetime.date(2026, 12, 1), datetime.date(2026, 12, 31)
        )
        with freeze_time("2026-12-10"):
            with self.assertRaises(billing_simulation.SimulationError) as raised:
                billing_simulation.estimate(
                    self.customer,
                    [Change("add", plan=plan, project=self.project)],
                    JANUARY,
                )
        self.assertIn("campaign", str(raised.exception))


class OneOffTest(SimulationTestBase):
    def test_total_limit_of_an_added_resource(self):
        plan = self.make_plan(
            [
                ("fee", BillingTypes.FIXED, "10", 1),
                {
                    "type": "storage",
                    "billing_type": BillingTypes.LIMIT,
                    "price": "5",
                    "limit_period": LimitPeriods.TOTAL,
                },
            ]
        )
        estimate = self.check(
            lambda: [
                Change(
                    "add",
                    plan=plan,
                    project=self.project,
                    name="vm",
                    limits={"storage": 30},
                )
            ],
            self.order(plan, limits={"storage": 30}),
        )
        self.assertEqual(estimate.one_off_total, Decimal("150.00"))
        self.assertEqual(estimate.monthly_difference, Decimal("10.00"))


class AnnualDiscountTest(SimulationTestBase):
    def test_discount_sees_only_items_of_the_same_anniversary(self):
        plan = self.make_plan(
            [
                {
                    "type": "cores",
                    "billing_type": BillingTypes.LIMIT,
                    "price": "10",
                    "limit_period": LimitPeriods.ANNUAL,
                    "discount_formula": "20 if usage >= 16 else 0",
                }
            ]
        )
        with freeze_time("2026-10-20"):
            self.activate(plan, {"cores": 8}, name="a")
        with freeze_time("2026-11-20"):
            self.activate(plan, {"cores": 8}, name="b")
        # Billed in October and in November, 8 cores each time: no discount.
        estimate = self.check(lambda: [], lambda: None, period=(JANUARY, 12))
        self.assertEqual(estimate.current.yearly_total, Decimal("160.00"))


class CreditTest(SimulationTestBase):
    def setUp(self):
        super().setUp()
        self.customer.default_tax_percent = 20
        self.customer.save()
        self.plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        with freeze_time("2026-11-20"):
            self.activate(self.plan)

    def test_net_of_credit_left_after_the_current_month(self):
        # November, closed on 1 December, takes 11.10 of the credit and
        # December 30; January gets the 10 left, net of VAT.
        invoice_factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal("51.10")
        )
        estimate = self.check(lambda: [], lambda: None)
        self.assertEqual(estimate.current.monthly_total, Decimal("30.00"))
        self.assertEqual(estimate.current.credit_compensation, Decimal("8.34"))
        self.assertEqual(estimate.current.monthly_total_net, Decimal("21.66"))
        self.assertEqual(estimate.current.tax_percent, 20)

    def test_added_resource_draws_on_the_current_month(self):
        invoice_factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal("81.10")
        )
        estimate = self.check(
            lambda: [Change("add", plan=self.plan, project=self.project)],
            self.order(self.plan),
        )
        self.assertLess(estimate.changed.monthly_total_net, Decimal("60.00"))
        self.assertGreater(estimate.changed.credit_compensation, 0)

    def test_credit_pays_the_monthly_share_of_a_quarterly_fee(self):
        plan = self.make_plan(
            [("fee", BillingTypes.FIXED, "100", 1)], unit=Units.PER_QUARTER
        )
        with freeze_time("2026-11-20"):
            self.activate(plan, name="quarterly")
        invoice_factories.CustomerCreditFactory(customer=self.customer, value=1000)
        with freeze_time("2026-12-10"):
            estimate = billing_simulation.estimate(self.customer, [], JANUARY)
        # 30 a month and a third of the quarter's 100, rounded up as prices are,
        # all paid by the credit.
        self.assertEqual(estimate.current.monthly_total, Decimal("63.34"))
        self.assertEqual(estimate.current.credit_compensation, Decimal("63.34"))
        self.assertEqual(estimate.current.monthly_total_net, 0)

    def test_no_credit_means_net_equals_gross(self):
        estimate = self.check(lambda: [], lambda: None)
        self.assertEqual(estimate.current.credit_compensation, 0)
        self.assertEqual(
            estimate.current.monthly_total_net, estimate.current.monthly_total
        )


class InactiveCustomerTest(SimulationTestBase):
    def setUp(self):
        super().setUp()
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        with freeze_time("2026-11-20"):
            self.activate(plan)

    def assert_refused_and_not_invoiced(self):
        with freeze_time("2026-12-10"):
            with self.assertRaises(billing_simulation.SimulationError):
                billing_simulation.estimate(self.customer, [], JANUARY)
        self.run_month("2027-01-01")
        self.assertFalse(
            invoice_models.Invoice.objects.filter(
                customer=self.customer, year=2027, month=1
            ).exists()
        )

    def test_archived_customer(self):
        self.customer.archived = True
        self.customer.save()
        self.assert_refused_and_not_invoiced()

    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_accounting_not_started(self):
        self.customer.accounting_start_date = timezone.make_aware(
            datetime.datetime(2027, 3, 1)
        )
        self.customer.save()
        self.assert_refused_and_not_invoiced()


class PrepaidAddTest(SimulationTestBase):
    def setUp(self):
        super().setUp()
        self.plan = self.make_plan(
            [
                {
                    "type": "gpu",
                    "billing_type": BillingTypes.ONE_TIME,
                    "price": "10",
                    "is_prepaid": True,
                }
            ]
        )

    def test_prepaid_for_the_whole_period(self):
        end_date = datetime.date(2027, 12, 9)
        estimate = self.check(
            lambda: [
                Change(
                    "add",
                    plan=self.plan,
                    project=self.project,
                    limits={"gpu": 2},
                    end_date=end_date,
                )
            ],
            self.order(self.plan, limits={"gpu": 2}, end_date=end_date),
        )
        # 2 x 10 for each of the 12 months to the end date.
        self.assertEqual(estimate.one_off_total, Decimal("240.00"))

    def test_without_end_date_it_is_not_estimated(self):
        with freeze_time("2026-12-10"):
            estimate = billing_simulation.estimate(
                self.customer,
                [
                    Change(
                        "add", plan=self.plan, project=self.project, limits={"gpu": 2}
                    )
                ],
                JANUARY,
            )
        self.assertEqual(estimate.one_off_total, 0)
        (row,) = estimate.changed.not_estimated
        self.assertEqual(row["component_type"], "gpu")
        self.assertIn("end date", row["reason"])


class PastMonthTest(SimulationTestBase):
    def test_past_month_is_refused(self):
        with freeze_time("2026-12-10"):
            for call in (billing_simulation.estimate, billing_simulation.simulate):
                with self.assertRaises(billing_simulation.SimulationError) as raised:
                    call(self.customer, [], datetime.date(2026, 10, 1))
                self.assertIsInstance(raised.exception, ValueError)
                self.assertIn("past", str(raised.exception))


FEBRUARY = datetime.date(2027, 2, 1)
MARCH = datetime.date(2027, 3, 1)


def quarterly_cores(price="10"):
    return {
        "type": "cores",
        "billing_type": BillingTypes.LIMIT,
        "price": price,
        "limit_period": LimitPeriods.QUARTERLY,
    }


def annual_cores(price="10"):
    return {
        "type": "cores",
        "billing_type": BillingTypes.LIMIT,
        "price": price,
        "limit_period": LimitPeriods.ANNUAL,
    }


class ReferenceInvoiceTest(SimulationTestBase):
    """``monthly_total`` spreads a quarter or a year over its months;
    ``invoice_total`` is what the reference month's own invoice bills."""

    def test_removing_a_quarterly_resource_saves_nothing_this_quarter(self):
        plan = self.make_plan([quarterly_cores()])
        with freeze_time("2026-11-20"):
            resource = self.activate(plan, {"cores": 3})
        self.run_month("2026-12-01")
        self.run_month("2027-01-01")
        with freeze_time("2027-01-10"):
            estimate = billing_simulation.estimate(
                self.customer, [Change("remove", resource=resource)], FEBRUARY
            )
            self.terminate(resource)()
        self.run_month("2027-02-01")
        self.run_month("2027-03-01")

        # January billed the whole quarter, and keeps it.
        self.assertEqual(self.invoice(2027, 1).price, Decimal("30.00"))
        self.assertEqual(self.invoice(2027, 2).price, Decimal(0))
        self.assertEqual(estimate.invoice_difference, Decimal(0))
        # From the next quarter on, the resource costs nothing.
        self.assertEqual(estimate.monthly_difference, Decimal("-10.00"))

    def test_quarterly_resource_added_mid_quarter_is_billed_next_quarter(self):
        plan = self.make_plan([quarterly_cores()])
        self.run_month("2026-12-01")
        self.run_month("2027-01-01")
        self.run_month("2027-02-01")
        with freeze_time("2027-02-10"):
            changes = [
                Change("add", plan=plan, project=self.project, limits={"cores": 3})
            ]
            march = billing_simulation.estimate(self.customer, changes, MARCH)
            april = billing_simulation.estimate(
                self.customer, changes, datetime.date(2027, 4, 1)
            )
            self.order(plan, limits={"cores": 3})()
        self.run_month("2027-03-01")
        self.run_month("2027-04-01")
        self.run_month("2027-05-01")

        self.assertEqual(self.invoice(2027, 3).price, Decimal(0))
        self.assertEqual(march.changed.invoice_total, Decimal(0))
        self.assertEqual(march.changed.monthly_total, Decimal("10.00"))
        self.assertEqual(self.invoice(2027, 4).price, Decimal("30.00"))
        self.assertEqual(april.changed.invoice_total, Decimal("30.00"))

    def test_added_annual_resource_bills_its_year_now(self):
        plan = self.make_plan([annual_cores()])
        estimate = self.check(
            lambda: [
                Change("add", plan=plan, project=self.project, limits={"cores": 3})
            ],
            self.order(plan, limits={"cores": 3}),
            period=(JANUARY, 12),
        )
        # The year is billed on December's invoice when the order completes,
        # and the next one on December's after; January bills nothing of it.
        self.assertEqual(estimate.current_month_total, Decimal("30.00"))
        self.assertEqual(estimate.one_off_total, Decimal(0))
        self.assertEqual(estimate.changed.invoice_total, Decimal(0))
        self.assertEqual(estimate.changed.monthly_total, Decimal("2.50"))

    def test_resize_of_a_quarterly_limit_is_listed(self):
        plan = self.make_plan([quarterly_cores()])
        with freeze_time("2026-11-20"):
            resource = self.activate(plan, {"cores": 3})
            estimate = billing_simulation.estimate(
                self.customer,
                [Change("resize", resource=resource, limits={"cores": 6})],
                FEBRUARY,
            )
        (row,) = estimate.changed.not_estimated
        self.assertEqual(row["component_type"], "cores")
        self.assertIn("quarter", row["reason"])

    def test_annual_resource_created_on_a_leap_day(self):
        plan = self.make_plan([annual_cores()])
        with freeze_time("2024-02-29"):
            factories.ResourceFactory(
                offering=plan.offering,
                plan=plan,
                project=self.project,
                limits={"cores": 3},
                state=ResourceStates.OK,
            )
        with freeze_time("2026-11-20"):
            estimate = billing_simulation.estimate(self.customer, [], FEBRUARY)
        # Its anniversary falls on 28 February in 2027.
        self.assertEqual(estimate.current.invoice_total, Decimal("30.00"))
        self.assertEqual(estimate.current.monthly_total, Decimal("2.50"))


class CurrentMonthChargeTest(SimulationTestBase):
    def test_added_resource_bills_the_rest_of_the_month(self):
        plan = self.make_plan([("fee", BillingTypes.FIXED, "31", 1)])
        estimate = self.check(
            lambda: [Change("add", plan=plan, project=self.project)],
            self.order(plan),
        )
        # 10 to 31 December: 22 of 31 days, rounded up as prices are.
        self.assertEqual(estimate.current_month_total, Decimal("22.01"))
        self.assertEqual(estimate.one_off_total, Decimal(0))

    def test_no_change_bills_nothing_now(self):
        plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        with freeze_time("2026-11-20"):
            self.activate(plan)
        estimate = self.check(lambda: [], lambda: None)
        self.assertEqual(estimate.current_month_total, Decimal(0))


class CreditProjectionTest(SimulationTestBase):
    def setUp(self):
        super().setUp()
        self.plan = self.make_plan([("fee", BillingTypes.FIXED, "30", 1)])
        with freeze_time("2026-11-20"):
            self.activate(self.plan)

    def test_months_before_the_reference_draw_their_share(self):
        # November takes 11.10, December and January 30 each: February gets
        # the 9.90 left.
        invoice_factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal("81.00")
        )
        estimate = self.check(lambda: [], lambda: None, month=FEBRUARY)
        self.assertEqual(estimate.current.credit_compensation, Decimal("9.90"))
        self.assertEqual(estimate.current.monthly_total_net, Decimal("20.10"))

    def test_credit_drawn_includes_the_minimal_consumption(self):
        credit = invoice_factories.CustomerCreditFactory(
            customer=self.customer,
            value=Decimal("1000"),
            expected_consumption=Decimal("100"),
        )
        self.run_month("2026-12-01")
        with freeze_time("2026-12-10"):
            estimate = billing_simulation.estimate(self.customer, [], JANUARY)
        self.run_month("2027-01-01")
        credit.refresh_from_db()
        before_january = credit.value
        self.run_month("2027-02-01")
        credit.refresh_from_db()
        # January costs 30, all paid by the credit, but the floor takes 100.
        self.assertEqual(estimate.current.credit_compensation, Decimal("30.00"))
        self.assertEqual(estimate.current.credit_drawn, Decimal("100.00"))
        self.assertEqual(before_january - credit.value, Decimal("100.00"))

    def test_credit_spent_before_the_reference_month(self):
        # November and December spend it all.
        invoice_factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal("20.00")
        )
        estimate = self.check(lambda: [], lambda: None)
        self.assertEqual(estimate.current.credit_compensation, 0)
        self.assertEqual(estimate.current.credit_drawn, 0)
        self.assertEqual(estimate.current.monthly_total_net, Decimal("30.00"))

    def test_previous_month_in_its_grace_period_draws_first(self):
        # On December 1, before the grace period ends, November's invoice is
        # still open: it takes its 11.10 and December its 30 before January,
        # which gets the 8.90 left.
        invoice_factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal("50.00")
        )
        with freeze_time("2026-12-01 00:30"):
            invoice_tasks.create_monthly_invoices()
            self.assertEqual(
                self.invoice(2026, 11).state,
                invoice_models.Invoice.States.PENDING_FINALIZATION,
            )
            during = billing_simulation.estimate(self.customer, [], JANUARY)
        with freeze_time("2026-12-01 23:00"):
            invoice_tasks.finalize_previous_invoices()
        with freeze_time("2026-12-10"):
            after = billing_simulation.estimate(self.customer, [], JANUARY)
        self.assertEqual(after.current.credit_compensation, Decimal("8.90"))
        self.assertEqual(during.current.credit_compensation, Decimal("8.90"))
