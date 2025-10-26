"""
Tests for Rancher usage collection functionality including the UnifiedRancherUsageCollector
and report_rancher_usage task.
"""

from datetime import datetime
from unittest.mock import patch

from django.test import TestCase

from waldur_core.core.enums import CoreStates
from waldur_mastermind.marketplace.enums import RANCHER_OFFERING, ResourceStates
from waldur_mastermind.marketplace.models import ComponentUsage
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_rancher.tasks import report_rancher_usage
from waldur_mastermind.marketplace_rancher.utils import UnifiedRancherUsageCollector
from waldur_openstack.tests import factories as openstack_factories
from waldur_rancher.tests import factories as rancher_factories


class UnifiedRancherUsageCollectorTest(TestCase):
    """Test the UnifiedRancherUsageCollector class."""

    def setUp(self):
        self.collector = UnifiedRancherUsageCollector()

    def test_collect_usage_accepts_resource_object(self):
        """Test that collect_usage method properly accepts a Resource object."""
        # Create a mock cluster
        cluster = rancher_factories.ClusterFactory()

        # Create a marketplace offering with plugin options
        offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, plugin_options={"deployment_mode": "managed"}
        )

        # Create a marketplace resource with the cluster as scope
        resource = marketplace_factories.ResourceFactory(
            offering=offering, scope=cluster, state=ResourceStates.OK
        )

        # Mock the Instance.objects.filter to return empty queryset
        with patch("waldur_openstack.models.Instance.objects.filter") as mock_filter:
            mock_filter.return_value = []

            # This should not raise an AttributeError
            usage = self.collector.collect_usage(resource)

            # Verify the structure of returned usage
            self.assertIsInstance(usage, dict)
            self.assertIn("cpu_hours", usage)
            self.assertIn("ram_hours", usage)
            self.assertIn("storage_hours", usage)

    def test_collect_usage_with_managed_deployment_mode(self):
        """Test that managed deployment mode calls the correct collection method."""
        cluster = rancher_factories.ClusterFactory()
        offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, plugin_options={"deployment_mode": "managed"}
        )
        resource = marketplace_factories.ResourceFactory(
            offering=offering, scope=cluster, state=ResourceStates.OK
        )

        with patch.object(self.collector, "_collect_managed_usage") as mock_managed:
            mock_managed.return_value = {
                "cpu_hours": 4,
                "ram_hours": 8,
                "storage_hours": 100,
            }

            usage = self.collector.collect_usage(resource)

            mock_managed.assert_called_once_with(cluster)
            self.assertEqual(usage["cpu_hours"], 4)

    def test_collect_usage_with_self_managed_deployment_mode(self):
        """Test that self-managed deployment mode calls the correct collection method."""
        cluster = rancher_factories.ClusterFactory()
        offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, plugin_options={"deployment_mode": "self-managed"}
        )
        resource = marketplace_factories.ResourceFactory(
            offering=offering, scope=cluster, state=ResourceStates.OK
        )

        with patch.object(
            self.collector, "_collect_self_managed_usage"
        ) as mock_self_managed:
            mock_self_managed.return_value = {
                "cpu_hours": 2,
                "ram_hours": 4,
                "storage_hours": 50,
            }

            usage = self.collector.collect_usage(resource)

            mock_self_managed.assert_called_once_with(cluster)
            self.assertEqual(usage["cpu_hours"], 2)

    def test_collect_usage_defaults_to_self_managed(self):
        """Test that missing or unknown deployment mode defaults to self-managed."""
        cluster = rancher_factories.ClusterFactory()
        offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING,
            plugin_options={},  # No deployment_mode specified
        )
        resource = marketplace_factories.ResourceFactory(
            offering=offering, scope=cluster, state=ResourceStates.OK
        )

        with patch.object(
            self.collector, "_collect_self_managed_usage"
        ) as mock_self_managed:
            mock_self_managed.return_value = {
                "cpu_hours": 2,
                "ram_hours": 4,
                "storage_hours": 50,
            }

            self.collector.collect_usage(resource)

            mock_self_managed.assert_called_once_with(cluster)

    def test_collect_usage_requires_resource_not_cluster(self):
        """Test that the core bug fix works - collect_usage requires Resource, not Cluster."""
        cluster = rancher_factories.ClusterFactory()

        # Test that passing a Cluster directly would fail (this simulates the original bug)
        with self.assertRaises(AttributeError):
            # This should fail because Cluster doesn't have offering attribute
            self.collector.collect_usage(cluster)  # type: ignore[arg-type]

        # Test that passing a Resource works correctly
        offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, plugin_options={"deployment_mode": "managed"}
        )
        resource = marketplace_factories.ResourceFactory(
            offering=offering, scope=cluster, state=ResourceStates.OK
        )

        with patch.object(self.collector, "_collect_managed_usage") as mock_managed:
            mock_managed.return_value = {
                "cpu_hours": 4,
                "ram_hours": 8,
                "storage_hours": 100,
            }

            # This should work because Resource has offering attribute
            usage = self.collector.collect_usage(resource)
            self.assertIsInstance(usage, dict)


