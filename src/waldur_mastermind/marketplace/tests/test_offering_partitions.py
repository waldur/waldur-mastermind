"""Tests for OfferingPartition model and related functionality."""

from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


class OfferingPartitionModelTest(test.APITestCase):
    """Test OfferingPartition model functionality."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.partition = factories.OfferingPartitionFactory(offering=self.offering)

    def test_partition_has_uuid(self):
        """Test that partition has UUID field."""
        self.assertIsNotNone(self.partition.uuid)
        self.assertEqual(len(str(self.partition.uuid)), 32)

    def test_partition_has_timestamps(self):
        """Test that partition has created and modified timestamps."""
        self.assertIsNotNone(self.partition.created)
        self.assertIsNotNone(self.partition.modified)

    def test_partition_belongs_to_offering(self):
        """Test that partition is linked to an offering."""
        self.assertEqual(self.partition.offering, self.offering)
        self.assertIn(self.partition, self.offering.partitions.all())

    def test_partition_str_representation(self):
        """Test string representation of partition."""
        expected = f"{self.offering.name} - {self.partition.partition_name}"
        self.assertEqual(str(self.partition), expected)

    def test_unique_constraint_on_offering_and_partition_name(self):
        """Test that partition_name is unique per offering."""
        # First partition should be created successfully
        partition1 = factories.OfferingPartitionFactory(
            offering=self.offering, partition_name="test-partition"
        )
        self.assertTrue(
            models.OfferingPartition.objects.filter(pk=partition1.pk).exists()
        )

        # Creating another partition with same name and offering should raise error
        with self.assertRaises(Exception):
            factories.OfferingPartitionFactory(
                offering=self.offering, partition_name="test-partition"
            )

    def test_different_offerings_can_have_same_partition_names(self):
        """Test that different offerings can have partitions with same names."""
        other_offering = factories.OfferingFactory(customer=self.fixture.customer)

        partition1 = factories.OfferingPartitionFactory(
            offering=self.offering, partition_name="shared-name"
        )
        partition2 = factories.OfferingPartitionFactory(
            offering=other_offering, partition_name="shared-name"
        )

        self.assertNotEqual(partition1.offering, partition2.offering)
        self.assertEqual(partition1.partition_name, partition2.partition_name)

    def test_slurm_field_mappings(self):
        """Test that SLURM-specific fields are properly stored."""
        partition = factories.OfferingPartitionFactory(
            cpu_bind=1,
            def_cpu_per_gpu=4,
            max_cpus_per_node=128,
            max_cpus_per_socket=64,
            def_mem_per_cpu=2048,
            def_mem_per_gpu=8192,
            def_mem_per_node=131072,
            max_mem_per_cpu=4096,
            max_mem_per_node=262144,
            default_time=120,
            max_time=2880,
            grace_time=600,
            max_nodes=50,
            min_nodes=1,
            exclusive_topo=True,
            exclusive_user=False,
            priority_tier=100,
            qos="high",
            req_resv=True,
        )

        # Verify all fields are stored correctly
        self.assertEqual(partition.cpu_bind, 1)
        self.assertEqual(partition.def_cpu_per_gpu, 4)
        self.assertEqual(partition.max_cpus_per_node, 128)
        self.assertEqual(partition.max_cpus_per_socket, 64)
        self.assertEqual(partition.def_mem_per_cpu, 2048)
        self.assertEqual(partition.def_mem_per_gpu, 8192)
        self.assertEqual(partition.def_mem_per_node, 131072)
        self.assertEqual(partition.max_mem_per_cpu, 4096)
        self.assertEqual(partition.max_mem_per_node, 262144)
        self.assertEqual(partition.default_time, 120)
        self.assertEqual(partition.max_time, 2880)
        self.assertEqual(partition.grace_time, 600)
        self.assertEqual(partition.max_nodes, 50)
        self.assertEqual(partition.min_nodes, 1)
        self.assertTrue(partition.exclusive_topo)
        self.assertFalse(partition.exclusive_user)
        self.assertEqual(partition.priority_tier, 100)
        self.assertEqual(partition.qos, "high")
        self.assertTrue(partition.req_resv)

    def test_nullable_fields_can_be_null(self):
        """Test that optional fields can be None."""
        partition = models.OfferingPartition.objects.create(
            offering=self.offering, partition_name="minimal-partition"
        )

        # All SLURM configuration fields should allow None
        self.assertIsNone(partition.cpu_bind)
        self.assertIsNone(partition.def_cpu_per_gpu)
        self.assertIsNone(partition.max_cpus_per_node)
        self.assertIsNone(partition.max_cpus_per_socket)
        self.assertIsNone(partition.def_mem_per_cpu)
        self.assertIsNone(partition.def_mem_per_gpu)
        self.assertIsNone(partition.def_mem_per_node)
        self.assertIsNone(partition.max_mem_per_cpu)
        self.assertIsNone(partition.max_mem_per_node)
        self.assertIsNone(partition.default_time)
        self.assertIsNone(partition.max_time)
        self.assertIsNone(partition.grace_time)
        self.assertIsNone(partition.max_nodes)
        self.assertIsNone(partition.min_nodes)
        self.assertIsNone(partition.priority_tier)

        # Boolean fields should have default values
        self.assertFalse(partition.exclusive_topo)
        self.assertFalse(partition.exclusive_user)
        self.assertFalse(partition.req_resv)

        # String fields should allow empty
        self.assertEqual(partition.qos, "")


class OfferingPartitionSerializerTest(test.APITestCase):
    """Test OfferingPartition serializer functionality."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)

    def test_serializer_includes_all_fields(self):
        """Test that serializer includes all expected fields."""
        from waldur_mastermind.marketplace.serializers import (
            OfferingPartitionSerializer,
        )

        partition = factories.OfferingPartitionFactory(offering=self.offering)
        serializer = OfferingPartitionSerializer(partition)

        expected_fields = {
            "uuid",
            "created",
            "modified",
            "offering",
            "offering_name",
            "partition_name",
            "cpu_arch",
            "gpu_arch",
            "cpu_bind",
            "def_cpu_per_gpu",
            "max_cpus_per_node",
            "max_cpus_per_socket",
            "def_mem_per_cpu",
            "def_mem_per_gpu",
            "def_mem_per_node",
            "max_mem_per_cpu",
            "max_mem_per_node",
            "default_time",
            "max_time",
            "grace_time",
            "max_nodes",
            "min_nodes",
            "exclusive_topo",
            "exclusive_user",
            "priority_tier",
            "qos",
            "qos_options",
            "req_resv",
        }

        self.assertEqual(set(serializer.data.keys()), expected_fields)

    def test_offering_name_is_read_only(self):
        """Test that offering_name field is read-only."""
        from waldur_mastermind.marketplace.serializers import (
            OfferingPartitionSerializer,
        )

        partition = factories.OfferingPartitionFactory(offering=self.offering)
        serializer = OfferingPartitionSerializer(partition)

        self.assertEqual(serializer.data["offering_name"], self.offering.name)

    def test_offering_field_uses_uuid_slug(self):
        """Test that offering field uses UUID as slug."""
        from waldur_mastermind.marketplace.serializers import (
            OfferingPartitionSerializer,
        )

        partition = factories.OfferingPartitionFactory(offering=self.offering)
        serializer = OfferingPartitionSerializer(partition)

        self.assertEqual(str(serializer.data["offering"]), str(self.offering.uuid))


