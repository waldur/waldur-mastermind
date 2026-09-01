"""`eta_days` must never invent a date, and must never hide a real one.

The client used to derive this and could not: waldur/waldur-homeport#244
divided the remaining headroom by the *credit burn rate* — the compensating
side of the subtraction `current_cost` is the result of — and told
credit-funded projects their resources would be paused within days, on
policies that were not triggered at all. These tests pin the two halves that
matter: a project whose credit covers its costs gets no date, and a project
genuinely over its limit is not silently passed over.
"""

import datetime
from decimal import Decimal

from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.models import PeriodMixin
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import eta as policy_eta
from waldur_mastermind.policy.tests import factories as policy_factories

P = PeriodMixin.Periods
MID_MONTH = datetime.date(2026, 8, 15)
LAST_DAY = datetime.date(2026, 8, 31)


class ProjectEtaDaysTest(test.APISimpleTestCase):
    """The projection itself, with the date and every rate as an input."""

    def project(self, **kwargs):
        params = dict(
            limit_cost=20000,
            current_cost=7500,
            gross_this_month=7500,
            uncompensated_this_month=7500,
            credit_days=0,
            credit_limit_days=0,
            period=P.TOTAL,
            today=MID_MONTH,
        )
        params.update(kwargs)
        return policy_eta.project_eta_days(**params)

    def test_no_credit_projects_from_the_gross_rate(self):
        # 7500 over 15 elapsed days is 500/day; 12500 of headroom is 25 days.
        self.assertEqual(self.project(), 25)

    def test_an_exceeded_limit_is_reported_as_reached(self):
        self.assertEqual(self.project(current_cost=25000), 0)

    def test_a_limit_exactly_reached_has_not_been_crossed(self):
        """`_is_triggered` is `> limit_cost`, so equality is not a breach."""
        self.assertIsNone(
            self.project(
                current_cost=20000, gross_this_month=0, uncompensated_this_month=0
            )
        )

    def test_a_covered_project_gets_no_date_from_the_credit_draw(self):
        """The shape of homeport#244.

        The credit covers everything the project spends, so `current_cost` does
        not move. A large credit draw must not shorten the projection — under
        the old client-side derivation it was the numerator's divisor.
        """
        self.assertIsNone(
            self.project(
                limit_cost=1567,
                current_cost=0,
                gross_this_month=15771,
                uncompensated_this_month=0,
                credit_days=None,
                credit_limit_days=None,
            )
        )

    def test_cost_the_credit_cannot_cover_still_accrues(self):
        """A credit covers named offerings only; the rest lands immediately."""
        # 150 uncovered over 15 days is 10/day, so 1000 of headroom is 100 days.
        self.assertEqual(
            self.project(
                limit_cost=1000,
                current_cost=0,
                gross_this_month=15771,
                uncompensated_this_month=150,
                credit_days=185,
                credit_limit_days=0,
            ),
            100,
        )

    def test_the_full_rate_applies_once_the_credit_is_gone(self):
        # 30 days of credit, then 15000/15 = 1000/day against 100000 of room.
        self.assertEqual(
            self.project(
                limit_cost=100000,
                current_cost=0,
                gross_this_month=15000,
                uncompensated_this_month=0,
                credit_days=30,
                credit_limit_days=0,
            ),
            130,
        )

    def test_nothing_spent_projects_nothing(self):
        self.assertIsNone(
            self.project(gross_this_month=0, uncompensated_this_month=0, current_cost=0)
        )

    def test_a_windowed_policy_does_not_project_past_its_window(self):
        """MONTH_1 re-measures at month end, so a later date is meaningless."""
        self.assertIsNone(self.project(period=P.MONTH_1))
        self.assertIsNone(self.project(period=P.MONTH_1, today=LAST_DAY))

    def test_a_total_policy_has_no_window_to_stop_at(self):
        self.assertEqual(self.project(limit_cost=90000, period=P.TOTAL), 165)

    def test_a_limit_out_of_sight_is_not_projected(self):
        """A trickle of spend against a huge limit is millions of days away.

        Such a figure is not knowledge, and it overflows `datetime.date` when
        the serializer turns it into `eta_date`.
        """
        self.assertIsNone(self.project(limit_cost=10**9, period=P.TOTAL))

    def test_the_horizon_is_a_year(self):
        # 500/day: 365 days of headroom projects, one more day does not.
        self.assertEqual(self.project(limit_cost=7500 + 500 * 365, period=P.TOTAL), 365)
        self.assertIsNone(self.project(limit_cost=7500 + 500 * 366, period=P.TOTAL))

    def test_the_credit_balance_gates_the_cost_crossing(self):
        """`is_triggered` returns `credit.value <= limit_cost` once the cost
        test passes, so a project whose balance is still above the limit cannot
        fire however much it spends. The later of the two is when it fires."""
        # Cost alone would say 25 days; the balance does not clear until 90.
        self.assertEqual(self.project(credit_limit_days=90), 90)
        # And a balance that never reaches the limit means it never fires.
        self.assertIsNone(self.project(credit_limit_days=None))

    def test_a_crossed_limit_still_waits_for_the_balance(self):
        self.assertEqual(self.project(current_cost=25000, credit_limit_days=40), 40)
        self.assertEqual(self.project(current_cost=25000, credit_limit_days=0), 0)

    def test_a_sub_day_projection_is_a_day_away_not_a_breach(self):
        """0 is reserved for a limit already crossed, so it must never be the
        result of rounding a real projection down."""
        self.assertEqual(self.project(limit_cost=7600), 1)

    def test_the_longer_windows_are_not_cut_off_at_month_end(self):
        """MONTH_1 starts its total again; MONTH_3 and MONTH_12 only drop their
        oldest month, so clamping them to the calendar month hid real dates."""
        self.assertIsNone(self.project(period=P.MONTH_1))
        self.assertEqual(self.project(period=P.MONTH_3), 25)
        self.assertEqual(self.project(period=P.MONTH_12), 25)

    def test_a_windowed_policy_projects_inside_its_window(self):
        # 5000 of headroom at 500/day is 10 days, inside August's remaining 16.
        self.assertEqual(self.project(limit_cost=12500, period=P.MONTH_1), 10)


