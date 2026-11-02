"""A cost policy that ignores credit must not count credit compensation.

Compensations are ordinary invoice items with a negative unit_price and
`credit` set, carrying the project and resource of the item they pay for. A
`use_credit=False` project policy and an offering policy pick them up with
everything else, so the month-end compensation run lowered their cost by what
credit paid, and a fired policy could reset without anything having changed in
what was spent (#906).
"""

import datetime
from decimal import Decimal

from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import compensations
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.exceptions import PolicyException
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import handlers
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.tests import factories as policy_factories

COST = 1000
# Enough to pay the whole month.
CREDIT = 5000
# Pays only part of it.
PARTIAL_CREDIT = 300
LIMIT = 500


class UncreditedPolicyCompensationTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.resource = self.fixture.resource

        month_start = core_utils.month_start(datetime.date.today())
        invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=month_start.year,
            month=month_start.month,
            tax_percent=0,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.project,
            resource=self.resource,
            unit_price=COST,
            quantity=1,
        )
        self.credit = invoices_factories.CustomerCreditFactory(
            customer=self.customer, value=CREDIT
        )

    def make_project_policy(self, use_credit):
        return policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=LIMIT,
            actions="block_creation_of_new_resources",
            period=ProjectEstimatedCostPolicy.Periods.TOTAL,
            use_credit=use_credit,
        )

    def make_offering_policy(self):
        return policy_factories.OfferingEstimatedCostPolicyFactory(
            scope=self.fixture.offering,
            limit_cost=LIMIT,
            actions="block_creation_of_new_resources",
            period=ProjectEstimatedCostPolicy.Periods.TOTAL,
        )

    def write_compensation(self):
        compensations.MonthlyCompensation(self.customer).apply_compensations()
        self.assertTrue(
            self.resource.invoice_items.filter(credit__isnull=False).exists(),
            "the credit must have paid for the month",
        )

    def assert_unaffected_by_compensation(self, policy):
        self.assertEqual(policy.get_current_cost(), COST)
        self.assertTrue(policy.is_triggered())

        self.write_compensation()

        self.assertEqual(policy.get_current_cost(), COST)
        self.assertTrue(policy.is_triggered())
        breakdown = policy.get_cost_breakdown()
        self.assertEqual(breakdown.cost_before_credit, COST)
        self.assertEqual(breakdown.credit_drawn, 0)

    def test_project_policy_ignoring_credit_keeps_its_cost(self):
        self.assert_unaffected_by_compensation(self.make_project_policy(False))

    def test_offering_policy_keeps_its_cost(self):
        self.assert_unaffected_by_compensation(self.make_offering_policy())

    def assert_unaffected_by_deleting_the_credit(self, policy):
        self.write_compensation()
        # `credit` is SET_NULL, so the compensation items outlive the credit.
        self.credit.delete()
        self.assertTrue(
            self.resource.invoice_items.filter(unit_price__lt=0).exists(),
        )

        self.assertEqual(policy.get_current_cost(), COST)
        self.assertTrue(policy.is_triggered())
        self.assertEqual(policy.get_cost_breakdown().credit_drawn, 0)

    def test_project_policy_ignoring_credit_keeps_its_cost_once_credit_is_deleted(
        self,
    ):
        self.assert_unaffected_by_deleting_the_credit(self.make_project_policy(False))

    def test_offering_policy_keeps_its_cost_once_credit_is_deleted(self):
        self.assert_unaffected_by_deleting_the_credit(self.make_offering_policy())

    def assert_creation_blocked(self):
        with self.assertRaises(PolicyException) as raised:
            handlers.validate_resource_creation_against_cost_policies(
                sender=None, resource=self.resource, cost=Decimal(1)
            )
        return str(raised.exception.detail[0])

    def test_creation_check_agrees_with_project_policy_ignoring_credit(self):
        self.make_project_policy(False)
        self.write_compensation()
        self.assert_creation_blocked()

    def test_creation_check_agrees_with_offering_policy(self):
        self.make_offering_policy()
        self.write_compensation()
        message = self.assert_creation_blocked()
        # The cost is shown to the user, to the cent.
        self.assertIn(f"Projected cost ({COST + 1}.00)", message)

    def test_credit_aware_project_policy_still_counts_compensation(self):
        policy = self.make_project_policy(True)
        self.write_compensation()

        self.assertEqual(policy.get_current_cost(), 0)
        self.assertFalse(policy.is_triggered())
        self.assertEqual(policy.get_cost_breakdown().credit_drawn, COST)

    def test_credit_aware_policy_keeps_its_figures_once_credit_is_deleted(self):
        policy = self.make_project_policy(True)
        self.write_compensation()
        self.credit.delete()

        self.assertEqual(policy.get_current_cost(), 0)
        breakdown = policy.get_cost_breakdown()
        self.assertEqual(breakdown.cost_before_credit, COST)
        self.assertEqual(breakdown.credit_drawn, COST)
        self.assertEqual(policy._gross_cost_this_month(policy._cost_inputs()[0]), COST)

    def test_new_credit_does_not_draw_again_what_a_deleted_credit_wrote(self):
        self.credit.value = PARTIAL_CREDIT
        self.credit.save()
        self.write_compensation()
        self.credit.delete()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=CREDIT)
        policy = self.make_project_policy(True)

        self.assertEqual(policy.get_current_cost(), 0)
        breakdown = policy.get_cost_breakdown()
        self.assertEqual(breakdown.credit_drawn, PARTIAL_CREDIT)
        self.assertEqual(breakdown.credit_pending, COST - PARTIAL_CREDIT)

    def test_policy_forecasts_what_a_new_credit_then_draws(self):
        self.credit.value = PARTIAL_CREDIT
        self.credit.save()
        self.write_compensation()
        self.credit.delete()
        new_credit = invoices_factories.CustomerCreditFactory(
            customer=self.customer, value=CREDIT
        )
        policy = self.make_project_policy(True)
        forecast = policy.get_cost_breakdown().credit_pending

        compensations.MonthlyCompensation(self.customer).apply_compensations()

        new_credit.refresh_from_db()
        self.assertEqual(CREDIT - new_credit.value, forecast)
        self.assertEqual(policy.get_current_cost(), 0)
        self.assertEqual(policy.get_cost_breakdown().credit_pending, 0)

    def test_policy_forecast_counts_what_a_deleted_project_credit_paid(self):
        invoices_factories.ProjectCreditFactory(
            project=self.project, value=PARTIAL_CREDIT
        )
        self.write_compensation()
        self.credit.delete()
        new_credit = invoices_factories.CustomerCreditFactory(
            customer=self.customer, value=CREDIT
        )
        policy = self.make_project_policy(True)
        forecast = policy.get_cost_breakdown().credit_pending

        compensations.MonthlyCompensation(self.customer).apply_compensations()

        new_credit.refresh_from_db()
        self.assertEqual(forecast, COST - PARTIAL_CREDIT)
        self.assertEqual(CREDIT - new_credit.value, forecast)

    def test_customer_policy_still_counts_compensation(self):
        policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=LIMIT,
            actions="notify_organization_owners",
            period=ProjectEstimatedCostPolicy.Periods.TOTAL,
        )
        self.write_compensation()

        self.assertEqual(policy.get_current_cost(), Decimal(0))
        self.assertFalse(policy.is_triggered())
        self.assertEqual(policy.get_cost_breakdown().credit_drawn, COST)