class OfferingPartitionFilterTest(test.APITestCase):
    """Test OfferingPartition filter functionality."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering1 = factories.OfferingFactory(
            customer=self.fixture.customer, name="Cluster-1"
        )
        self.offering2 = factories.OfferingFactory(
            customer=self.fixture.customer, name="Cluster-2"
        )

    def test_filter_by_offering_uuid(self):
        """Test filtering by offering UUID."""
        from waldur_mastermind.marketplace.filters import OfferingPartitionFilter

        partition1 = factories.OfferingPartitionFactory(offering=self.offering1)
        partition2 = factories.OfferingPartitionFactory(offering=self.offering2)

        queryset = models.OfferingPartition.objects.all()
        filter_instance = OfferingPartitionFilter(
            data={"offering_uuid": self.offering1.uuid.hex}, queryset=queryset
        )

        filtered_qs = filter_instance.qs
        self.assertIn(partition1, filtered_qs)
        self.assertNotIn(partition2, filtered_qs)

    def test_filter_by_offering_name(self):
        """Test filtering by offering name (case-insensitive)."""
        from waldur_mastermind.marketplace.filters import OfferingPartitionFilter

        partition1 = factories.OfferingPartitionFactory(offering=self.offering1)
        partition2 = factories.OfferingPartitionFactory(offering=self.offering2)

        queryset = models.OfferingPartition.objects.all()
        filter_instance = OfferingPartitionFilter(
            data={"offering_name": "cluster-1"},  # lowercase
            queryset=queryset,
        )

        filtered_qs = filter_instance.qs
        self.assertIn(partition1, filtered_qs)
        self.assertNotIn(partition2, filtered_qs)

    def test_filter_by_partition_name(self):
        """Test filtering by partition name (case-insensitive)."""
        from waldur_mastermind.marketplace.filters import OfferingPartitionFilter

        partition1 = factories.OfferingPartitionFactory(
            offering=self.offering1, partition_name="gpu-partition"
        )
        partition2 = factories.OfferingPartitionFactory(
            offering=self.offering1, partition_name="cpu-partition"
        )

        queryset = models.OfferingPartition.objects.all()
        filter_instance = OfferingPartitionFilter(
            data={"partition_name": "gpu"}, queryset=queryset
        )

        filtered_qs = filter_instance.qs
        self.assertIn(partition1, filtered_qs)
        self.assertNotIn(partition2, filtered_qs)

    def test_filter_by_priority_tier(self):
        """Test filtering by priority tier."""
        from waldur_mastermind.marketplace.filters import OfferingPartitionFilter

        partition1 = factories.OfferingPartitionFactory(
            offering=self.offering1, priority_tier=100
        )
        partition2 = factories.OfferingPartitionFactory(
            offering=self.offering1, priority_tier=50
        )

        queryset = models.OfferingPartition.objects.all()
        filter_instance = OfferingPartitionFilter(
            data={"priority_tier": 100}, queryset=queryset
        )

        filtered_qs = filter_instance.qs
        self.assertIn(partition1, filtered_qs)
        self.assertNotIn(partition2, filtered_qs)

    def test_filter_by_boolean_fields(self):
        """Test filtering by boolean fields."""
        from waldur_mastermind.marketplace.filters import OfferingPartitionFilter

        partition1 = factories.OfferingPartitionFactory(
            offering=self.offering1, exclusive_user=True
        )
        partition2 = factories.OfferingPartitionFactory(
            offering=self.offering1, exclusive_user=False
        )

        queryset = models.OfferingPartition.objects.all()
        filter_instance = OfferingPartitionFilter(
            data={"exclusive_user": True}, queryset=queryset
        )

        filtered_qs = filter_instance.qs
        self.assertIn(partition1, filtered_qs)
        self.assertNotIn(partition2, filtered_qs)

    def test_ordering_fields(self):
        """Test that ordering works for various fields."""
        from waldur_mastermind.marketplace.filters import OfferingPartitionFilter

        partition1 = factories.OfferingPartitionFactory(
            offering=self.offering1, partition_name="a-partition", priority_tier=100
        )
        factories.OfferingPartitionFactory(
            offering=self.offering1, partition_name="z-partition", priority_tier=50
        )

        queryset = models.OfferingPartition.objects.all()

        # Test ordering by partition name
        filter_instance = OfferingPartitionFilter(
            data={"o": "partition_name"}, queryset=queryset
        )
        ordered_qs = list(filter_instance.qs)
        self.assertEqual(ordered_qs[0], partition1)  # a-partition comes first

        # Test reverse ordering by priority_tier
        filter_instance = OfferingPartitionFilter(
            data={"o": "-priority_tier"}, queryset=queryset
        )
        ordered_qs = list(filter_instance.qs)
        self.assertEqual(ordered_qs[0], partition1)  # priority_tier 100 comes first

    def test_filter_by_cpu_arch(self):
        """Test filtering by CPU architecture."""
        from waldur_mastermind.marketplace.filters import OfferingPartitionFilter

        partition1 = factories.OfferingPartitionFactory(
            offering=self.offering1, cpu_arch="x86_64/amd/zen3"
        )
        partition2 = factories.OfferingPartitionFactory(
            offering=self.offering1, cpu_arch="aarch64/arm/neoverse_v1"
        )

        queryset = models.OfferingPartition.objects.all()
        filter_instance = OfferingPartitionFilter(
            data={"cpu_arch": "zen3"}, queryset=queryset
        )

        filtered_qs = filter_instance.qs
        self.assertIn(partition1, filtered_qs)
        self.assertNotIn(partition2, filtered_qs)

    def test_filter_by_gpu_arch(self):
        """Test filtering by GPU architecture."""
        from waldur_mastermind.marketplace.filters import OfferingPartitionFilter

        partition1 = factories.OfferingPartitionFactory(
            offering=self.offering1, gpu_arch="nvidia/cc90"
        )
        partition2 = factories.OfferingPartitionFactory(
            offering=self.offering1, gpu_arch="amd/gfx90a"
        )

        queryset = models.OfferingPartition.objects.all()
        filter_instance = OfferingPartitionFilter(
            data={"gpu_arch": "nvidia"}, queryset=queryset
        )

        filtered_qs = filter_instance.qs
        self.assertIn(partition1, filtered_qs)
        self.assertNotIn(partition2, filtered_qs)

    def test_filter_has_gpu_true(self):
        """Test filtering partitions that have GPU architecture."""
        from waldur_mastermind.marketplace.filters import OfferingPartitionFilter

        partition1 = factories.OfferingPartitionFactory(
            offering=self.offering1, gpu_arch="nvidia/cc90"
        )
        partition2 = factories.OfferingPartitionFactory(
            offering=self.offering1, gpu_arch=""
        )

        queryset = models.OfferingPartition.objects.all()
        filter_instance = OfferingPartitionFilter(
            data={"has_gpu": True}, queryset=queryset
        )

        filtered_qs = filter_instance.qs
        self.assertIn(partition1, filtered_qs)
        self.assertNotIn(partition2, filtered_qs)

    def test_filter_has_gpu_false(self):
        """Test filtering partitions without GPU architecture."""
        from waldur_mastermind.marketplace.filters import OfferingPartitionFilter

        partition1 = factories.OfferingPartitionFactory(
            offering=self.offering1, gpu_arch="nvidia/cc90"
        )
        partition2 = factories.OfferingPartitionFactory(
            offering=self.offering1, gpu_arch=""
        )

        queryset = models.OfferingPartition.objects.all()
        filter_instance = OfferingPartitionFilter(
            data={"has_gpu": False}, queryset=queryset
        )

        filtered_qs = filter_instance.qs
        self.assertNotIn(partition1, filtered_qs)
        self.assertIn(partition2, filtered_qs)

    def test_serializer_includes_arch_fields(self):
        """Test that cpu_arch and gpu_arch appear in serialized data."""
        from waldur_mastermind.marketplace.serializers import (
            OfferingPartitionSerializer,
        )

        partition = factories.OfferingPartitionFactory(
            offering=self.offering1,
            cpu_arch="x86_64/amd/zen3",
            gpu_arch="nvidia/cc90",
        )
        serializer = OfferingPartitionSerializer(partition)
        self.assertEqual(serializer.data["cpu_arch"], "x86_64/amd/zen3")
        self.assertEqual(serializer.data["gpu_arch"], "nvidia/cc90")