class CreditRunwayTest(test.APISimpleTestCase):
    class Credit:
        def __init__(self, value, minimal, last_month=0):
            self.value = self.spendable_value = Decimal(value)
            self.minimal_consumption = Decimal(minimal)
            self.consumption_last_month = Decimal(last_month)

    def test_no_credit_is_no_runway_rather_than_an_endless_one(self):
        self.assertEqual(policy_eta.credit_days_remaining(None, 0), 0)

    def test_a_credit_that_is_never_drawn_never_depletes(self):
        self.assertIsNone(
            policy_eta.credit_days_remaining(self.Credit(1000, minimal=0), 0)
        )

    def test_the_larger_of_floor_and_last_month_sets_the_rate(self):
        # The floor is taken whether or not it is used, so it is the better
        # lower bound when last month came in under it.
        credit = self.Credit(18000, minimal=18000, last_month=3600)
        self.assertEqual(policy_eta.credit_days_remaining(credit, 0), 30)

    def test_a_credit_with_no_floor_still_depletes_at_the_observed_rate(self):
        """A fresh allocation with no minimal consumption, in its first month,
        has neither figure — but it is still being spent on what it
        compensates. Calling that "never depletes" suppressed the projection
        for such projects entirely."""
        credit = self.Credit(1000, minimal=0)
        self.assertEqual(policy_eta.credit_days_remaining(credit, 10), 100)

    def test_the_observed_rate_cannot_lower_the_contractual_draw(self):
        credit = self.Credit(18000, minimal=18000)
        self.assertEqual(policy_eta.credit_days_remaining(credit, 1), 30)


