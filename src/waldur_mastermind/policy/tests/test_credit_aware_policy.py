"""
Tests that cost policies account for available credit balance.

A project with sufficient remaining credit should not have its resources
paused, even if the raw invoice total exceeds the policy limit.
"""

from django.test import override_settings
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.models import (
    CustomerEstimatedCostPolicy,
    ProjectEstimatedCostPolicy,
)
from waldur_mastermind.policy.tests import factories as policy_factories


@override_settings(task_always_eager=True)
@freeze_time("2026-04-01")
class ProjectCreditPreventsTriggering(test.APITestCase):
    """ProjectEstimatedCostPolicy should not fire when the project
    has sufficient remaining credit to cover costs."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            month=4,
            year=2026,
            tax_percent=0,
        )
        # CustomerCredit is required before creating ProjectCredit
        self.customer_credit = invoices_factories.CustomerCreditFactory(
            customer=self.customer,
            value=100000,
        )

    def _create_policy(self, limit_cost=100):
        return policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=limit_cost,
            actions="request_pausing",
            period=ProjectEstimatedCostPolicy.Periods.TOTAL,
        )

    def _create_item(self, unit_price):
        # Don't set resource — MonthlyCompensation excludes resource=None items,
        # so this item won't be compensated. This isolates the credit check logic.
        return invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            unit_price=unit_price,
            quantity=1,
        )

    def test_not_triggered_when_project_credit_covers_cost(self):
        """Project with 33,625 credit should not trigger on 200 cost."""
        invoices_factories.ProjectCreditFactory(
            project=self.project,
            value=33625,
        )
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertFalse(
            policy.is_triggered(),
            "Policy should not trigger when project credit exceeds limit_cost.",
        )

    def test_triggered_when_no_credit_at_all(self):
        """Without any credit, cost exceeding limit should trigger."""
        self.customer_credit.delete()
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertTrue(
            policy.is_triggered(),
            "Policy should trigger when cost exceeds limit and no credit exists.",
        )

    def test_triggered_when_project_credit_exhausted(self):
        """Zero remaining project credit should trigger."""
        invoices_factories.ProjectCreditFactory(
            project=self.project,
            value=0,
        )
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertTrue(
            policy.is_triggered(),
            "Policy should trigger when project credit is exhausted.",
        )

    def test_triggered_when_project_credit_below_limit(self):
        """Project credit smaller than limit_cost should trigger."""
        invoices_factories.ProjectCreditFactory(
            project=self.project,
            value=50,  # Less than limit_cost=100
        )
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertTrue(
            policy.is_triggered(),
            "Policy should trigger when project credit is below limit_cost.",
        )

    def test_project_credit_takes_precedence_over_customer_credit(self):
        """When ProjectCredit exists, only it matters — even if
        CustomerCredit is large enough to cover the cost."""
        # Small project credit (below limit) but large customer credit
        invoices_factories.ProjectCreditFactory(
            project=self.project,
            value=50,  # Below limit_cost=100
        )
        # customer_credit is 100000 (from setUp) — but should NOT prevent triggering

        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertTrue(
            policy.is_triggered(),
            "ProjectCredit should take precedence over CustomerCredit. "
            "Even with large CustomerCredit, exhausted ProjectCredit "
            "should allow triggering.",
        )

    def test_not_triggered_when_cost_below_limit(self):
        """Cost below limit should not trigger regardless of credit."""
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=50)

        self.assertFalse(
            policy.is_triggered(),
            "Policy should not trigger when cost is below limit.",
        )

    def test_falls_back_to_customer_credit_when_no_project_credit(self):
        """When no ProjectCredit exists, CustomerCredit should prevent triggering."""
        # No ProjectCredit — only CustomerCredit (100000 from setUp)
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertFalse(
            policy.is_triggered(),
            "Policy should fall back to CustomerCredit when no ProjectCredit exists.",
        )

    def test_triggered_when_no_project_credit_and_customer_credit_below_limit(self):
        """No ProjectCredit and CustomerCredit below limit should trigger."""
        self.customer_credit.value = 50
        self.customer_credit.save()

        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertTrue(
            policy.is_triggered(),
            "Policy should trigger when no ProjectCredit and "
            "CustomerCredit is below limit_cost.",
        )


@override_settings(task_always_eager=True)
@freeze_time("2026-04-01")
class CustomerCreditPreventsTriggering(test.APITestCase):
    """CustomerEstimatedCostPolicy should not fire when the customer
    has sufficient remaining credit."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            month=4,
            year=2026,
            tax_percent=0,
        )

    def test_not_triggered_when_customer_credit_covers_cost(self):
        invoices_factories.CustomerCreditFactory(
            customer=self.customer,
            value=50000,
        )
        policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=100,
            actions="notify_organization_owners",
            period=CustomerEstimatedCostPolicy.Periods.TOTAL,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.fixture.project,
            unit_price=200,
            quantity=1,
        )

        self.assertFalse(
            policy.is_triggered(),
            "Customer policy should not trigger when credit exceeds limit_cost.",
        )

    def test_triggered_when_no_customer_credit(self):
        policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=100,
            actions="notify_organization_owners",
            period=CustomerEstimatedCostPolicy.Periods.TOTAL,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.fixture.project,
            unit_price=200,
            quantity=1,
        )

        self.assertTrue(
            policy.is_triggered(),
            "Customer policy should trigger when no credit exists.",
        )
