"""Pin the query cost of refreshing price estimates.

``update_estimates_for_scopes`` is called for every affected project whenever
credit compensations are applied or cleared, and for every project of a
customer when an invoice is created. It receives the scope objects, so the
only queries it needs per scope are: fetch the estimate, aggregate the month's
invoice items, store the total. Reading ``scope`` or ``content_type`` off a
freshly fetched estimate would add two more per scope for data already in
memory.
"""

from django.db import DEFAULT_DB_ALIAS, connections
from django.test.utils import CaptureQueriesContext
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.billing import handlers
from waldur_mastermind.common.enums import Units
from waldur_mastermind.invoices.tests import factories as invoice_factories

QUERIES_PER_SCOPE = 3


class EstimateRefreshQueryCountTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        invoice = invoice_factories.InvoiceFactory(customer=self.customer)
        invoice_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.project,
            unit=Units.QUANTITY,
            unit_price=10,
            quantity=5,
        )
        # Estimates already exist — the post_save handler creates one for every
        # project and customer — so get_or_create takes its `get` branch, which
        # is the path production hits.
        handlers.update_estimates_for_scopes([self.customer, self.project])

    def refresh(self, scopes):
        with CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as ctx:
            handlers.update_estimates_for_scopes(scopes)
        return ctx.captured_queries

    def test_query_count_per_scope_is_fixed(self):
        for scopes in ([self.customer], [self.customer, self.project]):
            with self.subTest(scopes=len(scopes)):
                queries = self.refresh(scopes)
                self.assertEqual(len(queries), QUERIES_PER_SCOPE * len(scopes))

    def test_scope_and_content_type_are_not_refetched(self):
        # The semantic version of the count above: whatever else changes, the
        # refresh must not go back for objects it was handed. Matched on
        # `FROM "table"` rather than a bare name, because the project-scoped
        # aggregate legitimately JOINs structure_project to filter on its uuid.
        queries = self.refresh([self.customer, self.project])
        for table in ("django_content_type", "structure_project", "structure_customer"):
            self.assertFalse(
                [q for q in queries if f'FROM "{table}"' in q["sql"]],
                f"refresh re-fetched {table}",
            )

    def test_query_count_does_not_grow_with_scope_count(self):
        extra_projects = [
            structure_factories.ProjectFactory(customer=self.customer) for _ in range(3)
        ]
        handlers.update_estimates_for_scopes(extra_projects)

        one = len(self.refresh([self.project]))
        many = len(self.refresh([self.project, *extra_projects]))
        self.assertEqual(many - one, QUERIES_PER_SCOPE * len(extra_projects))
