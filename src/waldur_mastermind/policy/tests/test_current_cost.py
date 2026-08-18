"""`current_cost` must be the figure the policy actually evaluates.

The API exposes it so clients stop re-deriving the comparison. That is only
worth anything if the two cannot drift: the value served must be the same one
`is_triggered` compares against `limit_cost`, including the credit-still-to-be
-drawn deduction that no client can simulate.
"""

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import compensations
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.tests import factories as policy_factories

EARLIER_COST = 300
THIS_MONTH_COST = 50
CREDIT = 200
LIMIT = 270


class CurrentCostTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.resource = self.fixture.resource

        month_start = core_utils.month_start(datetime.date.today())
        previous = month_start - relativedelta(months=1)

        for invoice_month, cost in (
            (previous, EARLIER_COST),
            (month_start, THIS_MONTH_COST),
        ):
            invoice = invoices_factories.InvoiceFactory(
                customer=self.customer,
                year=invoice_month.year,
                month=invoice_month.month,
                tax_percent=0,
            )
            invoices_factories.InvoiceItemFactory(
                invoice=invoice,
                project=self.project,
                resource=self.resource,
                unit_price=cost,
                quantity=1,
            )

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

    def get_current_cost(self):
        self.client.force_authenticate(self.fixture.staff)
        url = policy_factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return Decimal(str(response.data["current_cost"]))

    def test_served_cost_is_the_one_compared_against_the_limit(self):
        # Before compensation is written, the deduction is the whole projected
        # draw — the case a client cannot reproduce on its own.
        served = self.get_current_cost()
        self.assertEqual(served, self.policy.get_current_cost())
        self.assertEqual(served > self.policy.limit_cost, self.policy.is_triggered())

    def test_served_cost_still_matches_once_compensation_is_written(self):
        compensations.MonthlyCompensation(self.customer).apply_compensations()

        served = self.get_current_cost()
        self.assertEqual(served, self.policy.get_current_cost())
        self.assertEqual(served > self.policy.limit_cost, self.policy.is_triggered())
        # The written compensation is already in the total, so only the earlier
        # month's charge remains — deducting the projection again would put this
        # below the limit and silently spare the policy.
        self.assertEqual(served, EARLIER_COST)

    def test_credit_is_ignored_when_the_policy_ignores_it(self):
        self.policy.use_credit = False
        self.policy.save()

        self.assertEqual(
            self.get_current_cost(), Decimal(EARLIER_COST + THIS_MONTH_COST)
        )

    def test_offering_policy_serves_the_figure_without_credit(self):
        """An offering policy spans many customers, so no credit applies to it.

        It shares the serializer, so it must still answer — deducting one
        customer's credit here would be meaningless, and failing to implement
        the method at all would 500 the endpoint.
        """
        offering_policy = policy_factories.OfferingEstimatedCostPolicyFactory(
            scope=self.fixture.offering,
            limit_cost=LIMIT,
        )
        self.client.force_authenticate(self.fixture.staff)
        url = policy_factories.OfferingEstimatedCostPolicyFactory.get_url(
            offering_policy
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        served = Decimal(str(response.data["current_cost"]))
        self.assertEqual(served, offering_policy.get_current_cost())
        self.assertEqual(
            served > offering_policy.limit_cost, offering_policy.is_triggered()
        )

    def test_field_selection_skips_the_figure(self):
        self.client.force_authenticate(self.fixture.staff)
        url = policy_factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)
        response = self.client.get(url, {"field": ["uuid", "limit_cost"]})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("current_cost", response.data)
