"""
Regression test for the fix to Critical security finding #5.

Before the fix, `InvoiceItemViewSet.costs` filtered InvoiceItem rows by the
`project_uuid` query parameter without applying the structure-aware
`filter_queryset_for_user` scope, so any authenticated user could read the
billing aggregates and per-resource current-month breakdown for any project
whose UUID they could name.

This test plants a victim invoice in customer B and verifies that an
attacker holding only an unrelated user account cannot read it via the
`/api/invoice-items/costs/` action.
"""

from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories as invoice_factories


class InvoiceCostsIdorFixedTest(APITestCase):
    def setUp(self):
        self.customer_a = structure_factories.CustomerFactory()
        self.customer_b = structure_factories.CustomerFactory()

        self.project_b = structure_factories.ProjectFactory(customer=self.customer_b)

        # Attacker has zero access to customer B.
        self.attacker = structure_factories.UserFactory()

        # Victim billing line that should never reach an outsider.
        invoice_b = invoice_factories.InvoiceFactory(customer=self.customer_b)
        invoice_factories.InvoiceItemFactory(
            name="VICTIM-CONFIDENTIAL-LINE",
            project=self.project_b,
            invoice=invoice_b,
            unit_price=42,
            quantity=7,
        )

    def test_attacker_does_not_see_victim_costs(self):
        self.client.force_authenticate(self.attacker)
        response = self.client.get(
            "/api/invoice-items/costs/",
            {"project_uuid": self.project_b.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        body = response.json()
        items = body if isinstance(body, list) else body.get("results", body)

        # The vulnerable code path returned an entry whose `price` was
        # 42 * 7 = 294. With the fix, the filtered queryset is empty and
        # the action returns no rows for the unrelated attacker.
        self.assertEqual(
            items,
            [],
            f"Costs leak still present — attacker received {items!r}.",
        )