class ReportRancherUsageTaskTest(TestCase):
    """Test the report_rancher_usage Celery task."""

    def setUp(self):
        # Create service settings and offering
        self.service_settings = rancher_factories.RancherServiceSettingsFactory()
        self.offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, plugin_options={"deployment_mode": "managed"}
        )

        # Create offering components
        self.cpu_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="cpu_hours"
        )
        self.ram_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="ram_hours"
        )
        self.storage_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="storage_hours"
        )

    def test_task_processes_ok_resources_only(self):
        """Test that the task only processes resources in OK state."""
        cluster1 = rancher_factories.ClusterFactory()
        cluster2 = rancher_factories.ClusterFactory()

        # Create resources in different states
        ok_resource = marketplace_factories.ResourceFactory(
            offering=self.offering, scope=cluster1, state=ResourceStates.OK
        )

        marketplace_factories.ResourceFactory(
            offering=self.offering, scope=cluster2, state=ResourceStates.CREATING
        )

        with patch(
            "waldur_mastermind.marketplace_rancher.utils.UnifiedRancherUsageCollector.collect_usage"
        ) as mock_collect:
            mock_collect.return_value = {
                "cpu_hours": 4,
                "ram_hours": 8,
                "storage_hours": 100,
            }

            report_rancher_usage()

            # Should only be called once for the OK resource
            self.assertEqual(mock_collect.call_count, 1)
            mock_collect.assert_called_with(ok_resource)

    def test_task_creates_component_usage_records(self):
        """Test that the task creates ComponentUsage records."""
        cluster = rancher_factories.ClusterFactory()
        resource = marketplace_factories.ResourceFactory(
            offering=self.offering, scope=cluster, state=ResourceStates.OK
        )

        with patch(
            "waldur_mastermind.marketplace_rancher.utils.UnifiedRancherUsageCollector.collect_usage"
        ) as mock_collect:
            mock_collect.return_value = {
                "cpu_hours": 4,
                "ram_hours": 8,
                "storage_hours": 100,
            }

            # Count initial usage records
            initial_count = ComponentUsage.objects.count()

            report_rancher_usage()

            # Should create 3 new ComponentUsage records
            self.assertEqual(ComponentUsage.objects.count(), initial_count + 3)

            # Verify the usage values
            cpu_usage = ComponentUsage.objects.get(
                resource=resource, component=self.cpu_component
            )
            self.assertEqual(cpu_usage.usage, 4)

            ram_usage = ComponentUsage.objects.get(
                resource=resource, component=self.ram_component
            )
            self.assertEqual(ram_usage.usage, 8)

            storage_usage = ComponentUsage.objects.get(
                resource=resource, component=self.storage_component
            )
            self.assertEqual(storage_usage.usage, 100)

    def test_task_skips_resources_without_scope(self):
        """Test that the task skips resources that don't have a scope."""
        # Create resource without scope (scope=None)
        marketplace_factories.ResourceFactory(
            offering=self.offering, scope=None, state=ResourceStates.OK
        )

        with patch(
            "waldur_mastermind.marketplace_rancher.utils.UnifiedRancherUsageCollector.collect_usage"
        ) as mock_collect:
            report_rancher_usage()

            # Should not call collect_usage for resources without scope
            mock_collect.assert_not_called()

    def test_task_updates_existing_usage_records(self):
        """Test that the task updates existing ComponentUsage records instead of creating duplicates."""
        cluster = rancher_factories.ClusterFactory()
        resource = marketplace_factories.ResourceFactory(
            offering=self.offering, scope=cluster, state=ResourceStates.OK
        )

        # Create existing usage record
        today = datetime.today()
        existing_usage = marketplace_factories.ComponentUsageFactory(
            resource=resource, component=self.cpu_component, usage=2, date=today
        )

        with patch(
            "waldur_mastermind.marketplace_rancher.utils.UnifiedRancherUsageCollector.collect_usage"
        ) as mock_collect:
            mock_collect.return_value = {
                "cpu_hours": 4,
                "ram_hours": 8,
                "storage_hours": 100,
            }

            initial_count = ComponentUsage.objects.count()

            report_rancher_usage()

            # Should not create new records, just update existing ones
            # We should have the same count + 2 new ones (ram and storage)
            self.assertEqual(ComponentUsage.objects.count(), initial_count + 2)

            # Verify the existing record was updated
            existing_usage.refresh_from_db()
            self.assertEqual(existing_usage.usage, 4)

    def test_task_filters_rancher_offering_type_only(self):
        """Test that the task only processes resources with RANCHER_OFFERING type."""
        # Create non-Rancher offering
        other_offering = marketplace_factories.OfferingFactory(type="OTHER_TYPE")

        cluster = rancher_factories.ClusterFactory()
        marketplace_factories.ResourceFactory(
            offering=other_offering, scope=cluster, state=ResourceStates.OK
        )

        with patch(
            "waldur_mastermind.marketplace_rancher.utils.UnifiedRancherUsageCollector.collect_usage"
        ) as mock_collect:
            report_rancher_usage()

            # Should not process non-Rancher resources
            mock_collect.assert_not_called()


