"""
Tests that cost policies account for available credit.

A credit-aware policy compares the cost the credit does not pay with its limit.
Credit that pays the cost keeps the policy from firing; how much credit is left
is not compared with the limit (#784), so cost the credit cannot pay fires the
policy even while credit remains.
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
    """ProjectEstimatedCostPolicy does not fire while credit pays its cost."""

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

    def _create_item(self, unit_price, creditable=True):
        # MonthlyCompensation pays only items with a resource, so an item
        # without one is cost no credit covers.
        return invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            resource=self.fixture.resource if creditable else None,
            unit_price=unit_price,
            quantity=1,
        )

    def test_not_triggered_when_project_credit_pays_the_cost(self):
        invoices_factories.ProjectCreditFactory(project=self.project, value=33625)
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertFalse(policy.is_triggered())

    def test_triggered_when_no_credit_at_all(self):
        self.customer_credit.delete()
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertTrue(policy.is_triggered())

    def test_triggered_when_project_credit_exhausted(self):
        invoices_factories.ProjectCreditFactory(project=self.project, value=0)
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertTrue(policy.is_triggered())

    def test_triggered_when_project_credit_pays_too_little(self):
        # 50 paid by credit leaves 150, over the limit of 100.
        invoices_factories.ProjectCreditFactory(project=self.project, value=50)
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertTrue(policy.is_triggered())

    def test_project_allocation_caps_what_the_organization_credit_pays(self):
        # The organization credit is large, but the project may draw only its
        # 50 allocation of it.
        invoices_factories.ProjectCreditFactory(project=self.project, value=50)
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertTrue(policy.is_triggered())

    def test_not_triggered_when_cost_below_limit(self):
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=50)

        self.assertFalse(policy.is_triggered())

    def test_falls_back_to_customer_credit_when_no_project_credit(self):
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertFalse(policy.is_triggered())

    def test_triggered_when_customer_credit_pays_too_little(self):
        self.customer_credit.value = 50
        self.customer_credit.save()

        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200)

        self.assertTrue(policy.is_triggered())

    def test_cost_no_credit_covers_fires_although_credit_is_left(self):
        # 100000 of credit is left, but none of it can pay an item without a
        # resource. The remaining balance used to hold the policy back.
        policy = self._create_policy(limit_cost=100)
        self._create_item(unit_price=200, creditable=False)

        self.assertTrue(policy.is_triggered())


@override_settings(task_always_eager=True)
@freeze_time("2026-04-01")
class CustomerCreditPreventsTriggering(test.APITestCase):
    """CustomerEstimatedCostPolicy does not fire while credit pays its cost."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            month=4,
            year=2026,
            tax_percent=0,
        )
        self.policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer,
            limit_cost=100,
            actions="notify_organization_owners",
            period=CustomerEstimatedCostPolicy.Periods.TOTAL,
        )

    def _create_item(self, creditable=True):
        invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.fixture.project,
            resource=self.fixture.resource if creditable else None,
            unit_price=200,
            quantity=1,
        )

    def test_not_triggered_when_customer_credit_pays_the_cost(self):
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=50000)
        self._create_item()

        self.assertFalse(self.policy.is_triggered())

    def test_triggered_when_no_customer_credit(self):
        self._create_item()

        self.assertTrue(self.policy.is_triggered())

    def test_cost_no_credit_covers_fires_although_credit_is_left(self):
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=50000)
        self._create_item(creditable=False)

        self.assertTrue(self.policy.is_triggered())
