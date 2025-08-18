"""Test for customer list performance with call_manager role."""

import time

from django.test import TestCase
from django.test.utils import override_settings
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.billing.tests import factories as billing_factories
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.proposal.tests.fixtures import ProposalFixture


@override_settings(DEBUG=True)
class CallManagerCustomerListPerformanceTest(TestCase):
    """Test performance of customer list for call_manager users."""

    def setUp(self):
        """Set up test data with multiple customers and call_manager."""
        self.fixture = ProposalFixture()
        self.call_manager = self.fixture.call_manager
        self.call = self.fixture.call

        # Create multiple customers with credits and estimates
        self.customers = []
        for i in range(10):
            customer = structure_factories.CustomerFactory(name=f"Customer {i}")
            self.customers.append(customer)

            # Add customer credit
            invoice_factories.CustomerCreditFactory(
                customer=customer, value=1000 + i * 100
            )

            # Add price estimate
            billing_factories.PriceEstimateFactory(scope=customer, total=100 + i * 10)

            # Add projects to customer
            for j in range(3):
                structure_factories.ProjectFactory(
                    customer=customer, name=f"Project {i}-{j}"
                )

        self.client = test.APIClient()
        self.client.force_authenticate(user=self.call_manager)

    def test_customer_list_query_count_with_fields(self):
        """Test query count when requesting customer list with specific fields."""
        from django.db import connection, reset_queries

        # Reset queries
        reset_queries()

        # Make the same request as in the issue
        url = "/api/customers/"
        params = {
            "page": 1,
            "page_size": 10,
            "user_uuid": self.call_manager.uuid.hex,
            "field": [
                "uuid",
                "name",
                "abbreviation",
                "email",
                "projects_count",
                "created",
                "image",
                "customer_credit",
                "billing_price_estimate",
                "organization_groups",
                "url",
            ],
        }

        start_time = time.time()
        response = self.client.get(url, params)
        end_time = time.time()

        # Check response is successful
        self.assertEqual(response.status_code, 200)

        # Get query count
        query_count = len(connection.queries)

        # Print diagnostic information
        print(f"\nQuery count: {query_count}")
        print(f"Response time: {end_time - start_time:.2f} seconds")
        # Handle both paginated (dict with 'results') and non-paginated (list) responses
        if isinstance(response.data, dict) and "results" in response.data:
            customer_count = len(response.data["results"])
        else:
            customer_count = len(response.data)
        print(f"Number of customers returned: {customer_count}")

        # Print the most expensive queries
        print("\nQueries related to customer_credit and billing_price_estimate:")
        for query in connection.queries:
            if (
                "customercredit" in query["sql"].lower()
                or "priceestimate" in query["sql"].lower()
            ):
                print(f"  - {query['sql'][:100]}... (time: {query['time']})")

        # The query count should be reasonable, not O(n) for each customer
        # With proper optimization, it should be less than 30 queries total (including expensive fields)
        self.assertLess(query_count, 30, f"Too many queries: {query_count}")

    def test_customer_list_without_expensive_fields(self):
        """Test query count when requesting customer list without expensive fields."""
        from django.db import connection, reset_queries

        # Reset queries
        reset_queries()

        # Make request without expensive fields
        url = "/api/customers/"
        params = {
            "page": 1,
            "page_size": 10,
            "user_uuid": self.call_manager.uuid.hex,
            "field": ["uuid", "name", "abbreviation", "created", "url"],
        }

        start_time = time.time()
        response = self.client.get(url, params)
        end_time = time.time()

        # Check response is successful
        self.assertEqual(response.status_code, 200)

        # Get query count
        query_count = len(connection.queries)

        # Print diagnostic information
        print(f"\nQuery count without expensive fields: {query_count}")
        print(f"Response time: {end_time - start_time:.2f} seconds")

        # Print all queries to diagnose performance issues
        print("\nAll SQL queries executed:")
        for i, query in enumerate(connection.queries, 1):
            sql = (
                query["sql"][:200] + "..." if len(query["sql"]) > 200 else query["sql"]
            )
            print(f"{i:2d}. {sql} (time: {query['time']})")

        # Without expensive fields, query count should be reasonable
        self.assertLess(
            query_count,
            25,
            f"Too many queries even without expensive fields: {query_count}",
        )
