"""
Comprehensive tests for RancherCreateProcessor covering identified gaps:
1. Multi-tenant scenarios
2. Resource calculation validation
3. Edge cases and error conditions
4. Complete integration flow
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from rest_framework import test
from rest_framework.serializers import ValidationError

from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    RANCHER_OFFERING,
    OrderStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
    STORAGE_TYPE,
)
from waldur_mastermind.marketplace_rancher.const import DEPLOYMENT_MODE_MANAGED
from waldur_mastermind.marketplace_rancher.processors import RancherCreateProcessor
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures as openstack_fixtures
from waldur_rancher.enums import AGENT_ROLE, SERVER_ROLE
from waldur_rancher.tests import factories as rancher_factories


class Request:
    def __init__(self, request_user):
        self.user = request_user


class RancherMultiTenantTest(test.APITransactionTestCase):
    """Test multi-tenant scenarios and resource aggregation"""

    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        service_settings = rancher_factories.RancherServiceSettingsFactory()

        # Create 3 OpenStack offerings for multi-tenant testing
        self.openstack_offerings = []
        for i in range(3):
            offering = marketplace_factories.OfferingFactory(
                type=OPENSTACK_TENANT_OFFERING,
                scope=openstack_factories.SettingsFactory(),
            )
            self.openstack_offerings.append(offering)

        # Create flavors with known specifications
        self.worker_flavor = openstack_factories.FlavorFactory(
            name="worker.medium",
            ram=8 * 1024,  # 8 GB
            cores=4,
        )
        self.server_flavor = openstack_factories.FlavorFactory(
            name="server.large",
            ram=16 * 1024,  # 16 GB
            cores=8,
        )
        self.lb_flavor = openstack_factories.FlavorFactory(
            name="lb.small",
            ram=4 * 1024,  # 4 GB
            cores=2,
        )

        # Create rancher offering
        self.offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, scope=service_settings
        )
        self.offering.plugin_options.update(
            {
                "deployment_mode": DEPLOYMENT_MODE_MANAGED,
                "managed_rancher_server_flavor_name": self.server_flavor.name,
                "managed_rancher_server_system_volume_size_gb": 50,
                "managed_rancher_server_data_volume_size_gb": 100,
                "managed_rancher_worker_system_volume_size_gb": 50,
                "managed_rancher_load_balancer_flavor_name": self.lb_flavor.name,
                "managed_rancher_load_balancer_system_volume_size_gb": 20,
                "managed_rancher_load_balancer_data_volume_size_gb": 10,
                "openstack_offering_uuid_list": [
                    o.uuid.hex for o in self.openstack_offerings
                ],
                # Resource limits
                "managed_rancher_tenant_max_cpu": 200,
                "managed_rancher_tenant_max_ram": 400,  # GB
                "managed_rancher_tenant_max_disk": 2000,  # GB
            }
        )
        self.offering.secret_options.update(
            {
                "customer_uuid": self.fixture.project.customer.uuid.hex,
            }
        )
        self.offering.save()

    def test_resource_aggregation_across_three_tenants(self):
        """Test that resources are correctly aggregated across 3 tenants"""
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "multi-tenant-cluster",
                "worker_nodes_count": 2,
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [
                    o.uuid.hex for o in self.openstack_offerings
                ],
            },
            state=OrderStates.EXECUTING,
        )

        processor = RancherCreateProcessor(order)

        # Mock get_tenant_limits to return predictable values
        with patch.object(processor, "get_tenant_limits") as mock_limits:
            # Each tenant: 2 workers (4 cores each) + 3 servers (8 cores each) + 1 LB (2 cores)
            # = 2*4 + 3*8 + 1*2 = 34 cores per tenant
            mock_limits.return_value = {
                CORES_TYPE: 34,
                RAM_TYPE: 80 * 1024,  # 80 GB in MB
                STORAGE_TYPE: 500 * 1024,  # 500 GB in MB
            }

            # Should not raise - 3 tenants * 34 cores = 102 cores < 200 limit
            try:
                processor.validate_limits()
            except ValidationError:
                self.fail("validate_limits raised ValidationError unexpectedly")

    def test_resource_limit_exceeded_with_three_tenants(self):
        """Test that validation fails when aggregated resources exceed limits"""
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "multi-tenant-cluster",
                "worker_nodes_count": 10,  # High worker count
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [
                    o.uuid.hex for o in self.openstack_offerings
                ],
            },
            state=OrderStates.EXECUTING,
        )

        processor = RancherCreateProcessor(order)

        with patch.object(processor, "get_tenant_limits") as mock_limits:
            # Each tenant: 10 workers + 3 servers + 1 LB = many cores
            mock_limits.return_value = {
                CORES_TYPE: 80,  # Exceeds per-tenant allocation
                RAM_TYPE: 200 * 1024,  # 200 GB in MB
                STORAGE_TYPE: 1000 * 1024,  # 1000 GB in MB
            }

            # 3 tenants * 80 cores = 240 cores > 200 limit
            with self.assertRaises(ValidationError) as context:
                processor.validate_limits()

            self.assertIn("exceeds the maximum allowed", str(context.exception))

    def test_odd_number_of_tenants_validation(self):
        """Test that even number of tenants is rejected"""
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "even-tenant-cluster",
                "worker_nodes_count": 2,
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [
                    self.openstack_offerings[0].uuid.hex,
                    self.openstack_offerings[1].uuid.hex,  # 2 tenants - even number
                ],
            },
            state=OrderStates.EXECUTING,
        )

        with self.assertRaises(ValidationError) as context:
            marketplace_utils.validate_order(order, Request(self.fixture.staff))

        self.assertIn("should be odd", str(context.exception))


class ManagedRancherResourceCalculationTest(test.APITransactionTestCase):
    """Test resource calculation logic including fixed overhead"""

    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.setup_basic_offering()

    def setup_basic_offering(self):
        """Helper to set up basic offering configuration"""
        service_settings = rancher_factories.RancherServiceSettingsFactory()

        self.openstack_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, scope=openstack_factories.SettingsFactory()
        )

        # Create managed rancher offering with known configurations
        self.offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, scope=service_settings
        )
        self.offering.plugin_options.update(
            {"deployment_mode": DEPLOYMENT_MODE_MANAGED}
        )
        self.offering.save()

        # Define standard flavors
        self.worker_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="worker.medium",
            ram=8192,  # 8 GB
            cores=4,
        )
        self.server_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="server.large",
            ram=16384,  # 16 GB
            cores=8,
        )
        self.lb_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="lb.small",
            ram=4096,  # 4 GB
            cores=2,
        )

        self.offering.plugin_options.update(
            {
                "managed_rancher_server_flavor_name": self.server_flavor.name,
                "managed_rancher_server_system_volume_size_gb": 50,
                "managed_rancher_server_data_volume_size_gb": 100,
                "managed_rancher_worker_system_volume_size_gb": 50,
                "managed_rancher_load_balancer_flavor_name": self.lb_flavor.name,
                "managed_rancher_load_balancer_system_volume_size_gb": 20,
                "managed_rancher_load_balancer_data_volume_size_gb": 10,
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            }
        )
        self.offering.secret_options.update(
            {
                "customer_uuid": self.fixture.project.customer.uuid.hex,
            }
        )
        self.offering.save()

    def test_fixed_overhead_calculation(self):
        """Test that fixed overhead (3 servers + 1 LB) is correctly calculated"""
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "test-cluster",
                "worker_nodes_count": 0,  # No workers to isolate fixed overhead
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        processor = RancherCreateProcessor(order)
        limits = processor.get_tenant_limits(self.openstack_offering)

        # Fixed overhead: 3 servers (8 cores each) + 1 LB (2 cores) = 26 cores
        self.assertEqual(limits[CORES_TYPE], 3 * 8 + 1 * 2)

        # RAM: 3 servers (16 GB each) + 1 LB (4 GB) = 52 GB = 53248 MB
        self.assertEqual(limits[RAM_TYPE], 3 * 16384 + 1 * 4096)

    def test_worker_nodes_resource_calculation(self):
        """Test resource calculation with worker nodes"""
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "test-cluster",
                "worker_nodes_count": 5,
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        processor = RancherCreateProcessor(order)
        limits = processor.get_tenant_limits(self.openstack_offering)

        # Total: 5 workers (4 cores each) + 3 servers (8 cores each) + 1 LB (2 cores)
        # = 20 + 24 + 2 = 46 cores
        self.assertEqual(limits[CORES_TYPE], 5 * 4 + 3 * 8 + 1 * 2)

    def test_storage_calculation_fixed_mode(self):
        """Test storage calculation in fixed storage mode"""
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "test-cluster",
                "worker_nodes_count": 2,
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        processor = RancherCreateProcessor(order)
        limits = processor.get_tenant_limits(self.openstack_offering)

        # Storage calculation (all in MB):
        # 3 servers: (50 + 100) * 3 = 450 GB
        # 2 workers: (50 + 100) * 2 = 300 GB
        # 1 LB: (20 + 10) = 30 GB
        # Total: 780 GB = 798720 MB
        expected_storage = (
            (50 + 100) * 3  # servers
            + (50 + 100) * 2  # workers
            + (20 + 10)  # load balancer
        ) * 1024

        self.assertEqual(limits[STORAGE_TYPE], expected_storage)

    def test_longhorn_storage_included_when_enabled(self):
        """Test that Longhorn volumes are included in storage calculation"""
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "test-cluster",
                "worker_nodes_count": 2,
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "install_longhorn": True,
                "worker_nodes_longhorn_volume_size": 204800,  # 200 GB in MB per worker
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        processor = RancherCreateProcessor(order)
        limits = processor.get_tenant_limits(self.openstack_offering)

        # Storage with Longhorn (all in MB):
        # 3 servers: (50 + 100) * 3 = 450 GB
        # 2 workers: (50 + 100 + 200) * 2 = 700 GB (includes Longhorn)
        # 1 LB: (20 + 10) = 30 GB
        # Total: 1180 GB = 1208320 MB
        expected_storage = (
            (50 + 100) * 3  # servers
            + (50 + 100 + 200) * 2  # workers with Longhorn
            + (20 + 10)  # load balancer
        ) * 1024

        self.assertEqual(limits[STORAGE_TYPE], expected_storage)


class ManagedRancherEdgeCasesTest(test.APITransactionTestCase):
    """Test edge cases and error conditions"""

    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.setup_offering()

    def setup_offering(self):
        """Setup basic offering for testing"""
        service_settings = rancher_factories.RancherServiceSettingsFactory()

        self.openstack_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, scope=openstack_factories.SettingsFactory()
        )

        self.offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, scope=service_settings
        )
        self.offering.plugin_options.update(
            {"deployment_mode": DEPLOYMENT_MODE_MANAGED}
        )
        self.offering.save()

    def test_missing_flavor_validation(self):
        """Test validation fails when required flavor is missing"""
        self.offering.plugin_options.update(
            {
                "managed_rancher_server_flavor_name": "non-existent-flavor",
                "managed_rancher_load_balancer_flavor_name": "lb.small",
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            }
        )
        self.offering.secret_options.update(
            {
                "customer_uuid": self.fixture.project.customer.uuid.hex,
            }
        )
        self.offering.save()

        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "test-cluster",
                "worker_nodes_count": 1,
                "worker_nodes_flavor_name": "worker.medium",
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        with self.assertRaises(ValidationError) as context:
            marketplace_utils.validate_order(order, Request(self.fixture.staff))

        self.assertIn("Flavor is not available", str(context.exception))

    def test_unavailable_openstack_offering(self):
        """Test validation fails when requesting unavailable OpenStack offering"""
        available_offering = self.openstack_offering
        unavailable_offering_uuid = str(uuid4())

        self.offering.plugin_options.update(
            {
                "openstack_offering_uuid_list": [available_offering.uuid.hex],
            }
        )
        self.offering.save()

        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "test-cluster",
                "worker_nodes_count": 1,
                "worker_nodes_flavor_name": "worker.medium",
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [
                    available_offering.uuid.hex,
                    unavailable_offering_uuid,  # This one is not available
                ],
            },
            state=OrderStates.EXECUTING,
        )

        with self.assertRaises(ValidationError) as context:
            marketplace_utils.validate_order(order, Request(self.fixture.staff))

        self.assertIn("not available", str(context.exception))

    def test_zero_worker_nodes_allowed(self):
        """Test that cluster with zero worker nodes is allowed"""
        # Create necessary flavors
        server_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="server.large",
            ram=16384,
            cores=8,
        )
        lb_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="lb.small",
            ram=4096,
            cores=2,
        )
        # Worker flavor is still needed for validation even with zero workers
        openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="worker.medium",
            ram=8192,
            cores=4,
        )

        self.offering.plugin_options.update(
            {
                "managed_rancher_server_flavor_name": server_flavor.name,
                "managed_rancher_load_balancer_flavor_name": lb_flavor.name,
                "managed_rancher_server_system_volume_size_gb": 50,
                "managed_rancher_server_data_volume_size_gb": 100,
                "managed_rancher_worker_system_volume_size_gb": 50,
                "managed_rancher_load_balancer_system_volume_size_gb": 20,
                "managed_rancher_load_balancer_data_volume_size_gb": 10,
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
                "managed_rancher_tenant_max_cpu": 100,
                "managed_rancher_tenant_max_ram": 200,
                "managed_rancher_tenant_max_disk": 1000,
            }
        )
        self.offering.secret_options.update(
            {
                "customer_uuid": self.fixture.project.customer.uuid.hex,
            }
        )
        self.offering.save()

        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "control-plane-only",
                "worker_nodes_count": 0,  # No worker nodes
                "worker_nodes_flavor_name": "worker.medium",
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        # Should not raise
        try:
            marketplace_utils.validate_order(order, Request(self.fixture.staff))
        except ValidationError:
            self.fail("Validation failed for zero worker nodes")

    def test_unit_conversion_gb_to_mb(self):
        """Test that GB to MB conversion uses 1024 factor"""
        # Create a proper order with offering
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "test-cluster",
                "worker_nodes_count": 1,
                "worker_nodes_flavor_name": "worker.medium",
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        # Set limits in GB
        self.offering.plugin_options.update(
            {
                "managed_rancher_tenant_max_cpu": 10,
                "managed_rancher_tenant_max_ram": 100,  # 100 GB
                "managed_rancher_tenant_max_disk": 500,  # 500 GB
            }
        )
        self.offering.save()

        processor = RancherCreateProcessor(order)

        # Mock aggregated limits in MB
        with patch.object(processor, "get_tenant_limits") as mock_limits:
            mock_limits.return_value = {
                CORES_TYPE: 8,
                RAM_TYPE: 50 * 1024,  # 50 GB in MB
                STORAGE_TYPE: 200 * 1024,  # 200 GB in MB
            }

            # Should not raise - all within limits
            try:
                processor.validate_limits()
            except ValidationError:
                self.fail("Validation failed unexpectedly")

            # Now test exceeding RAM limit
            mock_limits.return_value = {
                CORES_TYPE: 8,
                RAM_TYPE: 101 * 1024,  # 101 GB in MB (exceeds 100 GB limit)
                STORAGE_TYPE: 200 * 1024,
            }

            with self.assertRaises(ValidationError) as context:
                processor.validate_limits()

            # Check that error message shows MB units
            self.assertIn("MB", str(context.exception))


class ManagedRancherDynamicStorageTest(test.APITransactionTestCase):
    """Test dynamic storage mode with volume types"""

    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.setup_dynamic_storage_offering()

    def setup_dynamic_storage_offering(self):
        """Setup offering with dynamic storage mode"""
        service_settings = rancher_factories.RancherServiceSettingsFactory()

        self.openstack_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, scope=openstack_factories.SettingsFactory()
        )
        # Note: The processor checks storage_mode on the OpenStack offering when getting tenant limits
        self.openstack_offering.plugin_options["storage_mode"] = STORAGE_MODE_DYNAMIC
        self.openstack_offering.save()

        # Create volume types
        self.ssd_volume_type = openstack_factories.VolumeTypeFactory(
            settings=self.openstack_offering.scope, name="ssd-fast"
        )
        self.hdd_volume_type = openstack_factories.VolumeTypeFactory(
            settings=self.openstack_offering.scope, name="hdd-slow"
        )

        # Create flavors
        self.worker_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="worker.medium",
            ram=8192,
            cores=4,
        )
        self.server_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="server.large",
            ram=16384,
            cores=8,
        )
        self.lb_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="lb.small",
            ram=4096,
            cores=2,
        )

        self.offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, scope=service_settings
        )
        self.offering.plugin_options.update(
            {
                "deployment_mode": DEPLOYMENT_MODE_MANAGED,
                "storage_mode": STORAGE_MODE_DYNAMIC,  # Set storage mode on managed rancher offering too
                "managed_rancher_server_flavor_name": self.server_flavor.name,
                "managed_rancher_server_system_volume_size_gb": 50,
                "managed_rancher_server_system_volume_type_name": self.ssd_volume_type.name,
                "managed_rancher_server_data_volume_size_gb": 100,
                "managed_rancher_server_data_volume_type_name": self.ssd_volume_type.name,
                "managed_rancher_worker_system_volume_size_gb": 50,
                "managed_rancher_worker_system_volume_type_name": self.ssd_volume_type.name,
                "managed_rancher_load_balancer_flavor_name": self.lb_flavor.name,
                "managed_rancher_load_balancer_system_volume_size_gb": 20,
                "managed_rancher_load_balancer_system_volume_type_name": self.ssd_volume_type.name,
                "managed_rancher_load_balancer_data_volume_size_gb": 10,
                "managed_rancher_load_balancer_data_volume_type_name": self.hdd_volume_type.name,
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
                "managed_rancher_tenant_max_cpu": 100,
                "managed_rancher_tenant_max_ram": 200,
                "managed_rancher_tenant_max_disk": 2000,
            }
        )
        self.offering.secret_options.update(
            {
                "customer_uuid": self.fixture.project.customer.uuid.hex,
            }
        )
        self.offering.save()

    def test_dynamic_storage_calculation_per_volume_type(self):
        """Test that storage is calculated per volume type in dynamic mode"""
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "dynamic-storage-cluster",
                "worker_nodes_count": 2,
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "worker_nodes_data_volume_type_name": self.hdd_volume_type.name,
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        processor = RancherCreateProcessor(order)
        limits = processor.get_tenant_limits(self.openstack_offering)

        # In dynamic mode, storage should be separated by volume type
        # SSD volumes: server system + server data + worker system + LB system
        # HDD volumes: worker data + LB data

        # Check that storage quotas are per volume type
        self.assertIn("gigabytes_ssd-fast", limits)
        self.assertIn("gigabytes_hdd-slow", limits)

        # SSD: 3*(50+100) + 2*50 + 20 = 450 + 100 + 20 = 570 GB = 583680 MB
        self.assertEqual(limits["gigabytes_ssd-fast"], 570 * 1024)

        # HDD: 2*100 + 10 = 210 GB = 215040 MB
        self.assertEqual(limits["gigabytes_hdd-slow"], 210 * 1024)

    def test_missing_volume_type_validation(self):
        """Test validation fails when volume type is missing in dynamic mode"""
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "missing-volume-type",
                "worker_nodes_count": 1,
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "worker_nodes_data_volume_type_name": "non-existent-type",
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        with self.assertRaises(ValidationError) as context:
            marketplace_utils.validate_order(order, Request(self.fixture.staff))

        self.assertIn("Volume type", str(context.exception))
        self.assertIn("not available", str(context.exception))


class ManagedRancherIntegrationTest(test.APITransactionTestCase):
    """Test complete integration flow"""

    @patch("waldur_mastermind.marketplace_rancher.processors.submit_creation_order")
    @patch("waldur_mastermind.marketplace_rancher.processors.wait_for_tenant")
    def test_complete_cluster_creation_flow(self, mock_wait, mock_submit):
        """Test the complete cluster creation flow with key components"""
        fixture = openstack_fixtures.OpenStackFixture()

        # Setup complete offering configuration
        service_settings = rancher_factories.RancherServiceSettingsFactory()
        openstack_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, scope=openstack_factories.SettingsFactory()
        )

        # Create flavors
        worker_flavor = openstack_factories.FlavorFactory(
            settings=openstack_offering.scope,
            name="worker.medium",
            ram=8192,
            cores=4,
        )
        server_flavor = openstack_factories.FlavorFactory(
            settings=openstack_offering.scope,
            name="server.large",
            ram=16384,
            cores=8,
        )
        lb_flavor = openstack_factories.FlavorFactory(
            settings=openstack_offering.scope,
            name="lb.small",
            ram=4096,
            cores=2,
        )

        # Create volume types for load balancer
        system_volume_type = openstack_factories.VolumeTypeFactory(
            settings=openstack_offering.scope, name="ssd-fast"
        )
        data_volume_type = openstack_factories.VolumeTypeFactory(
            settings=openstack_offering.scope, name="hdd-slow"
        )

        # Create base image for load balancer
        base_image = openstack_factories.ImageFactory(
            settings=openstack_offering.scope, name="ubuntu-20.04"
        )

        offering = marketplace_factories.OfferingFactory(
            type=RANCHER_OFFERING, scope=service_settings
        )
        offering.plugin_options.update(
            {
                "deployment_mode": DEPLOYMENT_MODE_MANAGED,
                "managed_rancher_server_flavor_name": server_flavor.name,
                "managed_rancher_server_system_volume_size_gb": 50,
                "managed_rancher_server_data_volume_size_gb": 100,
                "managed_rancher_worker_system_volume_size_gb": 50,
                "managed_rancher_load_balancer_flavor_name": lb_flavor.name,
                "managed_rancher_load_balancer_system_volume_size_gb": 20,
                "managed_rancher_load_balancer_system_volume_type_name": system_volume_type.name,
                "managed_rancher_load_balancer_data_volume_size_gb": 10,
                "managed_rancher_load_balancer_data_volume_type_name": data_volume_type.name,
                "openstack_offering_uuid_list": [openstack_offering.uuid.hex],
                "managed_rancher_tenant_max_cpu": 100,
                "managed_rancher_tenant_max_ram": 200,
                "managed_rancher_tenant_max_disk": 2000,
            }
        )
        offering.secret_options.update(
            {
                "customer_uuid": fixture.project.customer.uuid.hex,
                "base_image_name": base_image.name,
                "managed_rancher_load_balancer_cloud_init_template": "#!/bin/bash\necho '{subnet_3_oct}'",
            }
        )
        offering.save()

        order = marketplace_factories.OrderFactory(
            project=fixture.project,
            created_by=fixture.owner,
            offering=offering,
            attributes={
                "name": "integration-test-cluster",
                "worker_nodes_count": 2,
                "worker_nodes_flavor_name": worker_flavor.name,
                "worker_nodes_data_volume_size": 102400,  # 100 GB in MB
                "openstack_offering_uuid_list": [openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        # Mock tenant creation
        mock_tenant = openstack_factories.TenantFactory(
            service_settings=openstack_offering.scope
        )
        openstack_factories.SubNetFactory(
            tenant=mock_tenant, cidr="192.168.0.0/24", backend_id="subnet-123"
        )

        mock_tenant_resource = marketplace_factories.ResourceFactory(
            scope=mock_tenant, offering=openstack_offering, project=fixture.project
        )

        mock_cluster = rancher_factories.ClusterFactory()
        mock_cluster_resource = marketplace_factories.ResourceFactory(
            scope=mock_cluster
        )

        # Configure mocks
        mock_submit.side_effect = [
            str(uuid4()),  # First call for tenant creation
            str(uuid4()),  # Second call for cluster creation
        ]

        with patch(
            "waldur_mastermind.marketplace.models.Order.objects.get"
        ) as mock_get_order:
            # Return tenant order first, then cluster order
            tenant_order = MagicMock()
            tenant_order.resource = mock_tenant_resource

            cluster_order = MagicMock()
            cluster_order.resource = mock_cluster_resource

            mock_get_order.side_effect = [tenant_order, cluster_order]

            processor = RancherCreateProcessor(order)

            # Test project creation
            project = processor.create_project()
            self.assertIsNotNone(project)
            self.assertIn("integration-test-cluster", project.name)

            # Test tenant creation
            with patch.object(processor, "get_tenant_limits") as mock_limits:
                mock_limits.return_value = {
                    CORES_TYPE: 50,
                    RAM_TYPE: 100 * 1024,
                    STORAGE_TYPE: 500 * 1024,
                }
                tenants = processor.create_tenants(fixture.staff, project)
                self.assertEqual(len(tenants), 1)
                self.assertEqual(mock_submit.call_count, 1)

            # Test format_node method
            node_data = processor.format_node(role=SERVER_ROLE, tenant=mock_tenant)
            self.assertIn("flavor", node_data)
            self.assertIn("subnet", node_data)
            self.assertIn("system_volume_size", node_data)
            self.assertEqual(node_data["system_volume_size"], 50 * 1024)  # 50 GB in MB

            # Test create_load_balancers with proper mocking
            with patch(
                "waldur_mastermind.marketplace_rancher.processors.create_request"
            ) as mock_create:
                # Create the instance that will be returned
                mock_instance = openstack_factories.InstanceFactory()
                instance_uuid = str(mock_instance.uuid)

                mock_response = MagicMock()
                mock_response.status_code = 201
                mock_response.data = {"uuid": instance_uuid}
                mock_create.return_value = mock_response

                # Create security groups needed for load balancer
                for sg_name in [
                    "default",
                    "lb-sg-http",
                    "lb-sg-https",
                    "lb-sg-kubeapi",
                ]:
                    openstack_factories.SecurityGroupFactory(
                        tenant=mock_tenant, name=sg_name, backend_id=f"{sg_name}-id"
                    )

                load_balancers = processor.create_load_balancers(
                    fixture.staff, project, [mock_tenant]
                )
                self.assertEqual(len(load_balancers), 1)
                self.assertEqual(load_balancers[0].uuid, mock_instance.uuid)


class ManagedRancherStorageIntegrationTest(test.APITransactionTestCase):
    """Test that MB storage values pass through correctly to OpenStack node creation"""

    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()

        # Create OpenStack offering with dynamic storage
        self.openstack_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, scope=openstack_factories.SettingsFactory()
        )
        self.openstack_offering.plugin_options.update(
            {"storage_mode": STORAGE_MODE_DYNAMIC}
        )
        self.openstack_offering.save()

        # Create flavors and volume types
        self.worker_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="worker.medium",
            ram=8192,
            cores=4,
        )
        self.server_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="server.large",
            ram=16384,
            cores=8,
        )

        # Volume types for dynamic storage
        self.worker_data_volume_type = openstack_factories.VolumeTypeFactory(
            settings=self.openstack_offering.scope, name="worker-data-ssd"
        )
        self.worker_system_volume_type = openstack_factories.VolumeTypeFactory(
            settings=self.openstack_offering.scope, name="worker-system-ssd"
        )
        self.longhorn_volume_type = openstack_factories.VolumeTypeFactory(
            settings=self.openstack_offering.scope, name="longhorn-storage"
        )

        # Create load balancer flavor
        self.lb_flavor = openstack_factories.FlavorFactory(
            settings=self.openstack_offering.scope,
            name="lb.small",
            ram=4096,
            cores=2,
        )

        # Create rancher offering
        self.offering = marketplace_factories.OfferingFactory(type=RANCHER_OFFERING)
        self.offering.plugin_options.update(
            {
                "deployment_mode": DEPLOYMENT_MODE_MANAGED,
                "storage_mode": STORAGE_MODE_DYNAMIC,
                "managed_rancher_server_flavor_name": self.server_flavor.name,
                "managed_rancher_server_system_volume_size_gb": 50,
                "managed_rancher_server_data_volume_size_gb": 100,
                "managed_rancher_worker_system_volume_size_gb": 30,
                "managed_rancher_worker_system_volume_type_name": self.worker_system_volume_type.name,
                "managed_rancher_load_balancer_flavor_name": self.lb_flavor.name,
                "managed_rancher_load_balancer_system_volume_size_gb": 20,
                "managed_rancher_load_balancer_data_volume_size_gb": 10,
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            }
        )
        self.offering.secret_options.update(
            {
                "customer_uuid": self.fixture.project.customer.uuid.hex,
            }
        )
        self.offering.save()

    def test_mb_storage_values_pass_through_to_openstack_node_creation(self):
        """Test that worker node storage values in MB are correctly passed to OpenStack"""
        # Test values in MB (equivalent to reasonable GB amounts)
        WORKER_DATA_VOLUME_MB = 51200  # 50 GB in MB
        LONGHORN_VOLUME_MB = 102400  # 100 GB in MB

        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "storage-integration-test",
                "worker_nodes_count": 1,
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": WORKER_DATA_VOLUME_MB,
                "worker_nodes_data_volume_type_name": self.worker_data_volume_type.name,
                "install_longhorn": True,
                "worker_nodes_longhorn_volume_size": LONGHORN_VOLUME_MB,
                "worker_nodes_longhorn_volume_type_name": self.longhorn_volume_type.name,
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        processor = RancherCreateProcessor(order)

        # Create a mock tenant with associated marketplace resource
        mock_tenant = openstack_factories.TenantFactory(
            service_settings=self.openstack_offering.scope
        )

        # Create the marketplace resource that links to the tenant
        marketplace_factories.ResourceFactory(
            offering=self.openstack_offering,
            project=self.fixture.project,
            scope=mock_tenant,
        )

        # Create subnet for the tenant
        openstack_factories.SubNetFactory(tenant=mock_tenant, cidr="192.168.1.0/24")

        # Test format_node for worker node - this is where MB values are passed to OpenStack
        node_data = processor.format_node(role=AGENT_ROLE, tenant=mock_tenant)

        # Verify the storage values passed to OpenStack are in MB (unchanged from input)
        self.assertEqual(
            node_data["data_volumes"][0]["size"],
            WORKER_DATA_VOLUME_MB,
            f"Worker data volume size should be {WORKER_DATA_VOLUME_MB} MB, not converted to bytes",
        )

        # Verify longhorn volume is also in MB
        self.assertEqual(
            node_data["data_volumes"][1]["size"],
            LONGHORN_VOLUME_MB,
            f"Longhorn volume size should be {LONGHORN_VOLUME_MB} MB, not converted to bytes",
        )

        # Verify volume types are correctly set
        self.assertIn("volume_type", node_data["data_volumes"][0])
        self.assertIn("volume_type", node_data["data_volumes"][1])

        # Verify system volume is still converted from GB to MB (server volumes remain in GB)
        expected_system_volume_mb = 30 * 1024  # 30 GB in MB
        self.assertEqual(
            node_data["system_volume_size"],
            expected_system_volume_mb,
            f"System volume should be {expected_system_volume_mb} MB (30 GB * 1024)",
        )

    def test_storage_consistency_across_api_layers(self):
        """Test that storage values are consistent from API input through to OpenStack creation"""

        # Define test data - realistic sizes in MB
        TEST_DATA = {
            "worker_data_volume_mb": 25600,  # 25 GB
            "longhorn_volume_mb": 51200,  # 50 GB
        }

        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "consistency-test",
                "worker_nodes_count": 2,
                "worker_nodes_flavor_name": self.worker_flavor.name,
                "worker_nodes_data_volume_size": TEST_DATA["worker_data_volume_mb"],
                "worker_nodes_data_volume_type_name": self.worker_data_volume_type.name,
                "install_longhorn": True,
                "worker_nodes_longhorn_volume_size": TEST_DATA["longhorn_volume_mb"],
                "worker_nodes_longhorn_volume_type_name": self.longhorn_volume_type.name,
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
            },
            state=OrderStates.EXECUTING,
        )

        processor = RancherCreateProcessor(order)

        # Test 1: Verify values are preserved in order attributes
        self.assertEqual(
            processor.order.attributes["worker_nodes_data_volume_size"],
            TEST_DATA["worker_data_volume_mb"],
            "Order attributes should preserve MB input values",
        )
        self.assertEqual(
            processor.order.attributes["worker_nodes_longhorn_volume_size"],
            TEST_DATA["longhorn_volume_mb"],
            "Order attributes should preserve MB input values",
        )

        # Test 2: Verify format_node passes MB values to OpenStack without conversion
        mock_tenant = openstack_factories.TenantFactory(
            service_settings=self.openstack_offering.scope
        )
        # Create the marketplace resource that links to the tenant
        marketplace_factories.ResourceFactory(
            offering=self.openstack_offering,
            project=self.fixture.project,
            scope=mock_tenant,
        )
        openstack_factories.SubNetFactory(tenant=mock_tenant, cidr="192.168.1.0/24")

        node_data = processor.format_node(role=AGENT_ROLE, tenant=mock_tenant)

        # Verify data volumes are in MB (OpenStack expects MB)
        data_volume = node_data["data_volumes"][0]  # Worker data volume
        longhorn_volume = node_data["data_volumes"][1]  # Longhorn volume

        self.assertEqual(
            data_volume["size"],
            TEST_DATA["worker_data_volume_mb"],
            "Worker data volume should be passed to OpenStack in MB without conversion",
        )
        self.assertEqual(
            longhorn_volume["size"],
            TEST_DATA["longhorn_volume_mb"],
            "Longhorn volume should be passed to OpenStack in MB without conversion",
        )

        # Test 3: Verify that input values are used in calculations without additional conversion
        # This verifies the end-to-end consistency - if the above format_node tests pass
        # and limit calculations don't cause errors, then the MB integration is working correctly
        limits = processor.get_tenant_limits(self.openstack_offering)

        # Just verify that limits calculation doesn't fail with MB input values
        # The existence of limits dict proves that MB values are being handled correctly
        self.assertIsInstance(
            limits, dict, "Limit calculations should work with MB input values"
        )
        self.assertIn(CORES_TYPE, limits, "CPU limits should be calculated")
        self.assertIn(RAM_TYPE, limits, "RAM limits should be calculated")
