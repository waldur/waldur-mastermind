"""A cost policy waits for the *live* credit balance, not the stored one.

`is_triggered` spares a policy while the credit balance stays above
`limit_cost`. The stored balance only falls when the month's compensations are
written at month end, so comparing it as is held every credit-funded policy back
until the 1st, however early in the month usage exhausted the credit: a project
with ~2500 of credit spending >4500 in a month with a limit of 100 was not
downscaled until the credit was drawn to 0 at month end.

The items here carry a resource so MonthlyCompensation actually compensates
them; test_credit_aware_policy uses uncompensated items on purpose and so never
exercises the draw.
"""

import datetime
import decimal

from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import compensations
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.exceptions import PolicyException
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import handlers
from waldur_mastermind.policy.models import (
    CustomerEstimatedCostPolicy,
    ProjectEstimatedCostPolicy,
)
from waldur_mastermind.policy.tests import factories as policy_factories

CREDIT = 2500
LIMIT = 100


class LiveCreditBalanceTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        month_start = core_utils.month_start(datetime.date.today())
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=month_start.year,
            month=month_start.month,
            tax_percent=0,
        )

    def _charge(self, amount):
        invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            resource=self.resource,
            unit_price=amount,
            quantity=1,
        )

    def _project_policy(self):
        return policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=LIMIT,
            actions="request_downscaling",
            period=ProjectEstimatedCostPolicy.Periods.MONTH_1,
            use_credit=True,
        )

    def test_project_credit_exhausted_mid_month_triggers(self):
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        project_credit = invoices_factories.ProjectCreditFactory(
            project=self.project, value=CREDIT
        )
        self._charge(4500)
        policy = self._project_policy()

        # Nothing is written until month end: the stored balance is untouched.
        project_credit.refresh_from_db()
        self.assertEqual(project_credit.value, CREDIT)
        self.assertTrue(policy.is_triggered())

    def test_project_credit_still_covering_does_not_trigger(self):
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=CREDIT)
        self._charge(2000)

        self.assertFalse(self._project_policy().is_triggered())

    def test_organization_credit_exhausted_mid_month_triggers(self):
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=CREDIT)
        self._charge(4500)

        self.assertTrue(self._project_policy().is_triggered())

    def test_vat_does_not_leave_a_phantom_balance(self):
        # The last partial compensation is the balance net of tax; a balance
        # derived from the items would keep the credit's VAT "remaining".
        self.invoice.tax_percent = decimal.Decimal("8.1")
        self.invoice.save()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=CREDIT)
        self._charge(4500)

        self.assertTrue(self._project_policy().is_triggered())

    def test_customer_policy_credit_exhausted_mid_month_triggers(self):
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=CREDIT)
        self._charge(4500)
        policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=LIMIT,
            actions="request_downscaling",
            period=CustomerEstimatedCostPolicy.Periods.MONTH_1,
        )

        self.assertTrue(policy.is_triggered())

    def _earlier_uncovered_cost(self, amount):
        # Cost in an earlier month that no credit covered, so a TOTAL policy's
        # Gate 1 stays open and the credit balance alone decides.
        previous = core_utils.month_start(datetime.date.today()) - datetime.timedelta(
            days=1
        )
        old_invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=previous.year,
            month=previous.month,
            tax_percent=0,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=old_invoice,
            project=self.project,
            resource=self.resource,
            unit_price=decimal.Decimal(amount),
            quantity=1,
        )

    def _total_policy(self, limit_cost=LIMIT):
        return policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=limit_cost,
            actions="request_downscaling",
            period=ProjectEstimatedCostPolicy.Periods.TOTAL,
            use_credit=True,
        )

    def test_total_period_with_monthly_allocation(self):
        # The shape that surfaced this: a TOTAL policy far over its limit on
        # earlier, uncompensated months, so the cost test always passes and the
        # credit balance is the only gate. A fresh monthly allocation must hold
        # the policy back until this month's usage has spent it — not until the
        # month-end run writes the draw.
        self._earlier_uncovered_cost("7701.67")
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        policy = self._total_policy()

        self._charge(50)  # start of the month, allocation untouched
        self.assertFalse(policy.is_triggered())

        self._charge(2000)  # 2050 of 2900 drawn
        self.assertFalse(policy.is_triggered())

        self._charge(1000)  # 3050: the allocation is spent
        self.assertTrue(policy.is_triggered())

    def test_project_allocation_with_minimal_consumption_untouched(self):
        # The minimal-consumption floor is drawn at month end whatever the
        # usage; it is not usage, so it must not read as the allocation spent.
        self._earlier_uncovered_cost("7701.67")
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(
            project=self.project, value=2900, expected_consumption=2900
        )
        policy = self._total_policy()

        self._charge(50)
        self.assertFalse(policy.is_triggered())

        self._charge(3000)  # usage, not the floor, spends it
        self.assertTrue(policy.is_triggered())

    def test_organization_credit_with_minimal_consumption_untouched(self):
        self._earlier_uncovered_cost("7701.67")
        invoices_factories.CustomerCreditFactory(
            customer=self.customer, value=2900, expected_consumption=2900
        )
        policy = self._total_policy()

        self._charge(50)
        self.assertFalse(policy.is_triggered())

        self._charge(3000)
        self.assertTrue(policy.is_triggered())

    def _apply_mid_month(self):
        # Staff apply compensations on the open invoice: 1000 of the 2500 is
        # written now, lowering the stored value to 1500 mid-month.
        credit = invoices_factories.CustomerCreditFactory(
            customer=self.customer, value=CREDIT
        )
        self._charge(1000)
        compensations.MonthlyCompensation(self.customer).apply_compensations()
        credit.refresh_from_db()
        self.assertEqual(credit.value, CREDIT - 1000)

    def test_written_compensation_is_not_drawn_twice(self):
        # The re-simulation sees all of this month's usage again; counting the
        # written 1000 a second time would read 500 and fire this 1000 limit.
        self._earlier_uncovered_cost("5000")
        self._apply_mid_month()

        self.assertFalse(self._total_policy(limit_cost=1000).is_triggered())

    def test_mid_month_apply_then_credit_exhausted(self):
        self._earlier_uncovered_cost("5000")
        self._apply_mid_month()

        self._charge(2000)  # 3000 used of 2500: real balance 0

        self.assertTrue(self._total_policy(limit_cost=LIMIT).is_triggered())

    def test_mid_month_apply_balance_below_limit(self):
        self._earlier_uncovered_cost("5000")
        self._apply_mid_month()

        self._charge(600)  # 1600 used of 2500: real balance 900

        self.assertTrue(self._total_policy(limit_cost=950).is_triggered())

    def _capped_floor_apply(self):
        # The floor (3000) is larger than the credit left (1000): the mid-month
        # apply writes the 100 used and takes only the remaining 900 of the
        # floor, which leaves no invoice item behind.
        credit = invoices_factories.CustomerCreditFactory(
            customer=self.customer,
            value=1000,
            expected_consumption=3000,
            grace_coefficient=0,
        )
        self._charge(100)
        compensations.MonthlyCompensation(self.customer).apply_compensations()
        credit.refresh_from_db()
        self.assertEqual(credit.value, 0)

    def test_capped_floor_live_balance(self):
        self._earlier_uncovered_cost("5000")
        self._capped_floor_apply()

        self._charge(1000)  # 1100 used of an opening 1000

        policy = self._total_policy(limit_cost=1000)
        # Restoring the full floor would open the month at 3000 and read 1900.
        self.assertEqual(policy._live_credit_balance(), 0)
        self.assertTrue(policy.is_triggered())
        # Gate 1 pays usage only from credit that existed: 5000 earlier + 1100
        # used - 1000 compensated.
        self.assertEqual(policy.get_current_cost(), decimal.Decimal("5100"))

    def test_project_floor_is_restored(self):
        # A project allocation's own floor is drawn by the apply too; the
        # restore must add it back, or the opening allocation reads short.
        self._earlier_uncovered_cost("5000")
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        project_credit = invoices_factories.ProjectCreditFactory(
            project=self.project, value=2900, expected_consumption=1000
        )
        self._charge(100)
        compensations.MonthlyCompensation(self.customer).apply_compensations()
        project_credit.refresh_from_db()
        self.assertEqual(project_credit.value, 1900)  # 100 used + 900 floor

        self._charge(1000)  # 1100 used of an opening 2900: 1800 left

        self.assertEqual(self._total_policy()._live_credit_balance(), 1800)

    def test_mid_month_apply_balance_above_limit(self):
        self._earlier_uncovered_cost("5000")
        self._apply_mid_month()

        self._charge(600)  # real balance 900

        self.assertFalse(self._total_policy(limit_cost=850).is_triggered())

    def test_eta_agrees_with_is_triggered(self):
        # The projection must start from the same live balance the gate reads,
        # or a policy that has already fired is reported as days away.
        self._earlier_uncovered_cost("7701.67")
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        policy = self._total_policy()

        self._charge(2850)

        self.assertTrue(policy.is_triggered())
        self.assertEqual(policy.get_eta_days(), 0)

    # The resource-creation check must read the same live balance: a copy that
    # compared the stored one let resources through while the policy was fired.

    def _creation_check(self, check, resource, cost):
        """Run a creation check, then invoice `cost` on `resource` as the new
        resource would, so the policy's own verdict can be compared with it."""
        try:
            check(resource, cost)
            blocked = False
        except PolicyException:
            blocked = True
        invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            resource=resource,
            unit_price=cost,
            quantity=1,
        )
        return blocked

    def _blocking_total_policy(self):
        policy = self._total_policy()
        policy.actions = "block_creation_of_new_resources"
        policy.save()
        return policy

    def test_creation_blocked_while_project_policy_is_fired(self):
        self._earlier_uncovered_cost("7701.67")
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        policy = self._blocking_total_policy()
        self._charge(3000)  # the allocation is spent; the stored value is not
        self.assertTrue(policy.is_triggered())

        self.assertTrue(
            self._creation_check(handlers._check_project_policies, self.resource, 1000)
        )

    def test_creation_blocked_while_customer_policy_is_fired(self):
        self._earlier_uncovered_cost("7701.67")
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=2900)
        policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=LIMIT,
            actions="block_creation_of_new_resources",
            period=CustomerEstimatedCostPolicy.Periods.TOTAL,
        )
        self._charge(3000)
        self.assertTrue(policy.is_triggered())

        self.assertTrue(
            self._creation_check(handlers._check_customer_policies, self.resource, 1000)
        )

    def _allocation_with_900_left(self):
        self._earlier_uncovered_cost("7701.67")
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        policy = self._blocking_total_policy()
        self._charge(2000)
        return policy

    def test_creation_allowed_when_credit_covers_the_resource(self):
        policy = self._allocation_with_900_left()

        self.assertFalse(
            self._creation_check(handlers._check_project_policies, self.resource, 500)
        )
        self.assertFalse(policy.is_triggered())

    def test_creation_blocked_when_the_resource_spends_the_credit(self):
        # Before !6531's live balance reached this copy, the stored 2900 let
        # the resource through and the policy fired as soon as it was invoiced.
        policy = self._allocation_with_900_left()
        self.assertFalse(policy.is_triggered())

        self.assertTrue(
            self._creation_check(handlers._check_project_policies, self.resource, 1000)
        )
        self.assertTrue(policy.is_triggered())

    def test_credit_does_not_cover_cost_on_an_uncovered_offering(self):
        # 1050 left, limit 100: a 1000 resource on a covered offering draws the
        # credit down to 50 and fires the policy; on an offering the credit
        # does not cover it leaves the balance untouched.
        uncovered = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=marketplace_factories.OfferingFactory(customer=self.customer),
        )
        self._earlier_uncovered_cost("7701.67")
        credit = invoices_factories.CustomerCreditFactory(
            customer=self.customer, value=1050
        )
        credit.offerings.add(self.resource.offering)
        policy = self._blocking_total_policy()

        self.assertFalse(
            self._creation_check(handlers._check_project_policies, uncovered, 1000)
        )
        self.assertFalse(policy.is_triggered())
        self.assertTrue(
            self._creation_check(handlers._check_project_policies, self.resource, 1000)
        )
        self.assertTrue(policy.is_triggered())

    def test_minimal_consumption_tail_absorbs_added_cost(self):
        # Floor 1000, credit 1050; 50 used this month, so the tail is 950 and
        # total_compensation is 1000 whatever the next 950 of usage is. The
        # organization policy deducts that total, so the 950 resource lowers
        # Gate 1 by nothing, and the live balance falls to 50.
        self._earlier_uncovered_cost("1000")
        invoices_factories.CustomerCreditFactory(
            customer=self.customer, value=1050, expected_consumption=1000
        )
        policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=LIMIT,
            actions="block_creation_of_new_resources",
            period=CustomerEstimatedCostPolicy.Periods.TOTAL,
        )
        self._charge(50)
        self.assertFalse(policy.is_triggered())

        blocked = self._creation_check(
            handlers._check_customer_policies, self.resource, 950
        )

        self.assertTrue(blocked)
        self.assertTrue(policy.is_triggered())
