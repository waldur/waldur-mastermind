"""
Performance tests for CustomerViewSet permission filtering.

This module tests SQL query counts for different user permission scenarios
to identify and measure performance bottlenecks in the permission system.
"""

import logging

from django.db import DEFAULT_DB_ALIAS, connections
from django.test.utils import CaptureQueriesContext
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories

# Note: Call-related performance tests are now in the proposal app
# See: waldur_mastermind.proposal.tests.test_call_permission_performance


# Suppress SQL logging during tests for cleaner output
logging.getLogger("django.db.backends").setLevel(logging.WARNING)


class CustomerPermissionPerformanceTest(test.APITransactionTestCase):
    """
    Test SQL query performance for different permission scenarios in CustomerViewSet.

    This test measures the number of database queries required for customer list operations
    under different user permission configurations to identify optimization opportunities.
    """

    def setUp(self):
        """Set up test fixtures with various permission scenarios."""
        # Enable required permissions
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_PROJECTS)
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_PROJECTS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_PROJECTS)
        ProjectRole.MEMBER.add_permission(PermissionEnum.LIST_PROJECTS)

        # Create base test data
        self.staff_user = factories.UserFactory(is_staff=True)
        self.regular_user = factories.UserFactory()

        # Create multiple customers with projects to simulate real-world scenario
        self.num_customers = 10
        self.num_projects_per_customer = 3

        self.customers = []
        self.projects = []

        for i in range(self.num_customers):
            customer = factories.CustomerFactory(name=f"Customer {i}")
            self.customers.append(customer)

            # Create projects for this customer
            customer_projects = []
            for j in range(self.num_projects_per_customer):
                project = factories.ProjectFactory(
                    customer=customer, name=f"Project {i}-{j}"
                )
                customer_projects.append(project)
                self.projects.append(project)

        # Note: Call-related user setup is now handled in proposal app tests

    def _capture_queries(self, user, params=None):
        """
        Capture SQL queries for a customer list request.

        Args:
            user: User to authenticate as
            params: Optional query parameters

        Returns:
            tuple: (query_count, results_list, queries_list)
        """
        self.client.force_authenticate(user=user)

        url = factories.CustomerFactory.get_list_url()
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())

        # Warm up caches with a single request
        self.client.get(url)

        # Capture queries for the actual request
        with CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as context:
            response = self.client.get(url)

        # Handle both paginated and non-paginated responses
        if isinstance(response.data, dict) and "results" in response.data:
            results = response.data["results"]
        elif isinstance(response.data, list):
            results = response.data
        else:
            results = []

        return len(context.captured_queries), results, context.captured_queries

    def _setup_customer_owner_permissions(self, user):
        """Set up user as owner of some customers."""
        # Make user owner of first 3 customers
        for customer in self.customers[:3]:
            customer.add_user(user, CustomerRole.OWNER)

    def _setup_project_role_permissions(self, user):
        """Set up user with project roles across multiple customers."""
        # Give user roles in projects across different customers
        for i, project in enumerate(
            self.projects[:6]
        ):  # First 6 projects (2 customers worth)
            if i % 3 == 0:
                project.add_user(user, ProjectRole.ADMIN)
            elif i % 3 == 1:
                project.add_user(user, ProjectRole.MANAGER)
            else:
                project.add_user(user, ProjectRole.MEMBER)

    def _setup_mixed_permissions(self, user):
        """Set up user with both customer and project roles."""
        # Customer owner of 2 customers
        for customer in self.customers[:2]:
            customer.add_user(user, CustomerRole.OWNER)

        # Project roles in other customers
        for project in self.projects[6:12]:  # Projects from customers 2-3
            project.add_user(user, ProjectRole.ADMIN)

    def test_staff_user_query_count(self):
        """Test query count for staff user (baseline - should be minimal)."""
        query_count, results, queries = self._capture_queries(self.staff_user)

        print("\n=== STAFF USER PERFORMANCE ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print(f"Expected customers: {self.num_customers}")

        # Staff should see all customers
        self.assertEqual(len(results), self.num_customers)

        # Staff should have fewer queries than regular users (bypasses permission filtering)
        # Note: This is our baseline - even staff users may have many queries due to serializer complexity
        self.assertGreater(query_count, 0, "Should have at least some queries")

        # Print detailed query information for analysis
        self._print_query_analysis(queries, "Staff User")

    def test_customer_owner_query_count(self):
        """Test query count for user with customer owner permissions."""
        self._setup_customer_owner_permissions(self.regular_user)

        query_count, results, queries = self._capture_queries(self.regular_user)

        print("\n=== CUSTOMER OWNER PERFORMANCE ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print("Expected customers: 3 (owner of 3 customers)")

        # User should see only customers they own
        self.assertEqual(len(results), 3)

        # Print detailed query information for analysis
        self._print_query_analysis(queries, "Customer Owner")

    def test_project_role_query_count(self):
        """Test query count for user with project-level permissions."""
        self._setup_project_role_permissions(self.regular_user)

        query_count, results, queries = self._capture_queries(self.regular_user)

        print("\n=== PROJECT ROLE PERFORMANCE ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print("Expected customers: up to 2 (projects in first 2 customers)")

        # User should see customers that have projects they have roles in
        # Note: Actual number may vary based on how projects are distributed
        self.assertGreaterEqual(len(results), 1)

        # Print detailed query information for analysis
        self._print_query_analysis(queries, "Project Role")

    def test_mixed_permissions_query_count(self):
        """Test query count for user with both customer and project permissions."""
        self._setup_mixed_permissions(self.regular_user)

        query_count, results, queries = self._capture_queries(self.regular_user)

        print("\n=== MIXED PERMISSIONS PERFORMANCE ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print("Expected customers: 3-4 (owner of 2 + project roles in others)")

        # User should see customers they own + customers with project roles
        self.assertGreaterEqual(len(results), 2)

        # Print detailed query information for analysis
        self._print_query_analysis(queries, "Mixed Permissions")

    def test_no_permissions_query_count(self):
        """Test query count for user with no permissions."""
        query_count, results, queries = self._capture_queries(self.regular_user)

        print("\n=== NO PERMISSIONS PERFORMANCE ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print("Expected customers: 0")

        # User should see no customers
        self.assertEqual(len(results), 0)

        # Even with no results, permission checks still run
        self._print_query_analysis(queries, "No Permissions")

    def test_scaling_performance(self):
        """Test how query count scales with more data."""
        # Create additional customers and projects
        additional_customers = []
        for i in range(self.num_customers, self.num_customers + 20):
            customer = factories.CustomerFactory(name=f"Additional Customer {i}")
            additional_customers.append(customer)

            # Create projects
            for j in range(self.num_projects_per_customer):
                factories.ProjectFactory(
                    customer=customer, name=f"Additional Project {i}-{j}"
                )

        # Give user permissions to some additional customers
        for customer in additional_customers[:5]:
            customer.add_user(self.regular_user, CustomerRole.OWNER)

        query_count, results, queries = self._capture_queries(self.regular_user)

        print("\n=== SCALING PERFORMANCE (30 total customers) ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print("Expected customers: at least 5")

        self.assertGreaterEqual(len(results), 5)

        # Check if query count scales linearly (bad) or stays constant (good)
        self._print_query_analysis(queries, "Scaling Test")

    def test_performance_comparison(self):
        """Run all scenarios and compare performance."""
        print(f"\n{'=' * 60}")
        print("CUSTOMER PERMISSION PERFORMANCE COMPARISON")
        print(f"{'=' * 60}")
        print(
            f"Test scenario: {self.num_customers} customers, "
            f"{self.num_projects_per_customer} projects each"
        )

        # Run all tests and collect results by capturing queries directly
        staff_queries, _, _ = self._capture_queries(self.staff_user)
        no_perm_queries, _, _ = self._capture_queries(self.regular_user)

        # Set up customer owner permissions
        self._setup_customer_owner_permissions(self.regular_user)
        customer_owner_queries, _, _ = self._capture_queries(self.regular_user)

        # Reset user and set up project role permissions
        self.regular_user = factories.UserFactory()
        self._setup_project_role_permissions(self.regular_user)
        project_role_queries, _, _ = self._capture_queries(self.regular_user)

        # Reset user and set up mixed permissions
        self.regular_user = factories.UserFactory()
        self._setup_mixed_permissions(self.regular_user)
        mixed_perm_queries, _, _ = self._capture_queries(self.regular_user)

        # Reset user and test scaling
        self.regular_user = factories.UserFactory()
        # Create additional customers and projects for scaling test
        additional_customers = []
        for i in range(self.num_customers, self.num_customers + 20):
            customer = factories.CustomerFactory(name=f"Additional Customer {i}")
            additional_customers.append(customer)

            # Create projects
            for j in range(self.num_projects_per_customer):
                factories.ProjectFactory(
                    customer=customer, name=f"Additional Project {i}-{j}"
                )

        # Give user permissions to some additional customers
        for customer in additional_customers[:5]:
            customer.add_user(self.regular_user, CustomerRole.OWNER)

        scaling_queries, _, _ = self._capture_queries(self.regular_user)

        # Note: Call-related tests are now in waldur_mastermind.proposal.tests.test_call_permission_performance

        # Summary comparison
        print(f"\n{'=' * 60}")
        print("QUERY COUNT SUMMARY")
        print(f"{'=' * 60}")
        print(f"Staff user (baseline):      {staff_queries:3d} queries")
        print(f"No permissions:             {no_perm_queries:3d} queries")
        print(f"Customer owner:             {customer_owner_queries:3d} queries")
        print(f"Project roles:              {project_role_queries:3d} queries")
        print(f"Mixed permissions:          {mixed_perm_queries:3d} queries")
        print(f"Scaling test (30 customers): {scaling_queries:3d} queries")

        # Calculate performance ratios
        print(f"\n{'=' * 60}")
        print("PERFORMANCE IMPACT vs STAFF")
        print(f"{'=' * 60}")
        print(
            f"No permissions:             {no_perm_queries / staff_queries:.1f}x slower"
        )
        print(
            f"Customer owner:             {customer_owner_queries / staff_queries:.1f}x slower"
        )
        print(
            f"Project roles:              {project_role_queries / staff_queries:.1f}x slower"
        )
        print(
            f"Mixed permissions:          {mixed_perm_queries / staff_queries:.1f}x slower"
        )
        print(
            f"Scaling test:               {scaling_queries / staff_queries:.1f}x slower"
        )

    def _print_query_analysis(self, queries, scenario_name):
        """Print detailed analysis of SQL queries."""
        print(f"\n--- {scenario_name} Query Analysis ---")

        # Group queries by type
        query_types = {}
        repeated_patterns = {}

        for i, query in enumerate(queries):
            sql = query["sql"]

            # Categorize query types
            if "SELECT" in sql and "user_role" in sql.lower():
                query_type = "Permission Check"
            elif "SELECT" in sql and "customer" in sql.lower():
                query_type = "Customer Query"
            elif "SELECT" in sql and "project" in sql.lower():
                query_type = "Project Query"
            elif "SELECT" in sql and "contenttypes" in sql.lower():
                query_type = "ContentType Query"
            elif "COUNT" in sql:
                query_type = "Count Query"
            elif "INSERT" in sql or "UPDATE" in sql or "DELETE" in sql:
                query_type = "Write Query"
            else:
                query_type = "Other"

            if query_type not in query_types:
                query_types[query_type] = []
            query_types[query_type].append(
                {"index": i, "sql": sql, "time": query["time"]}
            )

            # Look for N+1 patterns - create simplified signature
            import re

            signature = re.sub(r"\b\d+\b", "N", sql)  # Replace numbers with N
            signature = re.sub(r"'[^']*'", "'X'", signature)  # Replace strings with X
            signature = re.sub(r'"[^"]*"', '"X"', signature)  # Replace quoted strings

            if signature not in repeated_patterns:
                repeated_patterns[signature] = []
            repeated_patterns[signature].append(
                {"index": i, "original": sql, "time": query["time"]}
            )

        # Print summary by type
        for query_type, type_queries in query_types.items():
            total_time = sum(float(q["time"]) for q in type_queries)
            print(
                f"  {query_type}: {len(type_queries)} queries ({total_time:.3f}s total)"
            )

        # Find potential N+1 patterns (queries that repeat more than once)
        n_plus_one_patterns = {
            sig: queries
            for sig, queries in repeated_patterns.items()
            if len(queries) > 1
        }

        if n_plus_one_patterns:
            print("\n🔍 Potential N+1 Patterns Detected:")
            sorted_patterns = sorted(
                n_plus_one_patterns.items(),
                key=lambda x: len(x[1]) * sum(float(q["time"]) for q in x[1]),
                reverse=True,
            )

            for signature, occurrences in sorted_patterns[:3]:  # Show top 3 patterns
                count = len(occurrences)
                total_time = sum(float(q["time"]) for q in occurrences)
                print(f"  🚨 {count} identical queries ({total_time:.3f}s total):")

                # Show original query (truncated)
                example = occurrences[0]["original"]
                if len(example) > 150:
                    example = example[:150] + "..."
                print(f"     {example}")
                print(f"     Query indexes: {[q['index'] for q in occurrences]}")

        # Print sample queries for the most problematic types
        print("\n📋 Sample queries from main categories:")
        priority_types = ["Customer Query", "Permission Check", "Project Query"]
        for query_type in priority_types:
            if query_type in query_types and query_types[query_type]:
                print(f"\n  {query_type} example:")
                sql = query_types[query_type][0]["sql"]
                if len(sql) > 200:
                    sql = sql[:200] + "..."
                print(f"    {sql}")
