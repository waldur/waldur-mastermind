"""Cost policies and a project credit paying earlier invoices.

A period-Total policy counts every month the credit did not cover, so a top-up
alone never clears it (#784). Paying the earlier months from the allocation
does, and the payment, written on the current invoice, must count in the month
it pays: the current month's figures stay as they were.
"""

import decimal

from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import settlement
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import handlers
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.tests import factories as policy_factories

Periods = ProjectEstimatedCostPolicy.Periods


@freeze_time("2026-10-15")
class EarlierInvoicesPolicyTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=200000)

    def _setup(self, earlier, this_month, allocation):
        september = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=2026,
            month=9,
            state=invoices_models.Invoice.States.CREATED,
            tax_percent=0,
        )
        self._charge(september, earlier)
        self.october = invoices_factories.InvoiceFactory(
            customer=self.customer, year=2026, month=10, tax_percent=0
        )
        self._charge(self.october, this_month)
        self.credit = invoices_factories.ProjectCreditFactory(
            project=self.project, value=allocation
        )

    def _charge(self, invoice, amount):
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.project,
            resource=self.resource,
            unit_price=decimal.Decimal(amount),
            quantity=1,
        )

    def _policy(self, limit, period):
        return policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=limit,
            actions="notify_project_team",
            period=period,
            use_credit=True,
        )

    def _pay_earlier_invoices(self, allocation):
        self.credit.value = allocation
        self.credit.pay_earlier_invoices = True
        self.credit.save()
        return settlement.pay_earlier_invoices(self.credit).total

    def test_paying_earlier_invoices_clears_total_policies(self):
        # The #784 example: 20 000 overspent earlier, the new month just begun.
        self._setup(earlier=20000, this_month=10, allocation=1000)
        downscaling = self._policy(100, Periods.TOTAL)
        pausing = self._policy(5000, Periods.TOTAL)
        self.assertTrue(downscaling.is_triggered())
        self.assertTrue(pausing.is_triggered())

        self.assertEqual(self._pay_earlier_invoices(25000), 20000)

        self.assertFalse(downscaling.is_triggered())
        self.assertFalse(pausing.is_triggered())
        # October's 10 is still deducted as pending credit, so nothing is left.
        self.assertEqual(downscaling.get_current_cost(), 0)

    def test_the_deployments_figures(self):
        # 2 871 overspent earlier, 6 163 this month against a 2 300 allocation.
        self._setup(earlier=2871, this_month=6163, allocation=2300)
        downscaling = self._policy(100, Periods.TOTAL)
        pausing = self._policy(5615, Periods.TOTAL)
        self.assertTrue(downscaling.is_triggered())
        self.assertTrue(pausing.is_triggered())

        # A 10 000 allocation pays October's 6 163 first, then September.
        self.assertEqual(self._pay_earlier_invoices(10000), 2871)

        self.assertFalse(downscaling.is_triggered())
        self.assertFalse(pausing.is_triggered())

    def test_month_policy_cost_does_not_go_below_zero(self):
        # The measured failure: a 20 000 payment counted in October made a
        # Month policy's cost -19 990.
        self._setup(earlier=20000, this_month=10, allocation=1000)
        policy = self._policy(100, Periods.MONTH_1)

        before = policy.get_current_cost()

        self._pay_earlier_invoices(25000)

        self.assertEqual(before, 0)
        self.assertEqual(policy.get_current_cost(), 0)
        self.assertEqual(
            policy._uncompensated_cost_this_month(*policy._cost_inputs()), 0
        )

    def test_three_month_policy_counts_the_payment_of_a_month_inside_it(self):
        self._setup(earlier=20000, this_month=10, allocation=1000)
        policy = self._policy(5000, Periods.MONTH_3)
        self.assertTrue(policy.is_triggered())

        self._pay_earlier_invoices(25000)

        self.assertFalse(policy.is_triggered())

    def test_offering_creation_check_leaves_the_payment_out_of_this_month(self):
        self._setup(earlier=20000, this_month=10, allocation=1000)
        self._pay_earlier_invoices(25000)
        offering_policy = policy_factories.OfferingEstimatedCostPolicyFactory(
            scope=self.resource.offering, period=Periods.MONTH_1
        )

        total = handlers._existing_total(
            invoices_models.InvoiceItem.objects.filter(
                resource__offering=self.resource.offering
            ),
            offering_policy,
        )

        self.assertEqual(total, 10)
