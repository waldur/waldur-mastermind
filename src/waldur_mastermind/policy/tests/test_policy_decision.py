"""An estimated-cost policy records which check decided its evaluation.

`is_triggered()` returned a bare bool, and the events of the actions it ran
re-read credit balances afterwards, so a fire or reset could not be traced back
to the figures that caused it. `evaluate()` returns the gate and those figures,
and keeps them on the instance for the actions' events.
"""

import datetime
import decimal

from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import utils
from waldur_mastermind.policy.models import (
    CustomerEstimatedCostPolicy,
    ProjectEstimatedCostPolicy,
)
from waldur_mastermind.policy.structures import PolicyGates
from waldur_mastermind.policy.tests import factories as policy_factories

LIMIT = 100


class PolicyDecisionTest(test.APITestCase):
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

    def _earlier_uncovered_cost(self, amount="7701.67"):
        # Cost in an earlier month no credit covered: a TOTAL policy counts it
        # for good, however much credit is added later.
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

    def _project_policy(self, **kwargs):
        defaults = dict(
            scope=self.project,
            limit_cost=LIMIT,
            actions="request_downscaling",
            period=ProjectEstimatedCostPolicy.Periods.TOTAL,
            use_credit=True,
        )
        defaults.update(kwargs)
        return policy_factories.ProjectEstimatedCostPolicyFactory(**defaults)

    def _assert_decision(self, policy, triggered, gate, compared=None):
        decision = policy.evaluate()
        self.assertEqual(decision.triggered, triggered)
        self.assertEqual(decision.gate, gate)
        if compared is not None:
            self.assertEqual(decision.credit_value_compared, decimal.Decimal(compared))
        self.assertEqual(policy.is_triggered(), triggered)
        self.assertEqual(policy.decision.gate, gate)
        return decision

    def test_cost_below_limit(self):
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        self._charge(50)

        decision = self._assert_decision(
            self._project_policy(), False, PolicyGates.COST_BELOW_LIMIT
        )
        self.assertIsNone(decision.credit_value_compared)

    def test_fires_on_cost_although_project_credit_is_left(self):
        # The allocation still has 2850 left, but the cost after credit is over
        # the limit: the credit left is no longer compared with it (#784).
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(50)

        decision = self._assert_decision(
            self._project_policy(), True, PolicyGates.COST_OVER_LIMIT
        )
        self.assertIsNone(decision.credit_value_compared)

    def test_fires_on_cost_although_organization_credit_is_left(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=2900)

        self._assert_decision(self._project_policy(), True, PolicyGates.COST_OVER_LIMIT)

    def test_customer_policy_fires_on_cost_although_credit_is_left(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=2900)
        policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=LIMIT,
            period=CustomerEstimatedCostPolicy.Periods.TOTAL,
        )

        self._assert_decision(policy, True, PolicyGates.COST_OVER_LIMIT)

    def test_cost_over_limit_with_the_credit_used_up(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(3000)

        self._assert_decision(self._project_policy(), True, PolicyGates.COST_OVER_LIMIT)

    def test_the_smaller_limit_fires_first(self):
        # 3000 overspent earlier, 1000 of credit left. The credit-balance gate
        # held the 100 policy back (1000 > 100) while a policy whose cost was
        # over its larger limit fired: the order the limits were meant to give
        # was inverted. Now each policy is judged on its cost alone.
        self._earlier_uncovered_cost("3000")
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=1000)

        self._assert_decision(
            self._project_policy(limit_cost=100), True, PolicyGates.COST_OVER_LIMIT
        )
        self._assert_decision(
            self._project_policy(limit_cost=5000),
            False,
            PolicyGates.COST_BELOW_LIMIT,
        )

    def test_no_credit(self):
        self._earlier_uncovered_cost()

        decision = self._assert_decision(
            self._project_policy(), True, PolicyGates.NO_CREDIT
        )
        self.assertIsNone(decision.credit_value_compared)

    def test_credit_ignored(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)

        decision = self._assert_decision(
            self._project_policy(use_credit=False), True, PolicyGates.CREDIT_IGNORED
        )
        self.assertFalse(decision.use_credit)
        self.assertEqual(decision.credit_deduction, 0)

    def test_offering_policy_reports_no_credit(self):
        self._earlier_uncovered_cost()
        policy = policy_factories.OfferingEstimatedCostPolicyFactory(
            scope=self.resource.offering,
            limit_cost=LIMIT,
            period=ProjectEstimatedCostPolicy.Periods.TOTAL,
        )

        self._assert_decision(policy, True, PolicyGates.NO_CREDIT)

    def test_creation_check_reads_the_same_decision(self):
        # 2900 - 2000 used leaves 900. A 1050 resource is paid by it up to 900,
        # so 150 counts towards the limit of 100; a 500 one is covered whole.
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(2000)
        policy = self._project_policy()

        decision = policy.evaluate(1050, self.resource.offering)

        self.assertTrue(decision.triggered)
        self.assertEqual(decision.gate, PolicyGates.COST_OVER_LIMIT)
        self.assertEqual(decision.evaluated_cost, 150)
        self.assertIsNone(decision.credit_value_compared)
        self.assertTrue(policy.would_trigger(1050, self.resource.offering))
        self.assertFalse(policy.would_trigger(500, self.resource.offering))

    def test_hypothetical_does_not_replace_the_policy_decision(self):
        # The creation check describes a resource that may never exist; the
        # decision the policy's events report must stay its own.
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(2000)
        policy = self._project_policy()
        own = policy.evaluate()
        self.assertEqual(own.gate, PolicyGates.COST_BELOW_LIMIT)

        hypothetical = policy.evaluate(1050, self.resource.offering)

        self.assertEqual(hypothetical.gate, PolicyGates.COST_OVER_LIMIT)
        self.assertEqual(policy.decision, own)

    def test_credit_absorbing_the_added_cost_keeps_the_policy_clear(self):
        # Nothing used yet: a 1000 resource is covered by the 2900 allocation.
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)

        decision = self._project_policy().evaluate(1000, self.resource.offering)

        self.assertFalse(decision.triggered)
        self.assertEqual(decision.gate, PolicyGates.COST_BELOW_LIMIT)
        self.assertIsNone(decision.credit_value_compared)

    def test_offering_policy_refuses_an_added_cost(self):
        # Its cost spans many customers and applies to some of them only, which
        # the added-cost path does not model: handlers keeps its own check.
        policy = policy_factories.OfferingEstimatedCostPolicyFactory(
            scope=self.resource.offering, limit_cost=LIMIT
        )

        with self.assertRaises(NotImplementedError):
            policy.would_trigger(1000, self.resource.offering)
        self.assertFalse(policy.is_triggered())

    def test_api_save_path_events_carry_the_decision(self):
        # Creating a policy that is already over its limit fires it from the
        # serializer's save(); the actions run in an on_commit callback, which
        # must still hold the evaluated instance.
        self._downscaling_offering()
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(3000)
        self.client.force_authenticate(self.fixture.staff)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                policy_factories.ProjectEstimatedCostPolicyFactory.get_list_url(),
                {
                    "scope": structure_factories.ProjectFactory.get_url(self.project),
                    "limit_cost": LIMIT,
                    "period": ProjectEstimatedCostPolicy.Periods.TOTAL,
                    "actions": "request_downscaling",
                    "use_credit": True,
                },
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.data["has_fired"])
        event = logging_models.Event.objects.get(event_type="request_downscaling")
        self.assertEqual(event.context["gate"], PolicyGates.COST_OVER_LIMIT)
        # Always None now, and the event context leaves None out.
        self.assertNotIn("credit_value_compared", event.context)

    def test_action_events_carry_the_decision(self):
        self._downscaling_offering()
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(3000)
        policy = self._project_policy()

        utils.evaluate_policies([policy])

        event = logging_models.Event.objects.get(event_type="request_downscaling")
        self.assertEqual(event.context["gate"], PolicyGates.COST_OVER_LIMIT)
        # Always None now, and the event context leaves None out.
        self.assertNotIn("credit_value_compared", event.context)
        self.assertEqual(event.context["limit_cost"], LIMIT)
        self.assertTrue(event.context["triggered"])
        self.assertIn("evaluated_cost", event.context)
        self.assertIn("credit_deduction", event.context)

    def _downscaling_offering(self):
        offering = self.resource.offering
        offering.plugin_options.update({"supports_downscaling": True})
        offering.save()

    def test_reset_events_carry_the_decision(self):
        # This month's usage is 200 beyond the allocation, so it fires; a
        # top-up covers it and resets the policy.
        self._downscaling_offering()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(3100)
        policy = self._project_policy()
        utils.evaluate_policies([policy])
        policy.refresh_from_db()
        self.assertTrue(policy.has_fired)

        invoices_models.ProjectCredit.objects.filter(project=self.project).update(
            value=10000
        )
        utils.evaluate_policies([policy])

        event = logging_models.Event.objects.get(event_type="reset_downscaling")
        self.assertEqual(event.context["gate"], PolicyGates.COST_BELOW_LIMIT)
        self.assertFalse(event.context["triggered"])

    def test_a_top_up_does_not_reset_overspend_from_earlier_months(self):
        # A TOTAL policy keeps counting what earlier months overspent; added
        # credit only pays for this month's usage, so the policy stays fired.
        self._downscaling_offering()
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(3000)
        policy = self._project_policy()
        utils.evaluate_policies([policy])

        invoices_models.ProjectCredit.objects.filter(project=self.project).update(
            value=10000
        )
        utils.evaluate_policies([policy])

        policy.refresh_from_db()
        self.assertTrue(policy.has_fired)
        self.assertEqual(policy.decision.gate, PolicyGates.COST_OVER_LIMIT)
        self.assertFalse(
            logging_models.Event.objects.filter(event_type="reset_downscaling").exists()
        )
