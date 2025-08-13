"""
Performance tests for proposal app permission filtering.

This module tests SQL query counts for CALL_MANAGER and CALL_ORGANIZER user permission
scenarios to identify and measure performance bottlenecks in the call-related permission system.
Based on the CustomerPermissionPerformanceTest from waldur_core.structure.
"""

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import DEFAULT_DB_ALIAS, connections
from django.test.utils import CaptureQueriesContext
from rest_framework import test

from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.models import Role
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories

# Suppress SQL logging during tests for cleaner output
logging.getLogger("django.db.backends").setLevel(logging.WARNING)


class CallPermissionPerformanceTest(test.APITransactionTestCase):
    """
    Test SQL query performance for call-related permission scenarios in CustomerViewSet.

    This test measures the number of database queries required for customer list operations
    under different call-related user permission configurations to identify optimization opportunities.
    """

    def setUp(self):
        """Set up test fixtures with various call permission scenarios."""
        # Create base test data
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.regular_user = structure_factories.UserFactory()

        # Create multiple customers with projects to simulate real-world scenario
        self.num_customers = 10
        self.num_projects_per_customer = 3

        self.customers = []
        self.projects = []

        for i in range(self.num_customers):
            customer = structure_factories.CustomerFactory(name=f"Customer {i}")
            self.customers.append(customer)

            # Create projects for this customer
            customer_projects = []
            for j in range(self.num_projects_per_customer):
                project = structure_factories.ProjectFactory(
                    customer=customer, name=f"Project {i}-{j}"
                )
                customer_projects.append(project)
                self.projects.append(project)

        # Set up call-related users
        self._setup_call_users()

    def _setup_call_users(self):
        """Set up users with CALL_MANAGER and CALL_ORGANIZER roles."""
        # Create multiple call managing organizations to test scaling
        self.call_managing_organizations = []
        self.calls = []

        # Create call managing organizations for first 3 customers
        for i in range(3):
            call_managing_org = factories.CallManagingOrganisationFactory(
                customer=self.customers[i], description=f"Call Managing Org {i}"
            )
            self.call_managing_organizations.append(call_managing_org)

            # Create calls for each organization
            call = factories.CallFactory(manager=call_managing_org, name=f"Call {i}")
            self.calls.append(call)

        # Create CALL_MANAGER user with access to multiple calls
        self.call_manager_user = structure_factories.UserFactory()
        for i, call in enumerate(self.calls):
            call.add_user(self.call_manager_user, CallRole.MANAGER)
            self.call_managing_organizations[i].add_user(
                self.call_manager_user, CallRole.MANAGER
            )

        # Create CALL_ORGANIZER user with access to call managing organizations
        self.call_organizer_user = structure_factories.UserFactory()

        # Get the CALL_ORGANIZER role
        self.call_organizer_role = Role.objects.get_system_role(
            "CUSTOMER.CALL_ORGANIZER",
            content_type=ContentType.objects.get_for_model(
                models.CallManagingOrganisation
            ),
        )

        for i, call_managing_org in enumerate(self.call_managing_organizations):
            call_managing_org.add_user(
                self.call_organizer_user, self.call_organizer_role
            )
            self.customers[i].add_user(
                self.call_organizer_user, self.call_organizer_role
            )

        # Create single-call users for comparison
        self.single_call_manager = structure_factories.UserFactory()
        self.calls[0].add_user(self.single_call_manager, CallRole.MANAGER)
        self.call_managing_organizations[0].add_user(
            self.single_call_manager, CallRole.MANAGER
        )

        self.single_call_organizer = structure_factories.UserFactory()
        self.call_managing_organizations[0].add_user(
            self.single_call_organizer, self.call_organizer_role
        )
        self.customers[0].add_user(self.single_call_organizer, self.call_organizer_role)

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

        url = structure_factories.CustomerFactory.get_list_url()
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

    def test_staff_user_baseline_query_count(self):
        """Test query count for staff user (baseline for comparison)."""
        query_count, results, queries = self._capture_queries(self.staff_user)

        print("\n=== STAFF USER BASELINE (PROPOSAL TEST) ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print(f"Expected customers: {self.num_customers}")

        # Staff should see all customers
        self.assertEqual(len(results), self.num_customers)

        # Print detailed query information for analysis
        self._print_query_analysis(queries, "Staff User Baseline")

    def test_call_manager_query_count(self):
        """Test query count for user with CALL_MANAGER role across multiple calls."""
        query_count, results, queries = self._capture_queries(self.call_manager_user)

        print("\n=== CALL MANAGER PERFORMANCE (Multiple Calls) ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print("Expected customers: 3 (via 3 call managing organizations)")

        # User should see customers through call managing organizations
        self.assertEqual(len(results), 3)

        # Print detailed query information for analysis
        self._print_query_analysis(queries, "Call Manager (Multiple)")

    def test_call_organizer_query_count(self):
        """Test query count for user with CALL_ORGANIZER role across multiple organizations."""
        query_count, results, queries = self._capture_queries(self.call_organizer_user)

        print("\n=== CALL ORGANIZER PERFORMANCE (Multiple Orgs) ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print("Expected customers: 3 (via 3 call managing organizations)")

        # User should see customers through call managing organizations
        self.assertEqual(len(results), 3)

        # Print detailed query information for analysis
        self._print_query_analysis(queries, "Call Organizer (Multiple)")

    def test_single_call_manager_query_count(self):
        """Test query count for CALL_MANAGER with access to only one call."""
        query_count, results, queries = self._capture_queries(self.single_call_manager)

        print("\n=== SINGLE CALL MANAGER PERFORMANCE ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print("Expected customers: 1 (via 1 call managing organization)")

        # User should see one customer through call managing organization
        self.assertEqual(len(results), 1)

        # Print detailed query information for analysis
        self._print_query_analysis(queries, "Single Call Manager")

    def test_single_call_organizer_query_count(self):
        """Test query count for CALL_ORGANIZER with access to only one organization."""
        query_count, results, queries = self._capture_queries(
            self.single_call_organizer
        )

        print("\n=== SINGLE CALL ORGANIZER PERFORMANCE ===")
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")
        print("Expected customers: 1 (via 1 call managing organization)")

        # User should see one customer through call managing organization
        self.assertEqual(len(results), 1)

        # Print detailed query information for analysis
        self._print_query_analysis(queries, "Single Call Organizer")

    def test_filter_customers_direct_analysis(self):
        """Test the filter_customers function directly to identify bottlenecks."""
        from waldur_core.structure.models import filter_customers

        print("\n=== DIRECT FILTER_CUSTOMERS ANALYSIS ===")

        # Test with different users
        test_users = [
            ("Call Manager (Multiple)", self.call_manager_user),
            ("Call Organizer (Multiple)", self.call_organizer_user),
            ("Single Call Manager", self.single_call_manager),
            ("Regular User", self.regular_user),
        ]

        for user_type, user in test_users:
            print(f"\n--- {user_type} ---")

            with CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as context:
                q_obj = filter_customers(user)
                # Apply the filter to trigger actual queries
                filtered_customers = Customer.objects.filter(q_obj)
                results = list(filtered_customers)

            print(f"Filter query count: {len(context.captured_queries)}")
            print(f"Customers found: {len(results)}")

            # Analyze the specific queries generated by filter_customers
            self._analyze_filter_customers_queries(context.captured_queries, user_type)

    def test_permission_scaling_analysis(self):
        """Test how call permission queries scale with more data."""
        # Create additional call managing organizations
        additional_orgs = []
        for i in range(3, 7):  # Create 4 more organizations
            if i < len(self.customers):
                org = factories.CallManagingOrganisationFactory(
                    customer=self.customers[i], description=f"Additional Call Org {i}"
                )
                additional_orgs.append(org)

                call = factories.CallFactory(manager=org, name=f"Additional Call {i}")

                # Add our test users to these new organizations
                call.add_user(self.call_manager_user, CallRole.MANAGER)
                org.add_user(self.call_manager_user, CallRole.MANAGER)
                org.add_user(self.call_organizer_user, self.call_organizer_role)
                self.customers[i].add_user(
                    self.call_organizer_user, self.call_organizer_role
                )

        print("\n=== CALL PERMISSION SCALING ANALYSIS ===")
        print(
            f"Total call organizations: {len(self.call_managing_organizations) + len(additional_orgs)}"
        )

        # Test call manager with more organizations
        query_count, results, queries = self._capture_queries(self.call_manager_user)

        print(
            f"\nCall Manager with {len(self.call_managing_organizations) + len(additional_orgs)} organizations:"
        )
        print(f"Query count: {query_count}")
        print(f"Customers returned: {len(results)}")

        # Check if query count scales linearly (bad) or stays constant (good)
        self._print_query_analysis(queries, "Scaling Test")

    def test_call_performance_comparison(self):
        """Run comprehensive comparison of all call-related permission scenarios."""
        print(f"\n{'=' * 80}")
        print("CALL PERMISSION PERFORMANCE COMPARISON")
        print(f"{'=' * 80}")
        print(
            f"Test scenario: {self.num_customers} customers, {self.num_projects_per_customer} projects each"
        )
        print(f"Call organizations: {len(self.call_managing_organizations)}")

        # Run all tests and collect results
        print("Running individual test scenarios...")

        # Collect query counts by running capture directly
        staff_queries, _, _ = self._capture_queries(self.staff_user)
        call_manager_queries, _, _ = self._capture_queries(self.call_manager_user)
        call_organizer_queries, _, _ = self._capture_queries(self.call_organizer_user)
        single_manager_queries, _, _ = self._capture_queries(self.single_call_manager)
        single_organizer_queries, _, _ = self._capture_queries(
            self.single_call_organizer
        )

        # Run scaling test and capture queries
        # Create additional call managing organizations
        additional_orgs = []
        for i in range(3, 7):  # Create 4 more organizations
            if i < len(self.customers):
                org = factories.CallManagingOrganisationFactory(
                    customer=self.customers[i], description=f"Additional Call Org {i}"
                )
                additional_orgs.append(org)

                call = factories.CallFactory(manager=org, name=f"Additional Call {i}")

                # Add our test users to these new organizations
                call.add_user(self.call_manager_user, CallRole.MANAGER)
                org.add_user(self.call_manager_user, CallRole.MANAGER)
                org.add_user(self.call_organizer_user, self.call_organizer_role)
                self.customers[i].add_user(
                    self.call_organizer_user, self.call_organizer_role
                )

        scaling_queries, _, _ = self._capture_queries(self.call_manager_user)

        # Summary comparison
        print(f"\n{'=' * 80}")
        print("CALL PERMISSION QUERY COUNT SUMMARY")
        print(f"{'=' * 80}")
        print(f"Staff user (baseline):           {staff_queries:3d} queries")
        print(f"Call Manager (multiple calls):   {call_manager_queries:3d} queries")
        print(f"Call Organizer (multiple orgs):  {call_organizer_queries:3d} queries")
        print(f"Single Call Manager:             {single_manager_queries:3d} queries")
        print(f"Single Call Organizer:           {single_organizer_queries:3d} queries")
        print(f"Scaling test (7 organizations):  {scaling_queries:3d} queries")

        # Calculate performance ratios
        print(f"\n{'=' * 80}")
        print("PERFORMANCE IMPACT vs STAFF USER")
        print(f"{'=' * 80}")
        print(
            f"Call Manager (multiple):    {call_manager_queries / staff_queries:.1f}x slower"
        )
        print(
            f"Call Organizer (multiple):  {call_organizer_queries / staff_queries:.1f}x slower"
        )
        print(
            f"Single Call Manager:        {single_manager_queries / staff_queries:.1f}x slower"
        )
        print(
            f"Single Call Organizer:      {single_organizer_queries / staff_queries:.1f}x slower"
        )
        print(
            f"Scaling test:               {scaling_queries / staff_queries:.1f}x slower"
        )

        # Run direct filter analysis
        self.test_filter_customers_direct_analysis()

        # Run large dataset performance test
        self.test_call_users_with_large_dataset_performance()

    def test_call_users_with_large_dataset_performance(self):
        """Test call user performance when there are many customers they cannot see."""
        print(f"\n{'=' * 80}")
        print("CALL USER PERFORMANCE WITH LARGE DATASET (100 inaccessible customers)")
        print(f"{'=' * 80}")

        # Create 100 additional customers that call users have no access to
        inaccessible_customers = []
        for i in range(100):
            customer = structure_factories.CustomerFactory(
                name=f"Inaccessible Customer {i}"
            )
            # Create some projects to make it more realistic
            for j in range(2):
                structure_factories.ProjectFactory(
                    customer=customer, name=f"Inaccessible Project {i}-{j}"
                )
            inaccessible_customers.append(customer)

        print(
            f"Created {len(inaccessible_customers)} additional customers that call users cannot access"
        )
        print(
            f"Total customers in database: {len(self.customers) + len(inaccessible_customers)}"
        )
        print("Call users should still only see: 3 customers")

        # Test scenarios with large dataset
        scenarios = [
            ("Staff User (sees all 110 customers)", self.staff_user),
            ("Call Manager (sees 3 of 110 customers)", self.call_manager_user),
            ("Call Organizer (sees 3 of 110 customers)", self.call_organizer_user),
            ("Single Call Manager (sees 1 of 110 customers)", self.single_call_manager),
            (
                "Regular User (sees 0 of 110 customers)",
                structure_factories.UserFactory(),
            ),
        ]

        results = {}
        for name, user in scenarios:
            print(f"\n--- Testing {name} ---")

            # Warm up
            self.client.force_authenticate(user)
            url = structure_factories.CustomerFactory.get_list_url()
            self.client.get(url)

            # Capture actual performance
            query_count, customers_returned, queries = self._capture_queries(user)

            results[name] = {
                "queries": query_count,
                "customers": customers_returned,
                "query_details": queries,
            }

            print(f"  Query count: {query_count}")
            print(f"  Customers returned: {len(customers_returned)}")

            # Analyze permission-related queries specifically
            permission_queries = [
                query
                for query in queries
                if any(
                    table in query["sql"]
                    for table in [
                        "callmanagingorganisation",
                        "proposal_call",
                        "permissions_userrole",
                    ]
                )
            ]

            serializer_queries = [
                query
                for query in queries
                if any(
                    table in query["sql"]
                    for table in ["customercredit", "serviceprovider", "invoices_"]
                )
            ]

            print(f"  Permission queries: {len(permission_queries)}")
            print(f"  Serializer queries: {len(serializer_queries)}")

            # Show the most expensive permission query
            if permission_queries:
                expensive_perm_query = max(
                    permission_queries, key=lambda q: float(q["time"])
                )
                print(
                    f"  Most expensive permission query ({expensive_perm_query['time']}s):"
                )
                print(f"    {expensive_perm_query['sql'][:200]}...")

        # Performance comparison
        print(f"\n{'=' * 60}")
        print("LARGE DATASET PERFORMANCE COMPARISON")
        print(f"{'=' * 60}")

        for name, data in results.items():
            print(
                f"{name:<45} {data['queries']:3d} queries, {len(data['customers']):3d} customers"
            )

        # Calculate ratios vs staff user
        staff_queries = results["Staff User (sees all 110 customers)"]["queries"]
        print("\nPerformance vs Staff User:")
        for name, data in results.items():
            if name != "Staff User (sees all 110 customers)":
                ratio = data["queries"] / staff_queries if staff_queries > 0 else 0
                print(f"  {name:<45} {ratio:.2f}x")

        # Test if permission filtering scales with total dataset size
        print(f"\n{'=' * 60}")
        print("PERMISSION FILTERING SCALING ANALYSIS")
        print(f"{'=' * 60}")

        # Direct filter_customers test with large dataset
        from django.db import DEFAULT_DB_ALIAS, connections
        from django.test.utils import CaptureQueriesContext

        from waldur_core.structure.models import filter_customers

        test_users = [
            ("Call Manager", self.call_manager_user),
            ("Call Organizer", self.call_organizer_user),
        ]

        for user_name, user in test_users:
            print(f"\n--- {user_name} filter_customers with 110 total customers ---")

            with CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as context:
                q_obj = filter_customers(user)
                # Apply the filter to see actual performance impact
                filtered_customers = Customer.objects.filter(q_obj)
                results_list = list(filtered_customers)

            print(f"  Filter query count: {len(context.captured_queries)}")
            print(f"  Customers found: {len(results_list)}")
            print(
                f"  Total query time: {sum(float(q['time']) for q in context.captured_queries):.4f}s"
            )

            # Show the main filtering query
            if context.captured_queries:
                main_query = context.captured_queries[0]
                print(f"  Main filter query ({main_query['time']}s):")
                print(f"    {main_query['sql'][:300]}...")

                # Check if query uses indexes efficiently
                if "EXPLAIN" not in main_query["sql"]:
                    print("  Query analysis:")
                    if "callmanagingorganisation" in main_query["sql"].lower():
                        print("    ✓ Uses CallManagingOrganisation join")
                    if "permissions_userrole" in main_query["sql"].lower():
                        print("    ✓ Uses UserRole filtering")
                    if "IN (" in main_query["sql"]:
                        print("    ✓ Uses IN clause for efficiency")

        print(f"\n{'=' * 60}")
        print("CONCLUSION: Large Dataset Impact Analysis")
        print(f"{'=' * 60}")

        # Compare with our original small dataset results
        original_call_manager = 51  # From previous test
        original_staff = 115  # From previous test

        current_call_manager = results["Call Manager (sees 3 of 110 customers)"][
            "queries"
        ]
        current_staff = results["Staff User (sees all 110 customers)"]["queries"]

        print("Call Manager queries:")
        print(f"  Small dataset (10 customers): {original_call_manager} queries")
        print(f"  Large dataset (110 customers): {current_call_manager} queries")
        print(f"  Scaling impact: {current_call_manager / original_call_manager:.2f}x")

        print("\nStaff User queries:")
        print(f"  Small dataset (10 customers): {original_staff} queries")
        print(f"  Large dataset (110 customers): {current_staff} queries")
        print(f"  Scaling impact: {current_staff / original_staff:.2f}x")

        scaling_diff = (current_call_manager / original_call_manager) - (
            current_staff / original_staff
        )
        if abs(scaling_diff) > 0.1:
            print(
                f"\n🚨 PERFORMANCE ISSUE: Call users scale worse than staff by {scaling_diff:.2f}x"
            )
        else:
            print(
                "\n✅ Permission filtering scales similarly to staff user serialization"
            )

    def _analyze_filter_customers_queries(self, queries, user_type):
        """Analyze queries specifically generated by filter_customers function."""
        if not queries:
            print("  No queries generated")
            return

        for i, query in enumerate(queries):
            sql = query["sql"]
            print(f"  Query {i + 1}: {sql[:300]}...")

            # Identify call-specific query patterns
            call_patterns = []
            if "callmanagingorganisation" in sql.lower():
                call_patterns.append("CallManagingOrganisation join")
            if "permissions_userrole" in sql.lower():
                call_patterns.append("UserRole lookup")
            if "proposal_call" in sql.lower():
                call_patterns.append("Call lookup")
            if "content_type" in sql.lower():
                call_patterns.append("ContentType join")

            if call_patterns:
                print(f"    Patterns: {', '.join(call_patterns)}")

    def _print_query_analysis(self, queries, scenario_name):
        """Print detailed analysis of SQL queries."""
        print(f"\n--- {scenario_name} Query Analysis ---")

        # Group queries by type
        query_types = {}
        repeated_patterns = {}
        call_specific_queries = []

        for i, query in enumerate(queries):
            sql = query["sql"]

            # Categorize query types
            if "SELECT COUNT(*)" in sql:
                query_types["Count Query"] = query_types.get("Count Query", 0) + 1
            elif "structure_customer" in sql:
                query_types["Customer Query"] = query_types.get("Customer Query", 0) + 1
            elif any(
                table in sql
                for table in [
                    "callmanagingorganisation",
                    "proposal_call",
                    "permissions_userrole",
                ]
            ):
                query_types["Call Permission Query"] = (
                    query_types.get("Call Permission Query", 0) + 1
                )
                call_specific_queries.append((i, sql[:200]))
            elif any(
                table in sql
                for table in ["customercredit", "serviceprovider", "invoices_"]
            ):
                query_types["Serializer Query"] = (
                    query_types.get("Serializer Query", 0) + 1
                )
            else:
                query_types["Other"] = query_types.get("Other", 0) + 1

            # Track repeated patterns for N+1 detection
            # Normalize query by removing specific IDs and values
            normalized = sql
            for pattern in [r"\d+", r"'[^']*'", r'"[^"]*"']:
                import re

                normalized = re.sub(pattern, "X", normalized)

            if normalized in repeated_patterns:
                repeated_patterns[normalized]["count"] += 1
                repeated_patterns[normalized]["queries"].append(i)
            else:
                repeated_patterns[normalized] = {"count": 1, "queries": [i], "sql": sql}

        # Print query type breakdown
        sum(float(query["time"]) for query in queries)
        for qtype, count in query_types.items():
            type_time = sum(
                float(query["time"])
                for query in queries
                if (qtype == "Count Query" and "SELECT COUNT(*)" in query["sql"])
                or (
                    qtype == "Customer Query"
                    and "structure_customer" in query["sql"]
                    and "SELECT COUNT(*)" not in query["sql"]
                )
                or (
                    qtype == "Call Permission Query"
                    and any(
                        table in query["sql"]
                        for table in [
                            "callmanagingorganisation",
                            "proposal_call",
                            "permissions_userrole",
                        ]
                    )
                )
                or (
                    qtype == "Serializer Query"
                    and any(
                        table in query["sql"]
                        for table in ["customercredit", "serviceprovider", "invoices_"]
                    )
                )
                or (
                    qtype == "Other"
                    and not any(
                        marker in query["sql"]
                        for marker in [
                            "SELECT COUNT(*)",
                            "structure_customer",
                            "callmanagingorganisation",
                            "proposal_call",
                            "permissions_userrole",
                            "customercredit",
                            "serviceprovider",
                            "invoices_",
                        ]
                    )
                )
            )
            print(f"  {qtype}: {count} queries ({type_time:.3f}s total)")

        # Detect N+1 patterns
        n_plus_1_patterns = {
            pattern: data
            for pattern, data in repeated_patterns.items()
            if data["count"] > 1
        }

        if n_plus_1_patterns:
            print("\n🔍 Potential N+1 Patterns Detected:")
            for pattern, data in sorted(
                n_plus_1_patterns.items(), key=lambda x: x[1]["count"], reverse=True
            )[:3]:
                pattern_time = sum(float(queries[i]["time"]) for i in data["queries"])
                print(
                    f"  🚨 {data['count']} identical queries ({pattern_time:.3f}s total):"
                )
                print(f"     {data['sql'][:150]}...")
                print(f"     Query indexes: {data['queries']}")

        # Show call-specific queries
        if call_specific_queries:
            print("\n🎯 Call Permission Queries (potential bottleneck):")
            for i, sql in call_specific_queries[:3]:
                print(f"  Query {i + 1}: {sql}...")

        # Sample queries
        print("\n📋 Sample queries from main categories:")
        for qtype in ["Customer Query", "Call Permission Query", "Other"]:
            if qtype in query_types:
                for query in queries:
                    sql = query["sql"]
                    if (
                        (
                            qtype == "Customer Query"
                            and "structure_customer" in sql
                            and "SELECT COUNT(*)" not in sql
                        )
                        or (
                            qtype == "Call Permission Query"
                            and any(
                                table in sql
                                for table in [
                                    "callmanagingorganisation",
                                    "proposal_call",
                                    "permissions_userrole",
                                ]
                            )
                        )
                        or (
                            qtype == "Other"
                            and not any(
                                marker in sql
                                for marker in [
                                    "structure_customer",
                                    "callmanagingorganisation",
                                    "proposal_call",
                                    "permissions_userrole",
                                ]
                            )
                        )
                    ):
                        print(f"\n  {qtype} example:")
                        print(f"    {sql[:200]}...")
                        break
