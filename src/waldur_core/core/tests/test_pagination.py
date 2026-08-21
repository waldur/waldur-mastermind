from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import test

from waldur_core.core.pagination import ensure_total_ordering
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories


class EnsureTotalOrderingTest(SimpleTestCase):
    """
    Meta.ordering ends on the primary key, but an ordering filter replaces the
    ordering instead of extending it, so a sorted page is not a total order
    unless the tie-break is restored where the slicing happens.
    """

    def ordering_of(self, queryset):
        return list(ensure_total_ordering(queryset).query.order_by)

    def test_tiebreaker_is_appended_to_an_ordering_filter_result(self):
        # This is what ?o=name leaves behind.
        self.assertEqual(
            self.ordering_of(Customer.objects.order_by("name")), ["name", "pk"]
        )

    def test_tiebreaker_is_appended_to_an_unordered_queryset(self):
        self.assertEqual(self.ordering_of(Customer.objects.order_by()), ["pk"])

    def test_tiebreaker_is_appended_to_a_distinct_model_queryset(self):
        # The primary key is already selected, so DISTINCT is unaffected.
        queryset = Customer.objects.order_by("name").distinct()
        self.assertEqual(self.ordering_of(queryset), ["name", "pk"])

    def test_model_ordering_ending_on_the_primary_key_is_left_alone(self):
        queryset = Customer.objects.all()
        self.assertIs(ensure_total_ordering(queryset), queryset)

    def test_ordering_ending_on_a_unique_column_is_left_alone(self):
        queryset = Customer.objects.order_by("uuid")
        self.assertIs(ensure_total_ordering(queryset), queryset)

    def test_values_queryset_is_left_alone(self):
        # Appending a column here would change what DISTINCT deduplicates on.
        queryset = Customer.objects.values_list("name", flat=True).distinct()
        self.assertIs(ensure_total_ordering(queryset), queryset)

    def test_distinct_on_queryset_is_left_alone(self):
        queryset = Customer.objects.order_by("name").distinct("name")
        self.assertIs(ensure_total_ordering(queryset), queryset)

    def test_combined_queryset_is_left_alone(self):
        queryset = Customer.objects.filter(name="alpha").union(
            Customer.objects.filter(name="beta")
        )
        self.assertIs(ensure_total_ordering(queryset), queryset)

    def test_sliced_queryset_is_left_alone(self):
        queryset = Customer.objects.order_by("name")[:10]
        self.assertIs(ensure_total_ordering(queryset), queryset)

    def test_plain_list_is_left_alone(self):
        rows = [{"name": "alpha"}]
        self.assertIs(ensure_total_ordering(rows), rows)


class PaginatedListOrderingTest(test.APITestCase):
    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    def test_sorted_page_is_ordered_by_the_primary_key_as_well(self):
        # Three rows sharing the sort key: without a tie-break the database is
        # free to hand out one of them on both pages and never show another.
        for _ in range(3):
            factories.ProjectFactory(name="Shared name")

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                factories.ProjectFactory.get_list_url(),
                {"o": "name", "page_size": 1},
            )

        self.assertEqual(response.status_code, 200)
        page_queries = [
            query["sql"]
            for query in queries.captured_queries
            if "LIMIT 1" in query["sql"]
        ]
        self.assertTrue(page_queries)
        for sql in page_queries:
            self.assertIn('"structure_project"."id"', sql.split("ORDER BY")[-1])
