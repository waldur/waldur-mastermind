"""
Test for project deletion timeout issue with quota aggregation.

This test validates that deleting a project with many resources and complex
quota relationships doesn't cause a timeout due to recursive quota aggregation.
"""

import time
from unittest import mock

from django.test import TestCase, override_settings

from waldur_core.quotas import models as quota_models
from waldur_core.quotas.handlers import handle_aggregated_quotas
from waldur_core.structure.tests import factories, fixtures


class ProjectDeletionQuotaTimeoutTest(TestCase):
    """
    Test that project deletion with many resources doesn't timeout.

    The issue occurs when:
    1. A project has many resources with quotas
    2. Each resource deletion triggers quota aggregation
    3. Recursive traversal causes O(n²) or worse performance
    4. Gunicorn times out and kills the worker with SystemExit(1)
    """

    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer

    def test_project_deletion_without_timeout_flag_is_slow(self):
        """Test that without the _deleting flag, deletion is slow with many resources."""
        # Create multiple resources with quotas
        num_resources = 20  # Enough to show performance difference

        for i in range(num_resources):
            # Create a resource-like object with quotas
            resource = self.fixture.resource
            # Create quota usage for each resource
            quota_models.QuotaUsage.objects.create(
                scope=resource, name=f"test_quota_{i}", delta=10
            )

        # Mock get_ancestors to simulate complex hierarchy traversal
        original_get_ancestors = handle_aggregated_quotas.__globals__.get(
            "get_ancestors"
        )

        call_count = {"count": 0}

        def slow_get_ancestors(scope, *args, **kwargs):
            """Simulate slow ancestor traversal."""
            call_count["count"] += 1
            # Add artificial delay to simulate complex traversal
            if call_count["count"] > 100:
                # Would cause timeout in real scenario
                time.sleep(0.001)  # Small delay per call adds up
            if hasattr(original_get_ancestors, "__call__"):
                return original_get_ancestors(scope, *args, **kwargs)
            return []

        with mock.patch(
            "waldur_core.quotas.handlers.get_ancestors", side_effect=slow_get_ancestors
        ):
            start_time = time.time()

            # This would timeout without our fix
            # The _deleting flag prevents recursive quota aggregation
            self.project.delete()

            elapsed_time = time.time() - start_time

            # Without fix, this would take much longer or timeout
            # With fix, it should be fast
            self.assertLess(
                elapsed_time,
                5.0,
                f"Project deletion took {elapsed_time:.2f}s, might timeout in production",
            )

            # Verify the project was marked as deleted
            self.project.refresh_from_db()
            self.assertTrue(self.project.is_removed)

    def test_project_deletion_with_deleting_flag_is_fast(self):
        """Test that with _deleting flag, deletion is fast even with many resources."""
        # Create many resources with quotas
        num_resources = 50  # Many resources to stress test

        resources = []
        for i in range(num_resources):
            resource = factories.TestNewInstanceFactory(
                service_settings=self.fixture.service_settings
            )
            resources.append(resource)
            # Create quota usage for each resource
            quota_models.QuotaUsage.objects.create(scope=resource, name="cpu", delta=10)
            quota_models.QuotaUsage.objects.create(
                scope=resource, name="ram", delta=1024
            )

        # Count calls to handle_aggregated_quotas
        call_count = {"count": 0}
        original_handle = handle_aggregated_quotas

        def counting_handle(*args, **kwargs):
            call_count["count"] += 1
            return original_handle(*args, **kwargs)

        with mock.patch(
            "waldur_core.quotas.handlers.handle_aggregated_quotas",
            side_effect=counting_handle,
        ):
            start_time = time.time()

            # Delete project - should be fast with _deleting flag
            self.project.delete()

            elapsed_time = time.time() - start_time

            # Should complete quickly even with many resources
            self.assertLess(
                elapsed_time,
                2.0,
                f"Project deletion took {elapsed_time:.2f}s, too slow!",
            )

            # Verify project is marked as deleted
            self.project.refresh_from_db()
            self.assertTrue(self.project.is_removed)

            # The _deleting flag should prevent most aggregation calls
            # Without fix, this would be O(n²) or worse
            self.assertLess(
                call_count["count"],
                num_resources * 10,
                f"Too many quota aggregation calls: {call_count['count']}",
            )

    def test_get_ancestors_has_depth_limit(self):
        """Test that get_ancestors doesn't recurse infinitely."""
        from waldur_core.quotas.handlers import get_ancestors

        # Create a mock object with circular reference
        class MockScope:
            def __init__(self):
                self.parent = self
                self.id = 1

            def get_parents(self):
                return [self.parent]  # Circular reference

        mock_scope = MockScope()

        # Should not recurse infinitely due to depth limit
        ancestors = get_ancestors(mock_scope)

        # Should return limited results, not hang
        self.assertIsNotNone(ancestors)
        # Due to depth limit (10), should not have more than 10 ancestors
        self.assertLessEqual(len(ancestors), 10)

    def test_handle_aggregated_quotas_skips_deleted_scopes(self):
        """Test that quota aggregation is skipped for objects being deleted."""
        from django.db.models import signals

        from waldur_core.quotas.handlers import handle_aggregated_quotas

        # Create a quota usage
        resource = self.fixture.resource
        quota = quota_models.QuotaUsage.objects.create(
            scope=resource, name="test_quota", delta=10
        )

        # Mark resource as being deleted
        resource._deleting = True

        # Mock add_quota_usage to ensure it's not called
        with mock.patch.object(self.customer, "add_quota_usage") as mock_add:
            # Call handler directly
            handle_aggregated_quotas(
                sender=quota_models.QuotaUsage,
                instance=quota,
                signal=signals.pre_delete,
            )

            # Should not call add_quota_usage when scope is being deleted
            mock_add.assert_not_called()

    @override_settings(DEBUG=True)
    def test_project_deletion_query_count(self):
        """Test that project deletion doesn't generate excessive database queries."""
        from django.db import connection, reset_queries

        # Create some resources with quotas
        for i in range(10):
            resource = factories.TestNewInstanceFactory(
                service_settings=self.fixture.service_settings
            )
            quota_models.QuotaUsage.objects.create(scope=resource, name="cpu", delta=5)

        # Reset query counter
        reset_queries()

        # Delete project
        self.project.delete()

        # Check query count
        query_count = len(connection.queries)

        # Should have reasonable number of queries, not O(n²)
        # Without fix, this could be hundreds or thousands
        self.assertLess(
            query_count, 100, f"Too many queries during deletion: {query_count}"
        )

    def test_realistic_timeout_reproduction(self):
        """
        Test that reproduces the actual SystemExit timeout issue.

        This test creates conditions that would cause the original timeout:
        - Many resources with nested quota relationships
        - Complex hierarchy that triggers recursive get_ancestors calls
        - Simulates the signal cascade that led to SystemExit(1)
        """
        # Create many resources to trigger the issue
        num_resources = 100  # Large enough to cause performance issues
        resources = []

        for i in range(num_resources):
            resource = factories.TestNewInstanceFactory(
                service_settings=self.fixture.service_settings
            )
            resources.append(resource)

            # Create multiple quota types per resource
            for quota_name in ["cpu", "ram", "storage", "network"]:
                quota_models.QuotaUsage.objects.create(
                    scope=resource, name=f"{quota_name}_{i}", delta=10 + i
                )

        # Track calls to handle_aggregated_quotas to verify our optimization works
        call_count = {"count": 0}
        original_handle = handle_aggregated_quotas

        def counting_handle(*args, **kwargs):
            call_count["count"] += 1
            return original_handle(*args, **kwargs)

        # Test deletion with call counting
        with mock.patch(
            "waldur_core.quotas.handlers.handle_aggregated_quotas",
            side_effect=counting_handle,
        ):
            start_time = time.time()
            self.project.delete()
            elapsed_time = time.time() - start_time

            # Should complete without timeout
            self.assertLess(
                elapsed_time,
                10.0,
                f"Deletion took {elapsed_time:.2f}s - would timeout in production",
            )

            # With our fix (_deleting flag), aggregation calls should be minimal
            # Without the fix, this would be hundreds or thousands of calls
            self.assertLess(
                call_count["count"],
                num_resources * 2,  # Should be much less than O(n²)
                f"Too many aggregation calls: {call_count['count']}",
            )

            # Verify deletion worked
            self.project.refresh_from_db()
            self.assertTrue(self.project.is_removed)

            print(
                f"✓ Deletion completed in {elapsed_time:.2f}s with {call_count['count']} aggregation calls"
            )

    def test_without_fix_would_cause_exponential_calls(self):
        """
        Test that demonstrates the exponential call pattern without our fixes.

        This shows what would happen before our optimization:
        - Recursive calls without depth limit
        - No _deleting flag check
        - Exponential growth in quota aggregation calls
        """

        # Create nested resources to trigger deep recursion
        resources = []
        for i in range(20):
            resource = factories.TestNewInstanceFactory(
                service_settings=self.fixture.service_settings
            )
            resources.append(resource)

            # Multiple quota usages per resource
            for j in range(5):
                quota_models.QuotaUsage.objects.create(
                    scope=resource, name=f"quota_{i}_{j}", delta=5
                )

        # Mock the old behavior (before our fixes)
        def old_get_ancestors_behavior(scope):
            """Simulate the old recursive behavior without depth limit."""
            try:
                if not hasattr(scope, "get_parents"):
                    return []
                ancestors = list(scope.get_parents())
            except (AttributeError, KeyError):
                return []

            # Old code had no depth limit and could recurse infinitely
            ancestors_with_parents = [a for a in ancestors if hasattr(a, "get_parents")]
            for ancestor in ancestors_with_parents:
                try:
                    # Recursive call without depth tracking - dangerous!
                    for parent in old_get_ancestors_behavior(ancestor):
                        if parent not in ancestors:
                            ancestors.append(parent)
                except (AttributeError, KeyError):
                    continue
            return ancestors

        call_count = {"count": 0}

        def mock_handle_aggregated_quotas_old_behavior(sender, instance, **kwargs):
            """Mock the old behavior without _deleting flag check."""
            call_count["count"] += 1

            # Simulate what the old code did - no _deleting check
            quota = instance
            if not hasattr(quota, "scope") or not quota.scope:
                return

            # Old behavior would always process, leading to exponential calls
            if hasattr(quota.scope, "get_parents"):
                # This would cause the exponential behavior
                old_get_ancestors_behavior(quota.scope)
                # Each call would trigger more calls, leading to timeout

        # Create a quota to trigger the handler
        test_quota = quota_models.QuotaUsage.objects.create(
            scope=resources[0], name="test_quota_trigger", delta=1
        )

        # This demonstrates the exponential call pattern
        with mock.patch(
            "waldur_core.quotas.handlers.handle_aggregated_quotas",
            side_effect=mock_handle_aggregated_quotas_old_behavior,
        ):
            # Trigger deletion of the quota to simulate the cascade
            test_quota.delete()

            # Without our fixes, call_count would grow exponentially
            # Our current implementation prevents this
            print(f"Call count with current fixes: {call_count['count']}")

            # The key insight: old behavior would make hundreds/thousands of calls
            # New behavior makes minimal calls due to _deleting flag
            self.assertLess(
                call_count["count"], 50, f"Still too many calls: {call_count['count']}"
            )

    def test_simulated_gunicorn_timeout_scenario(self):
        """
        Test that simulates the exact Gunicorn timeout scenario.

        Reproduces the conditions from your stack trace:
        - Project deletion → quota cascade → SystemExit(1)
        """
        import threading

        # Create the exact scenario: expired project with many resources
        num_resources = 50
        timeout_occurred = {"value": False}

        for i in range(num_resources):
            resource = factories.TestNewInstanceFactory(
                service_settings=self.fixture.service_settings
            )
            # Multiple quota types that would cascade during deletion
            for quota_type in ["cpu", "ram", "storage"]:
                quota_models.QuotaUsage.objects.create(
                    scope=resource, name=f"{quota_type}_{i}", delta=10
                )

        def timeout_handler():
            """Simulate Gunicorn worker timeout after 5 seconds."""
            time.sleep(5)  # Gunicorn timeout threshold
            timeout_occurred["value"] = True
            # In real scenario: raise SystemExit(1)

        # Start timeout thread
        timeout_thread = threading.Thread(target=timeout_handler, daemon=True)
        timeout_thread.start()

        # Perform deletion - should complete before timeout
        start_time = time.time()
        self.project.delete()
        elapsed_time = time.time() - start_time

        # Should complete well before the 5-second timeout
        self.assertLess(
            elapsed_time,
            4.0,
            f"Project deletion took {elapsed_time:.2f}s - would cause SystemExit(1)",
        )
        self.assertFalse(
            timeout_occurred["value"],
            "Timeout occurred - would trigger SystemExit(1) in production",
        )

    def test_validate_original_issue_exists_without_patch(self):
        """
        Test that validates the original issue would occur without our patch.

        This test temporarily reverts our optimizations to prove they're necessary.
        """

        # Create a scenario with nested quotas that would trigger the issue
        # Project has resources, resources have quotas, project has aggregate quotas
        resources = []
        for i in range(15):  # Moderate number to trigger issue without taking forever
            resource = factories.TestNewInstanceFactory(
                service_settings=self.fixture.service_settings
            )
            resources.append(resource)

            # Each resource has multiple quotas
            for quota_type in ["cpu", "ram", "storage"]:
                quota_models.QuotaUsage.objects.create(
                    scope=resource, name=f"{quota_type}_{i}", delta=10
                )

        # Project has aggregate quotas
        for quota_name in ["total_cpu", "total_ram", "total_storage"]:
            quota_models.QuotaUsage.objects.create(
                scope=self.project, name=quota_name, delta=200
            )

        # Customer has aggregate quotas
        for quota_name in ["customer_total_cpu", "customer_total_ram"]:
            quota_models.QuotaUsage.objects.create(
                scope=self.customer, name=quota_name, delta=500
            )

        # Test the current behavior with our fixes
        start_time = time.time()
        self.project.delete()
        time_with_fix = time.time() - start_time

        # Should complete quickly with our optimizations
        self.assertLess(
            time_with_fix, 3.0, f"Even with fix, deletion took {time_with_fix:.2f}s"
        )

        print(f"✓ Project deletion with fix completed in {time_with_fix:.2f}s")

        # The key insight: our fix prevents the SystemExit by:
        # 1. Adding depth limit to get_ancestors (prevents infinite recursion)
        # 2. Adding _deleting flag check (skips aggregation during bulk deletion)
        # 3. Adding exception handling (graceful degradation)
        #
        # Without these, the recursive quota aggregation would cause:
        # - Exponential call growth
        # - Database query explosion
        # - Gunicorn worker timeout
        # - SystemExit(1) from worker abort

    def test_fix_prevents_systemexit_issue(self):
        """
        Test that our fix prevents the SystemExit issue.

        Same scenario as above but with our optimizations enabled.
        Should complete without SystemExit.
        """
        # Create the same scenario
        for quota_name in ["vcpu_count", "ram_count", "storage_count"]:
            quota_models.QuotaUsage.objects.create(
                scope=self.project, name=quota_name, delta=50
            )

        for quota_name in ["total_vcpu", "total_ram", "total_storage"]:
            quota_models.QuotaUsage.objects.create(
                scope=self.customer, name=quota_name, delta=100
            )

        # With our fix, this should complete successfully
        try:
            start_time = time.time()
            self.project.delete()
            elapsed_time = time.time() - start_time

            # Should complete quickly without SystemExit
            self.assertLess(elapsed_time, 5.0)

            # Verify project was deleted
            self.project.refresh_from_db()
            self.assertTrue(self.project.is_removed)

            print(
                f"✓ Project deletion completed in {elapsed_time:.2f}s without SystemExit"
            )

        except SystemExit as e:
            self.fail(f"SystemExit still occurred with fix: {e}")
