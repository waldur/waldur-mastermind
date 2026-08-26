"""Billing cost a demo credit does not cover.

A credit covers the offerings named on the organization balance and no others,
so a project can carry real cost no compensation will ever touch. No preset
could express that, which is why the dashboard defect it causes — pacing the
month against the whole project invoice rather than the covered part — had no
demo data to be seen in.

`credit_history` patterns now take an `uncovered` block. These tests hold the
split where it matters: the two groups are billed at their own fractions, and a
project that declares no such block is billed exactly as it was before the key
existed, so the other thirteen scenarios are untouched.
"""

import decimal

from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace.demo_presets.credit_history import (
    _bill_month_for_project,
)
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories


class BillMonthForProjectTest(TestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.invoice = invoice_factories.InvoiceFactory(customer=self.customer)
        self.credit = invoice_factories.ProjectCreditFactory.build(
            project=self.project, expected_consumption=decimal.Decimal("10000")
        )

        self.covered_offering = factories.OfferingFactory()
        self.uncovered_offering = factories.OfferingFactory()
        self.covered = factories.ResourceFactory(
            offering=self.covered_offering,
            project=self.project,
            state=ResourceStates.OK,
        )
        self.uncovered = factories.ResourceFactory(
            offering=self.uncovered_offering,
            project=self.project,
            state=ResourceStates.OK,
        )

    def prices(self):
        return {
            item.resource_id: item.price
            for item in invoice_models.InvoiceItem.objects.filter(invoice=self.invoice)
        }

    def test_each_group_is_billed_at_its_own_fraction(self):
        # The shape of the reported defect: the credit funds the plan, and the
        # project carries five times that again outside it.
        _bill_month_for_project(
            self.invoice,
            self.project,
            self.credit,
            decimal.Decimal("1.0"),
            uncovered_fraction=decimal.Decimal("5.0"),
            covered_offering_ids={self.covered_offering.id},
        )
        prices = self.prices()
        self.assertEqual(prices[self.covered.id], decimal.Decimal("10000.00"))
        self.assertEqual(prices[self.uncovered.id], decimal.Decimal("50000.00"))

    def test_without_the_key_the_project_is_billed_as_one(self):
        # The invariant that keeps the other thirteen scenarios unchanged: no
        # `uncovered` fraction means no partition, and the month's target is
        # spread evenly across every resource as it always was.
        _bill_month_for_project(
            self.invoice, self.project, self.credit, decimal.Decimal("1.0")
        )
        prices = self.prices()
        self.assertEqual(prices[self.covered.id], decimal.Decimal("5000.00"))
        self.assertEqual(prices[self.uncovered.id], decimal.Decimal("5000.00"))

    def test_a_credit_covering_nothing_bills_every_resource_as_uncovered(self):
        # An empty covered set is the caller's business, not this function's:
        # `_covered_offering_ids` returns empty for an unrestricted credit, and
        # a preset that pairs that with an `uncovered` block is describing a
        # project where nothing is covered.
        _bill_month_for_project(
            self.invoice,
            self.project,
            self.credit,
            decimal.Decimal("1.0"),
            uncovered_fraction=decimal.Decimal("2.0"),
            covered_offering_ids=set(),
        )
        prices = self.prices()
        self.assertEqual(prices[self.covered.id], decimal.Decimal("10000.00"))
        self.assertEqual(prices[self.uncovered.id], decimal.Decimal("10000.00"))

    def test_a_group_with_no_resources_bills_nothing(self):
        self.uncovered.delete()
        _bill_month_for_project(
            self.invoice,
            self.project,
            self.credit,
            decimal.Decimal("1.0"),
            uncovered_fraction=decimal.Decimal("5.0"),
            covered_offering_ids={self.covered_offering.id},
        )
        prices = self.prices()
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[self.covered.id], decimal.Decimal("10000.00"))
