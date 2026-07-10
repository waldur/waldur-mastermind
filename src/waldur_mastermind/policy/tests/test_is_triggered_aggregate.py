"""
Tests for the DB-side cost aggregation used by cost-policy evaluation.

``EstimatedCostPolicyMixin._scoped_cost`` replaces a per-item Python sum
(``sum(i.total for i in invoice_items)``) — which loaded every row and fetched
each item's invoice separately (N+1) — with a single grouped aggregate. These
tests pin two things:

* **Exactness** — the aggregate must equal the Python oracle
  ``sum(i.total ...)`` for every shape of data, including the ``ROUND_UP``
  per-item rounding of ``quantize_price``, non-zero tax, negative items, and
  items spread across several invoices/months.
* **Scalability** — the aggregate must issue a bounded number of queries
  regardless of how many invoice items are in scope (no N+1).
"""

import decimal

from django.test import TestCase
from freezegun import freeze_time

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import (
    EstimatedCostPolicyMixin,
    ProjectEstimatedCostPolicy,
)
from waldur_mastermind.policy.tests import factories as policy_factories

D = decimal.Decimal
TOTAL = ProjectEstimatedCostPolicy.Periods.TOTAL


@freeze_time("2026-07-15")
class ScopedCostAggregateTest(TestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project

    # --- helpers -----------------------------------------------------------

    def _invoice(self, month=7, year=2026, tax_percent=0):
        invoice, _ = invoices_models.Invoice.objects.get_or_create(
            customer=self.customer, month=month, year=year
        )
        invoice.tax_percent = tax_percent
        invoice.save()
        # Drop any items auto-created while provisioning so each test controls
        # the cost exactly.
        invoice.items.all().delete()
        return invoice

    def _item(self, invoice, unit_price, quantity):
        return invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.project,
            unit_price=D(unit_price),
            quantity=D(quantity),
        )

    def _project_items(self):
        return invoices_models.InvoiceItem.objects.filter(project=self.project)

    def _oracle(self, qs):
        # Ground truth: the exact per-item Python computation the aggregate
        # replaces (select_related to keep the oracle itself off the N+1 path).
        return sum([i.total for i in qs.select_related("invoice")])

    def _assert_matches(self, qs):
        expected = self._oracle(qs)
        actual = EstimatedCostPolicyMixin._scoped_cost(qs)
        self.assertEqual(
            actual,
            expected,
            f"aggregate {actual} != oracle {expected}",
        )

    # --- exactness scenarios ----------------------------------------------

    def test_empty_scope_is_zero(self):
        self._invoice()
        self.assertEqual(
            EstimatedCostPolicyMixin._scoped_cost(self._project_items()), D(0)
        )

    def test_single_whole_item_no_tax(self):
        inv = self._invoice(tax_percent=0)
        self._item(inv, "1.0", 5)
        self._assert_matches(self._project_items())

    def test_fractional_price_rounds_up_per_item(self):
        # quantize_price rounds UP (away from zero) to 2 dp: 0.001 -> 0.01.
        inv = self._invoice(tax_percent=0)
        self._item(inv, "0.001", 1)
        self._item(inv, "0.011", 1)  # -> 0.02
        self.assertEqual(
            EstimatedCostPolicyMixin._scoped_cost(self._project_items()), D("0.03")
        )
        self._assert_matches(self._project_items())

    def test_with_tax(self):
        inv = self._invoice(tax_percent=20)
        self._item(inv, "10", 3)  # 30 * 1.2 = 36
        self.assertEqual(
            EstimatedCostPolicyMixin._scoped_cost(self._project_items()), D("36.00")
        )
        self._assert_matches(self._project_items())

    def test_negative_item_rounds_away_from_zero(self):
        inv = self._invoice(tax_percent=10)
        self._item(inv, "-5.001", 1)  # -> -5.01, *1.1
        self._item(inv, "20", 1)
        self._assert_matches(self._project_items())

    def test_many_mixed_items_with_tax(self):
        inv = self._invoice(tax_percent=20)
        for n in range(1, 40):
            self._item(inv, f"{n}.{n % 100:02d}3", n)
        self._assert_matches(self._project_items())

    def test_multiple_invoices_and_tax_rates(self):
        # period=TOTAL sums across months; different tax rates must each apply.
        july = self._invoice(month=7, year=2026, tax_percent=20)
        june = self._invoice(month=6, year=2026, tax_percent=0)
        self._item(july, "3.337", 2)
        self._item(june, "9.991", 3)
        self._assert_matches(self._project_items())

    def test_high_precision_unit_price(self):
        inv = self._invoice(tax_percent=7)
        self._item(inv, "0.0000000001", 1000000)  # 0.0001 -> rounds up to 0.01
        self._item(inv, "1234.5678901234", 7)
        self._assert_matches(self._project_items())

    # --- scalability -------------------------------------------------------

    def test_query_count_constant_regardless_of_item_count(self):
        inv = self._invoice(tax_percent=20)
        for n in range(60):
            self._item(inv, "1.234567", n + 1)
        qs = self._project_items()
        # A single grouped aggregate query — no per-item invoice fetch.
        with self.assertNumQueries(1):
            EstimatedCostPolicyMixin._scoped_cost(qs)

    # --- integration through is_triggered ---------------------------------

    def test_is_triggered_uses_aggregate(self):
        inv = self._invoice(tax_percent=0)
        self._item(inv, "10", 1)
        over = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            use_credit=False,
            limit_cost=5,
            period=TOTAL,
            actions="request_pausing",
        )
        under = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            use_credit=False,
            limit_cost=50,
            period=TOTAL,
            actions="request_pausing",
        )
        self.assertTrue(over.is_triggered())
        self.assertFalse(under.is_triggered())
