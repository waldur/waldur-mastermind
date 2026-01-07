from unittest import mock

from django.http import QueryDict
from rest_framework import test

from waldur_openstack import executors, models, serializers
from waldur_openstack.tests import factories, fixtures


class TenantSerializerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.settings = self.fixture.settings
        self.project = self.fixture.project

    def test_create_tenant_skips_default_subnet_creation(self):
        validated_data = {
            "name": "test-tenant",
            "service_settings": self.settings,
            "project": self.project,
            "skip_creation_of_default_subnet": True,
            "subnet_cidr": "192.168.42.0/24",
            "security_groups": [],
        }

        # We need to ensure 'user_username' key exists or is handled. The serializer handles defaults in create?
        # No, defaults are handled during validation. We need to manually set defaults if skipping validation.
        validated_data["user_username"] = "test-user"
        validated_data["user_password"] = "password"

        request = mock.Mock()
        request.user = self.fixture.owner
        request.query_params = QueryDict()

        serializer = serializers.OpenStackTenantSerializer(context={"request": request})
        tenant = serializer.create(validated_data)

        self.assertEqual(models.Network.objects.filter(tenant=tenant).count(), 0)
        self.assertEqual(models.SubNet.objects.filter(tenant=tenant).count(), 0)

    def test_create_tenant_creates_default_subnet_by_default(self):
        validated_data = {
            "name": "test-tenant-default",
            "service_settings": self.settings,
            "project": self.project,
            "subnet_cidr": "192.168.42.0/24",
            "security_groups": [],
        }
        validated_data["user_username"] = "test-user-default"
        validated_data["user_password"] = "password"

        request = mock.Mock()
        request.user = self.fixture.owner
        request.query_params = QueryDict()

        serializer = serializers.OpenStackTenantSerializer(context={"request": request})
        tenant = serializer.create(validated_data)

        self.assertEqual(models.Network.objects.filter(tenant=tenant).count(), 1)
        self.assertEqual(models.SubNet.objects.filter(tenant=tenant).count(), 1)


class TenantRouterSkipTest(test.APITransactionTestCase):
    """Tests for skip_creation_of_default_router functionality."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant

    def test_skip_router_prevents_external_network_connection(self):
        """
        Test that when skip_creation_of_default_router=True,
        the system does not connect to external network even when
        external_network_id is configured in service settings.
        """
        # Configure external network ID in service settings (for auto-recovery)
        self.tenant.service_settings.options = {"external_network_id": "ext-net-123"}
        self.tenant.service_settings.save()

        # Enable skip_creation_of_default_router
        self.tenant.skip_creation_of_default_router = True
        self.tenant.save()

        # Create a network and subnet for the tenant
        network = factories.NetworkFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
        )
        factories.SubNetFactory(
            tenant=self.tenant,
            network=network,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
        )

        # Get the task chain
        task_chain = executors.get_tenant_create_tasks(self.tenant)

        # Extract all task signatures from the chain
        tasks = []
        current = task_chain
        while hasattr(current, "tasks"):
            tasks.extend(current.tasks)
            current = current.tasks[-1] if current.tasks else None

        # Verify that connect_tenant_to_external_network is NOT in the task chain
        external_network_tasks = [
            t
            for t in tasks
            if hasattr(t, "args")
            and "connect_tenant_to_external_network" in str(t.args)
        ]

        self.assertEqual(
            len(external_network_tasks),
            0,
            "External network connection task should be skipped when skip_creation_of_default_router=True",
        )

    def test_router_created_when_skip_flag_is_false(self):
        """
        Test that when skip_creation_of_default_router=False (default),
        the system creates routers and connects to external network when configured.
        """
        # Configure external network ID in service settings
        self.tenant.service_settings.options = {"external_network_id": "ext-net-456"}
        self.tenant.service_settings.save()

        # Ensure skip_creation_of_default_router is False (default)
        self.tenant.skip_creation_of_default_router = False
        self.tenant.save()

        # Create a router for the tenant
        factories.RouterFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
        )

        # Create a network and subnet for the tenant
        network = factories.NetworkFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
        )
        factories.SubNetFactory(
            tenant=self.tenant,
            network=network,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
        )

        # Get the task chain
        task_chain = executors.get_tenant_create_tasks(self.tenant)

        # Extract all task signatures from the chain
        tasks = []
        current = task_chain
        while hasattr(current, "tasks"):
            tasks.extend(current.tasks)
            current = current.tasks[-1] if current.tasks else None

        # Verify that connect_tenant_to_external_network IS in the task chain
        external_network_tasks = [
            t
            for t in tasks
            if hasattr(t, "args")
            and "connect_tenant_to_external_network" in str(t.args)
        ]

        self.assertGreater(
            len(external_network_tasks),
            0,
            "External network connection task should be present when skip_creation_of_default_router=False",
        )

    def test_backend_connect_router_respects_skip_flag(self):
        """
        Test that the backend connect_router method returns early
        when tenant has skip_creation_of_default_router=True.
        """
        # Enable skip_creation_of_default_router
        self.tenant.skip_creation_of_default_router = True
        self.tenant.save()

        backend = self.tenant.get_backend()

        # Call connect_router - it should return None due to early guard
        result = backend.connect_router(
            tenant=self.tenant,
            network_name="test-network",
            subnet_id="subnet-123",
            external=False,
            network_id="net-123",
        )

        # Should return None without calling the actual router creation logic
        self.assertIsNone(result)

    @mock.patch("waldur_openstack.backend.OpenStackBackend._get_router")
    @mock.patch("waldur_openstack.backend.OpenStackBackend._create_router")
    def test_backend_connect_router_creates_router_when_skip_flag_false(
        self, mock_create_router, mock_get_router
    ):
        """
        Test that the backend connect_router method creates router
        when tenant has skip_creation_of_default_router=False.
        """
        # Ensure skip_creation_of_default_router is False
        self.tenant.skip_creation_of_default_router = False
        self.tenant.save()

        # Mock _get_router to return None (no existing router)
        mock_get_router.return_value = None

        # Mock _create_router to return a router dict
        mock_create_router.return_value = {
            "id": "router-123",
            "name": "test-network-router",
        }

        backend = self.tenant.get_backend()

        # Mock the _connect_network_to_router method to avoid actual connection
        with mock.patch.object(backend, "_connect_network_to_router"):
            # Call connect_router
            result = backend.connect_router(
                tenant=self.tenant,
                network_name="test-network",
                subnet_id="subnet-123",
                external=False,
                network_id="net-123",
            )

        # Should return router ID
        self.assertEqual(result, "router-123")

        # _get_router should be called to check for existing router
        mock_get_router.assert_called_once_with(self.tenant)

        # _create_router should be called since no router exists
        mock_create_router.assert_called_once_with(self.tenant, "test-network-router")