class EtaApiTest(test.APITestCase):
    """The projection has to survive the serializer, not just the model."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        month_start = core_utils.month_start(datetime.date.today())
        invoice = invoices_factories.InvoiceFactory(
            customer=self.fixture.customer,
            year=month_start.year,
            month=month_start.month,
            tax_percent=0,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.fixture.project,
            resource=self.fixture.resource,
            unit_price=1000,
            quantity=1,
        )
        self.policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.fixture.project,
            limit_cost=100,
            actions="notify_project_team",
            period=P.TOTAL,
            use_credit=False,
        )

    def get(self, **query):
        self.client.force_authenticate(self.fixture.staff)
        url = policy_factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)
        response = self.client.get(url, query)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_an_exceeded_limit_is_served_as_reached_today(self):
        data = self.get()
        self.assertEqual(data["eta_days"], 0)
        self.assertEqual(data["eta_date"], datetime.date.today())
        # And it agrees with the flag the policy itself raises.
        self.assertTrue(self.policy.is_triggered())

    def test_the_projection_can_be_excluded_from_the_response(self):
        """It costs the same simulation as `current_cost`, so it is skippable."""
        data = self.get(field=["uuid", "limit_cost"])
        self.assertNotIn("eta_days", data)
        self.assertNotIn("eta_date", data)

    def test_a_limit_out_of_sight_serves_a_null_rather_than_overflowing(self):
        """Regression: this 500'd with `OverflowError: date value out of range`
        before the projection gained a horizon."""
        self.policy.limit_cost = 10**9
        self.policy.save()
        data = self.get()
        self.assertIsNone(data["eta_days"])
        self.assertIsNone(data["eta_date"])

    def test_an_offering_policy_answers_without_a_credit(self):
        """It shares the serializer and has no single customer, so it must not
        500 — the same requirement `current_cost` already carries."""
        offering_policy = policy_factories.OfferingEstimatedCostPolicyFactory(
            scope=self.fixture.offering, limit_cost=100
        )
        self.client.force_authenticate(self.fixture.staff)
        url = policy_factories.OfferingEstimatedCostPolicyFactory.get_url(
            offering_policy
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("eta_days", response.data)


class CreditCoveredProjectTest(test.APITestCase):
    """A compensated project must not be handed a date.

    This is waldur/waldur-homeport#244 reached through the server rather than
    the client: a project whose credit covers what it spends, told its
    resources are about to be paused. The unit tests above cannot catch these —
    they are handed the rates already computed.
    """

    # Compensation is computed pre-tax while the policy's total carries tax, so
    # on a taxed deployment a fully covered project still carries the tax in
    # `current_cost`. Zero tax hides every problem below.
    TAX_PERCENT = 20

    def build(self, *, limit_cost, with_project_credit=True):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        month_start = core_utils.month_start(datetime.date.today())
        invoice = invoices_factories.InvoiceFactory(
            customer=self.fixture.customer,
            year=month_start.year,
            month=month_start.month,
            tax_percent=self.TAX_PERCENT,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.fixture.project,
            resource=self.fixture.resource,
            unit_price=1000,
            quantity=1,
        )
        customer_credit = invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=500000
        )
        customer_credit.offerings.add(self.fixture.offering)
        if with_project_credit:
            invoices_factories.ProjectCreditFactory(
                project=self.fixture.project, value=500000
            )
        self.policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.fixture.project,
            limit_cost=limit_cost,
            actions="request_pausing",
            period=P.TOTAL,
            use_credit=True,
        )

    def eta(self):
        self.client.force_authenticate(self.fixture.staff)
        url = policy_factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_a_covered_project_well_inside_its_limit_gets_no_date(self):
        """The balance (500000) is far above the limit, so the policy cannot
        fire — whatever the uncompensated tax residue is doing to the cost."""
        self.build(limit_cost=5000)
        data = self.eta()
        self.assertFalse(self.policy.is_triggered())
        self.assertIsNone(data["eta_days"])
        self.assertIsNone(data["eta_date"])

    def test_a_project_funded_by_the_organization_balance_gets_no_date(self):
        """`is_triggered` falls back to CustomerCredit when the project has no
        allocation of its own, so the projection has to as well. Reading only
        ProjectCredit reported "no credit", and the projection then charged the
        full gross rate against a cost that is in fact compensated."""
        self.build(limit_cost=5000, with_project_credit=False)
        data = self.eta()
        self.assertFalse(self.policy.is_triggered())
        self.assertIsNone(data["eta_days"])

    def test_a_crossed_limit_the_credit_still_covers_is_not_reported_reached(self):
        """`is_triggered` applies a test the cost figures cannot see: a credit
        balance still above the limit holds the policy back. Serving 0 while the
        policy itself reports False is the contradiction this field exists to
        prevent a client inventing."""
        self.build(limit_cost=100)
        self.assertGreater(self.policy.get_current_cost(), self.policy.limit_cost)
        self.assertFalse(self.policy.is_triggered())
        self.assertIsNone(self.eta()["eta_days"])

    def test_a_crossed_limit_with_no_credit_left_is_reported_reached(self):
        """The other side of the same reconciliation: once the credit can no
        longer cover the overage, the policy triggers and 0 is correct."""
        self.build(limit_cost=100)
        invoices_models.ProjectCredit.objects.filter(
            project=self.fixture.project
        ).update(value=0)
        invoices_models.CustomerCredit.objects.filter(
            customer=self.fixture.customer
        ).update(value=0)
        self.assertTrue(self.policy.is_triggered())
        data = self.eta()
        self.assertEqual(data["eta_days"], 0)
        self.assertEqual(data["eta_date"], datetime.date.today())
