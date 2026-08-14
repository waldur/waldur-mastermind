"""Credit must be deducted from a policy's cost exactly once.

`_scoped_cost` sums every invoice item, and compensations are ordinary items
with a negative unit_price, so once the monthly compensation has been written
the cost total is already net of it. `MonthlyCompensation` re-simulates the
month from the gross items regardless — it has no notion of "already applied" —
so deducting its result wholesale subtracted the same credit twice.

The overcount only changes an outcome when credit survives the current month's
draw *and* cost outside that draw still exceeds the limit. With a single month
and a credit smaller than the bill the two cancel out: either the credit covers
everything (net zero, nothing left to over-deduct) or it is exhausted (the
projection is zero, nothing is deducted). A multi-month period is the realistic
case, and the one pinned here.
"""

import datetime

from dateutil.relativedelta import relativedelta
from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import compensations
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.tests import factories as policy_factories

EARLIER_COST = 300
THIS_MONTH_COST = 50
CREDIT = 200
# Above the 250 the double deduction produces, below the true 300.
LIMIT = 270
# CREDIT must also stay at or below LIMIT after the draw: is_triggered has a
# second, separate rule that spares a policy outright while the remaining
# balance exceeds limit_cost, which would otherwise mask the arithmetic here.


class DoubleCreditDeductionTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.resource = self.fixture.resource

        month_start = core_utils.month_start(datetime.date.today())
        previous = month_start - relativedelta(months=1)

        # Charged before the credit existed, so it is never compensated and
        # stays in the period total at its full value.
        self.old_invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=previous.year,
            month=previous.month,
            tax_percent=0,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=self.old_invoice,
            project=self.project,
            resource=self.resource,
            unit_price=EARLIER_COST,
            quantity=1,
        )

        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=month_start.year,
            month=month_start.month,
            tax_percent=0,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            resource=self.resource,
            unit_price=THIS_MONTH_COST,
            quantity=1,
        )

        # Larger than this month's bill, so credit remains after the draw —
        # which is what keeps the projection non-zero on re-simulation — but
        # below limit_cost, so the credit-balance override does not fire first.
        self.credit = invoices_factories.CustomerCreditFactory(
            customer=self.customer, value=CREDIT
        )
        self.policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=LIMIT,
            actions="notify_project_team",
            period=ProjectEstimatedCostPolicy.Periods.MONTH_3,
            use_credit=True,
        )

    def period_total(self):
        items = self.fixture.resource.invoice_items.all()
        return ProjectEstimatedCostPolicy._scoped_cost(items)

    def test_period_cost_over_the_limit_fires_after_credit_is_applied(self):
        compensations.MonthlyCompensation(self.customer).apply_compensations()

        self.credit.refresh_from_db()
        self.assertGreater(self.credit.value, 0, "credit must survive the draw")
        # This month is fully compensated; the earlier month is untouched.
        self.assertEqual(self.period_total(), EARLIER_COST)
        self.assertGreater(self.period_total(), LIMIT)

        self.assertTrue(
            self.policy.is_triggered(),
            "period cost is over the limit; this month's credit was deducted "
            "from a total that already excluded it",
        )

    def test_fires_before_any_credit_is_applied(self):
        # Nothing written yet, so the projection is the whole deduction:
        # 350 gross − 50 projected = 300, still over the limit.
        self.assertEqual(self.period_total(), EARLIER_COST + THIS_MONTH_COST)
        self.assertTrue(self.policy.is_triggered())

    def test_credit_is_still_deducted_once(self):
        # Raising the limit above the true period cost must spare the policy —
        # this fails if the fix over-corrects into ignoring credit entirely.
        self.policy.limit_cost = EARLIER_COST + THIS_MONTH_COST + 1
        self.policy.save(update_fields=["limit_cost"])
        self.assertFalse(self.policy.is_triggered())

    def test_policy_ignoring_credit_compares_the_written_total(self):
        self.policy.use_credit = False
        self.policy.save(update_fields=["use_credit"])
        compensations.MonthlyCompensation(self.customer).apply_compensations()
        # No projection is subtracted for a policy that opted out.
        self.assertTrue(self.policy.is_triggered())
