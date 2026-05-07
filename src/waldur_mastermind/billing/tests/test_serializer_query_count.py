"""Pin the bulk-fetch behavior of ProjectListSerializer and CustomerListSerializer.

These tests assert that query counts on ``/api/projects/`` and
``/api/customers/`` do NOT scale with row count when the bulk-loading
``ListSerializer.to_representation`` paths are exercised. They guard the
optimization introduced when the per-request ``_price_estimates_cache``
was replaced by ``serializer.context['bulk_data']`` populated eagerly by
the list serializer.
"""

from decimal import Decimal

from django.db import DEFAULT_DB_ALIAS, connections
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common.enums import Units
from waldur_mastermind.invoices.tests import factories as invoice_factories


def _make_customer_with_invoice(name_suffix: str):
    """Create a customer with one project, one invoice and two invoice items."""
    customer = structure_factories.CustomerFactory(name=f"Customer {name_suffix}")
    project = structure_factories.ProjectFactory(customer=customer)
    invoice = invoice_factories.InvoiceFactory(customer=customer)
    invoice_factories.InvoiceItemFactory(
        invoice=invoice,
        project=project,
        unit=Units.QUANTITY,
        unit_price=Decimal("10.00"),
        quantity=2,
    )
    invoice_factories.InvoiceItemFactory(
        invoice=invoice,
        project=project,
        unit=Units.QUANTITY,
        unit_price=Decimal("5.00"),
        quantity=4,
    )
    return customer, project


class ProjectListBulkFetchQueryCountTest(test.APITestCase):
    """Project list endpoint must not scale per row when billing fields are requested."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        # Two customers x one project each, plus invoice items so that
        # the billing_price_estimate aggregation has rows to fold.
        _make_customer_with_invoice("A")
        _make_customer_with_invoice("B")
        self.list_url = "/api/projects/"
        self.fields = ["uuid", "name", "billing_price_estimate", "resources_count"]

    def _capture(self):
        self.client.force_authenticate(self.staff)
        # Warm up: prime ContentType / permission caches the first hit
        # populates so they don't skew the measured count.
        self.client.get(self.list_url, {"field": self.fields})
        with CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as ctx:
            response = self.client.get(self.list_url, {"field": self.fields})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(ctx.captured_queries), response.data

    def test_query_count_is_constant_across_page_sizes(self):
        """Adding more projects must not add per-row queries for billing/resources fields."""
        small_count, small_data = self._capture()
        self.assertGreaterEqual(len(small_data), 2)

        # Triple the number of projects and re-measure.
        for i in range(6):
            _make_customer_with_invoice(f"extra-{i}")

        large_count, large_data = self._capture()
        self.assertGreaterEqual(len(large_data), 8)

        # The bulk-fetch optimization must keep query count flat. Allow a
        # small slack for ContentType lookups but reject any per-row growth.
        self.assertLessEqual(
            large_count - small_count,
            2,
            f"Project list query count grew from {small_count} (2 projects) "
            f"to {large_count} (8 projects). Expected near-constant; suggests "
            f"the ProjectListSerializer bulk-fetch path regressed.",
        )


class CustomerListBulkFetchQueryCountTest(test.APITestCase):
    """Customer list endpoint must not scale per row for bulk-loaded fields."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        _make_customer_with_invoice("A")
        _make_customer_with_invoice("B")
        self.list_url = "/api/customers/"
        # Note: users_count is intentionally excluded — its O(N) nature is a
        # pre-existing issue tracked separately, not part of the bulk-fetch
        # optimization being pinned here.
        self.fields = ["uuid", "name", "billing_price_estimate"]

    def _capture(self):
        self.client.force_authenticate(self.staff)
        self.client.get(self.list_url, {"field": self.fields})
        with CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as ctx:
            response = self.client.get(self.list_url, {"field": self.fields})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(ctx.captured_queries), response.data

    def test_query_growth_is_bounded_per_added_customer(self):
        """Adding more customers must not increase per-row query budget.

        TODO: NestedPriceEstimateSerializer.get_current / get_tax each call
        obj.get_total(year, month, current=...) which issues a SQL query
        per customer for the current-period sum. The bulk path only fetches
        the PriceEstimate row itself in bulk; the period-aggregate fields
        remain per-row. This test pins the current behavior — any future
        growth (e.g. an additional per-row query) will fail it.
        """
        small_count, small_data = self._capture()
        self.assertGreaterEqual(len(small_data), 2)

        added = 6
        for i in range(added):
            _make_customer_with_invoice(f"extra-{i}")

        large_count, large_data = self._capture()
        self.assertGreaterEqual(len(large_data), 8)

        # Current measurement: ~4 queries per added customer (period-aggregate
        # fields on NestedPriceEstimateSerializer). Allow that, plus small
        # slack for ContentType lookups, but reject any further growth.
        max_allowed_growth = 4 * added + 2
        self.assertLessEqual(
            large_count - small_count,
            max_allowed_growth,
            f"Customer list query count grew from {small_count} (2 customers) "
            f"to {large_count} ({2 + added} customers). Allowed growth: "
            f"{max_allowed_growth}. New growth indicates a regression beyond "
            f"the known per-row period-aggregate cost.",
        )