class UnifiedRancherUsageCollectorIntegrationTest(TestCase):
    """Integration tests that test actual database filtering without mocks."""

    def setUp(self):
        self.collector = UnifiedRancherUsageCollector()

    def test_managed_usage_collection_filters_by_state_correctly(self):
        """Test that managed usage collection works without ValueError and filters by state."""
        # Create tenant and cluster
        tenant = openstack_factories.TenantFactory(state=CoreStates.OK)
        cluster = rancher_factories.ClusterFactory()

        # Create instances in different states
        ok_instance = openstack_factories.InstanceFactory(
            tenant=tenant, state=CoreStates.OK, cores=4, ram=8192
        )
        creating_instance = openstack_factories.InstanceFactory(
            tenant=tenant, state=CoreStates.CREATING, cores=2, ram=4096
        )

        # Create volumes in different states
        ok_volume = openstack_factories.VolumeFactory(
            tenant=tenant,
            state=CoreStates.OK,
            size=20480,  # 20GB in MB
        )
        creating_volume = openstack_factories.VolumeFactory(
            tenant=tenant,
            state=CoreStates.CREATING,
            size=40960,  # 40GB in MB
        )

        # Associate volumes with instances
        ok_instance.volumes.add(ok_volume)
        creating_instance.volumes.add(creating_volume)

        # Create nodes so that linked_tenant_ids property returns the tenant
        rancher_factories.NodeFactory(cluster=cluster, instance=ok_instance)
        rancher_factories.NodeFactory(cluster=cluster, instance=creating_instance)

        # Create marketplace resource
        offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, plugin_options={"deployment_mode": "managed"}
        )
        resource = marketplace_factories.ResourceFactory(
            offering=offering, scope=cluster, state=ResourceStates.OK
        )

        # Test the actual usage collection - should not raise ValueError
        try:
            usage = self.collector.collect_usage(resource)
            # Basic validation that usage collection works
            self.assertIsInstance(usage, dict)
            self.assertIn("cpu_hours", usage)
            self.assertIn("ram_hours", usage)
            self.assertIn("storage_hours", usage)
            # Should include OK instance but not CREATING instance
            self.assertGreater(usage["cpu_hours"], 0)
        except ValueError as e:
            if "invalid literal for int()" in str(e) and "'OK'" in str(e):
                self.fail(
                    f"ValueError indicates the bug still exists: {e}. "
                    "Make sure to use CoreStates.OK instead of 'OK' in filtering."
                )
            else:
                raise

    def test_self_managed_usage_collection_filters_by_state_correctly(self):
        """Test that self-managed usage collection works without ValueError and filters by state."""
        # Create cluster with nodes
        cluster = rancher_factories.ClusterFactory()

        # Create tenant for instances
        tenant = openstack_factories.TenantFactory(state=CoreStates.OK)

        # Create instances for nodes
        ok_instance = openstack_factories.InstanceFactory(
            tenant=tenant, state=CoreStates.OK, cores=4, ram=8192
        )
        creating_instance = openstack_factories.InstanceFactory(
            tenant=tenant, state=CoreStates.CREATING, cores=2, ram=4096
        )

        # Create volumes
        ok_volume = openstack_factories.VolumeFactory(
            tenant=tenant,
            state=CoreStates.OK,
            size=20480,  # 20GB in MB
        )
        creating_volume = openstack_factories.VolumeFactory(
            tenant=tenant,
            state=CoreStates.CREATING,
            size=10240,  # 10GB in MB
        )

        # Associate volumes
        ok_instance.volumes.add(ok_volume)
        creating_instance.volumes.add(creating_volume)

        # Create nodes in different states
        rancher_factories.NodeFactory(
            cluster=cluster, instance=ok_instance, state=CoreStates.OK
        )
        rancher_factories.NodeFactory(
            cluster=cluster, instance=creating_instance, state=CoreStates.CREATING
        )

        # Create marketplace resource
        offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, plugin_options={"deployment_mode": "self-managed"}
        )
        resource = marketplace_factories.ResourceFactory(
            offering=offering, scope=cluster, state=ResourceStates.OK
        )

        # Test the actual usage collection - should not raise ValueError
        try:
            usage = self.collector.collect_usage(resource)
            # Basic validation that usage collection works
            self.assertIsInstance(usage, dict)
            self.assertIn("cpu_hours", usage)
            self.assertIn("ram_hours", usage)
            self.assertIn("storage_hours", usage)
            # Should include OK node with OK instance but not CREATING node
            self.assertGreater(usage["cpu_hours"], 0)
        except ValueError as e:
            if "invalid literal for int()" in str(e) and "'OK'" in str(e):
                self.fail(
                    f"ValueError indicates the bug still exists: {e}. "
                    "Make sure to use CoreStates.OK instead of 'OK' in filtering."
                )
            else:
                raise

    def test_state_filtering_prevents_value_error(self):
        """Test that the bug fix prevents ValueError when filtering by state."""
        # This test ensures that the code uses CoreStates.OK (integer) instead of "OK" (string)
        tenant = openstack_factories.TenantFactory(state=CoreStates.OK)
        cluster = rancher_factories.ClusterFactory()

        # Create instance in OK state
        instance = openstack_factories.InstanceFactory(
            tenant=tenant, state=CoreStates.OK, cores=2, ram=4096
        )

        # Create node so that linked_tenant_ids property returns the tenant
        rancher_factories.NodeFactory(cluster=cluster, instance=instance)

        offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, plugin_options={"deployment_mode": "managed"}
        )
        resource = marketplace_factories.ResourceFactory(
            offering=offering, scope=cluster, state=ResourceStates.OK
        )

        # This should not raise ValueError: invalid literal for int() with base 10: 'OK'
        try:
            usage = self.collector.collect_usage(resource)
            self.assertIsInstance(usage, dict)
            self.assertIn("cpu_hours", usage)
            self.assertIn("ram_hours", usage)
            self.assertIn("storage_hours", usage)
        except ValueError as e:
            if "invalid literal for int()" in str(e) and "'OK'" in str(e):
                self.fail(
                    f"ValueError indicates the bug still exists: {e}. "
                    "Make sure to use CoreStates.OK instead of 'OK' in filtering."
                )
            else:
                raise
