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
        # Cost in an earlier month no credit covered, so a TOTAL policy's
        # Gate 1 is open and Gate 2 decides.
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

    def test_held_by_project_credit(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(50)

        self._assert_decision(
            self._project_policy(), False, PolicyGates.HELD_BY_PROJECT_CREDIT, 2850
        )

    def test_held_by_customer_credit_when_organization_balance_is_lower(self):
        # The allocation reads 2900, but the organization credit funding it is
        # down to 1000: that is the balance Gate 2 compares, and it holds.
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        invoices_models.CustomerCredit.objects.filter(customer=self.customer).update(
            value=1000
        )

        self._assert_decision(
            self._project_policy(), False, PolicyGates.HELD_BY_CUSTOMER_CREDIT, 1000
        )

    def test_held_by_customer_credit_without_project_allocation(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=2900)

        self._assert_decision(
            self._project_policy(), False, PolicyGates.HELD_BY_CUSTOMER_CREDIT, 2900
        )

    def test_customer_policy_held_by_customer_credit(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=2900)
        policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=LIMIT,
            period=CustomerEstimatedCostPolicy.Periods.TOTAL,
        )

        self._assert_decision(policy, False, PolicyGates.HELD_BY_CUSTOMER_CREDIT, 2900)

    def test_credit_exhausted(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(3000)

        self._assert_decision(
            self._project_policy(), True, PolicyGates.CREDIT_EXHAUSTED, 0
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
        # 2900 - 2000 used leaves 900; a 1000 resource would spend it.
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(2000)
        policy = self._project_policy()

        decision = policy.evaluate(1000, self.resource.offering)

        self.assertTrue(decision.triggered)
        self.assertEqual(decision.gate, PolicyGates.CREDIT_EXHAUSTED)
        self.assertEqual(decision.credit_value_compared, 0)
        self.assertTrue(policy.would_trigger(1000, self.resource.offering))
        self.assertFalse(policy.would_trigger(500, self.resource.offering))

    def test_hypothetical_does_not_replace_the_policy_decision(self):
        # The creation check describes a resource that may never exist; the
        # decision the policy's events report must stay its own.
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(2000)
        policy = self._project_policy()
        own = policy.evaluate()
        self.assertEqual(own.gate, PolicyGates.HELD_BY_PROJECT_CREDIT)

        hypothetical = policy.evaluate(1000, self.resource.offering)

        self.assertEqual(hypothetical.gate, PolicyGates.CREDIT_EXHAUSTED)
        self.assertEqual(policy.decision, own)

    def test_credit_absorbing_the_added_cost_reports_the_balance(self):
        # Nothing used yet: a 1000 resource is covered by the 2900 allocation,
        # so gate 1 closes again, and the balance left is what held it.
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)

        decision = self._project_policy().evaluate(1000, self.resource.offering)

        self.assertFalse(decision.triggered)
        self.assertEqual(decision.gate, PolicyGates.COST_BELOW_LIMIT)
        self.assertEqual(decision.credit_value_compared, 1900)

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
        offering = self.resource.offering
        offering.plugin_options.update({"supports_downscaling": True})
        offering.save()
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
        self.assertEqual(event.context["gate"], PolicyGates.CREDIT_EXHAUSTED)
        self.assertEqual(event.context["credit_value_compared"], "0.00")

    def test_action_events_carry_the_decision(self):
        offering = self.resource.offering
        offering.plugin_options.update({"supports_downscaling": True})
        offering.save()
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(3000)
        policy = self._project_policy()

        utils.evaluate_policies([policy])

        event = logging_models.Event.objects.get(event_type="request_downscaling")
        self.assertEqual(event.context["gate"], PolicyGates.CREDIT_EXHAUSTED)
        self.assertEqual(event.context["credit_value_compared"], "0.00")
        self.assertEqual(event.context["limit_cost"], LIMIT)
        self.assertTrue(event.context["triggered"])
        self.assertIn("evaluated_cost", event.context)
        self.assertIn("credit_deduction", event.context)

    def test_reset_events_carry_the_decision(self):
        offering = self.resource.offering
        offering.plugin_options.update({"supports_downscaling": True})
        offering.save()
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(3000)
        policy = self._project_policy()
        utils.evaluate_policies([policy])

        # A top-up puts the allocation back above the limit.
        invoices_models.ProjectCredit.objects.filter(project=self.project).update(
            value=10000
        )
        utils.evaluate_policies([policy])

        event = logging_models.Event.objects.get(event_type="reset_downscaling")
        self.assertEqual(event.context["gate"], PolicyGates.HELD_BY_PROJECT_CREDIT)
        self.assertFalse(event.context["triggered"])
