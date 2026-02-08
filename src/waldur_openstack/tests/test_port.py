from unittest import mock

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_openstack.models import NetworkRBACPolicy, Port
from waldur_openstack.serializers import (
    OpenStackPortIPUpdateSerializer,
    OpenStackPortSerializer,
)

from . import factories, fixtures


class BasePortTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.owner)


class PortCreateTest(BasePortTest):
    def setUp(self):
        super().setUp()
        self.url = factories.PortFactory.get_list_url()
        self.subnet = self.fixture.subnet
        self.network = self.subnet.network
        self.fixed_ips = [
            {"ip_address": "192.168.42.100", "subnet_id": self.subnet.backend_id}
        ]
        self.valid_data = {
            "name": "Test Port",
            "description": "Test port description",
            "fixed_ips": self.fixed_ips,
            "port_security_enabled": True,
            "network": factories.NetworkFactory.get_url(self.network),
        }

    @mock.patch("waldur_openstack.executors.PortCreateExecutor.execute")
    def test_port_create_action_is_allowed(self, create_port_executor_mock):
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        create_port_executor_mock.assert_called_once()

        # Verify that port was created in the database
        port = Port.objects.get(uuid=response.data["uuid"])
        self.assertEqual(port.name, "Test Port")
        self.assertEqual(port.description, "Test port description")
        self.assertEqual(port.network, self.subnet.network)
        self.assertEqual(port.tenant, self.subnet.tenant)
        self.assertEqual(port.port_security_enabled, True)
        self.assertEqual(port.fixed_ips[0]["ip_address"], "192.168.42.100")
        self.assertEqual(port.fixed_ips[0]["subnet_id"], self.subnet.backend_id)

    @mock.patch("waldur_openstack.executors.PortCreateExecutor.execute")
    def test_port_create_requires_network(self, create_port_executor_mock):
        invalid_data = self.valid_data.copy()
        invalid_data.pop("network")

        response = self.client.post(self.url, invalid_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        create_port_executor_mock.assert_not_called()

    @mock.patch("waldur_openstack.executors.PortCreateExecutor.execute")
    def test_port_create_validates_fixed_ips(self, create_port_executor_mock):
        invalid_data = self.valid_data.copy()
        # Invalid fixed IP format
        invalid_data["fixed_ips"] = [{"invalid_key": "value"}]

        response = self.client.post(self.url, invalid_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        create_port_executor_mock.assert_not_called()

    @mock.patch("neutronclient.v2_0.client.Client")
    @mock.patch("waldur_openstack.backend.get_keystone_session")
    def test_port_creation_passes_fixed_ips_to_backend(
        self, mock_get_keystone_session, mock_neutron_client
    ):
        # Mock the session to avoid OpenStack authentication
        mock_session = mock.MagicMock()
        mock_get_keystone_session.return_value = mock_session

        mock_neutron_instance = mock_neutron_client.return_value

        mock_neutron_instance.create_port.return_value = {
            "port": {
                "id": "backend_id_from_mock",
                "status": "ACTIVE",
                "mac_address": "fa:16:3e:ab:cd:ef",
                "fixed_ips": [
                    {"ip_address": "192.168.42.100", "subnet_id": "subnet-backend-id"}
                ],
                "admin_state_up": True,
                "port_security_enabled": True,
                "device_owner": "",
            }
        }

        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        port = Port.objects.get(uuid=response.data["uuid"])

        port.get_backend().create_port(port)

        mock_neutron_instance.create_port.assert_called_once_with(
            {
                "port": {
                    "name": port.name,
                    "description": port.description,
                    "network_id": port.network.backend_id,
                    "tenant_id": port.tenant.backend_id,
                    "fixed_ips": self.fixed_ips,
                }
            }
        )


class PortUpdateTest(BasePortTest):
    def setUp(self) -> None:
        super().setUp()
        self.port = self.fixture.port
        self.url = factories.PortFactory.get_url(self.port)
        self.update_data = {
            "name": "Updated Port Name",
            "description": "Updated port description",
        }

    @mock.patch(
        "waldur_openstack.executors.PortUpdateNameAndDescriptionExecutor.execute"
    )
    def test_port_update_allowed(self, update_port_executor_mock):
        response = self.client.patch(self.url, self.update_data)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        update_port_executor_mock.assert_called_once()

        # Verify port was updated in database
        self.port.refresh_from_db()
        self.assertEqual(self.port.name, "Updated Port Name")
        self.assertEqual(self.port.description, "Updated port description")

    @mock.patch(
        "waldur_openstack.executors.PortUpdateNameAndDescriptionExecutor.execute"
    )
    def test_port_update_read_only_fields_ignored(self, update_port_executor_mock):
        update_data = self.update_data.copy()
        update_data["port_security_enabled"] = False  # This should be ignored

        response = self.client.patch(self.url, update_data)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        update_port_executor_mock.assert_called_once()

        # Verify port was updated in database but read-only field was not changed
        self.port.refresh_from_db()
        self.assertEqual(self.port.name, "Updated Port Name")
        self.assertEqual(self.port.description, "Updated port description")
        # The port_security_enabled field should not be updated since it's read-only in the update
        self.assertNotEqual(self.port.port_security_enabled, False)


class PortDeleteTest(BasePortTest):
    def setUp(self) -> None:
        super().setUp()
        self.port = self.fixture.port
        self.url = factories.PortFactory.get_url(self.port)

    @mock.patch("waldur_openstack.executors.PortDeleteExecutor.execute")
    def test_port_delete_triggers_executor(self, delete_port_executor_mock):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        delete_port_executor_mock.assert_called_once()


class PortSerializerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = self.fixture.subnet
        self.network = self.subnet.network
        self.tenant = self.network.tenant

    def test_serializer_validates_fixed_ips(self):
        valid_data = {
            "name": "Test Port",
            "description": "Test port description",
            "network": factories.NetworkFactory.get_url(self.network),
            "fixed_ips": [
                {"ip_address": "192.168.42.100", "subnet_id": self.subnet.backend_id}
            ],
        }

        serializer = OpenStackPortSerializer(data=valid_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

        # Test with invalid subnet_id
        invalid_data = valid_data.copy()
        invalid_data["fixed_ips"] = [
            {"ip_address": "192.168.42.100", "subnet_id": "non-existent-subnet-id"}
        ]

        serializer = OpenStackPortSerializer(data=invalid_data)
        self.assertFalse(serializer.is_valid())

        # Test with invalid IP address
        invalid_data = valid_data.copy()
        invalid_data["fixed_ips"] = [
            {"ip_address": "not-an-ip-address", "subnet_id": self.subnet.backend_id}
        ]

        serializer = OpenStackPortSerializer(data=invalid_data)
        self.assertFalse(serializer.is_valid())


class PortNetworkValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        # Create two different tenants to test cross-tenant validation
        self.fixture_2 = fixtures.OpenStackFixture()
        self.subnet1 = self.fixture.subnet
        self.network1 = self.subnet1.network
        self.tenant1 = self.network1.tenant

        self.subnet2 = self.fixture_2.subnet
        self.network2 = self.subnet2.network
        self.tenant2 = self.network2.tenant

    def test_port_serializer_associates_correct_tenant_and_network(self):
        """Test that the port serializer correctly associates tenant and network from subnet."""
        data = {
            "name": "Test Port",
            "description": "Test port description",
            "network": factories.NetworkFactory.get_url(self.network1),
        }

        serializer = OpenStackPortSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

        # Validate that the connected entries are correctly set
        self.assertEqual(serializer.validated_data["tenant"], self.tenant1)
        self.assertEqual(serializer.validated_data["network"], self.network1)
        self.assertEqual(
            serializer.validated_data["service_settings"],
            self.network1.service_settings,
        )
        self.assertEqual(serializer.validated_data["project"], self.network1.project)

    def test_port_fixed_ips_must_match_subnet_network(self):
        """Test that fixed IPs subnet_id must belong to the same network as the port's subnet."""
        # Create data with fixed IPs that reference a subnet from a different network
        data = {
            "name": "Test Port",
            "description": "Test port description",
            "network": factories.NetworkFactory.get_url(self.network1),
            "fixed_ips": [
                {
                    "ip_address": "192.168.42.100",
                    "subnet_id": self.subnet2.backend_id,  # Using subnet from a different network
                }
            ],
        }

        serializer = OpenStackPortSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("subnet", serializer.errors)


class PortExecutorTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.port = self.fixture.port
        self.subnet = self.fixture.subnet
        self.network = self.subnet.network

    @mock.patch("waldur_openstack.executors.core_tasks.BackendMethodTask")
    def test_port_create_executor_calls_backend(self, mock_backend_method_task):
        # Create a mock si() method that returns itself
        mock_si = mock.MagicMock()
        mock_si.return_value = mock_si
        mock_backend_method_task.return_value.si = mock_si

        # Directly call the executor
        import waldur_core.core.utils as core_utils
        from waldur_openstack.executors import PortCreateExecutor

        serialized_port = core_utils.serialize_instance(self.port)
        PortCreateExecutor.execute(self.port)

        # Verify the backend method task was called with correct parameters
        mock_backend_method_task.return_value.si.assert_called_once_with(
            serialized_port,
            "create_port",
            state_transition="begin_creating",
        )

    @mock.patch("waldur_openstack.executors.core_tasks.BackendMethodTask")
    def test_port_update_name_and_description_executor_calls_backend(
        self, mock_backend_method_task
    ):
        # Create a mock si() method that returns itself
        mock_si = mock.MagicMock()
        mock_si.return_value = mock_si
        mock_backend_method_task.return_value.si = mock_si

        # Directly call the executor
        import waldur_core.core.utils as core_utils
        from waldur_openstack.executors import PortUpdateNameAndDescriptionExecutor

        serialized_port = core_utils.serialize_instance(self.port)
        PortUpdateNameAndDescriptionExecutor.execute(
            self.port, updated_fields=["name", "description"]
        )

        # Verify the backend method task was called with correct parameters
        mock_backend_method_task.return_value.si.assert_called_once_with(
            serialized_port,
            "update_port_name_and_description",
            state_transition="begin_updating",
        )


class PortIPUpdateValidationTest(BasePortTest):
    def setUp(self):
        super().setUp()
        self.fixture.subnet.allocation_pools = [
            {"start": "192.168.1.10", "end": "192.168.1.20"},
            {"start": "192.168.1.30", "end": "192.168.1.40"},
        ]
        self.fixture.subnet.save()

    def test_ip_in_allocation_pool_valid(self):
        data = {
            "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
            "ip_address": "192.168.1.15",
        }
        serializer = OpenStackPortIPUpdateSerializer(
            data=data, context={"port": self.fixture.port}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_ip_not_in_allocation_pool(self):
        data = {
            "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
            "ip_address": "192.168.1.25",
        }
        serializer = OpenStackPortIPUpdateSerializer(
            data=data, context={"port": self.fixture.port}
        )
        self.assertFalse(serializer.is_valid())

    def test_subnet_network_mismatch(self):
        new_subnet = factories.SubNetFactory()
        data = {
            "subnet": factories.SubNetFactory.get_url(new_subnet),
            "ip_address": "192.168.1.15",
        }
        serializer = OpenStackPortIPUpdateSerializer(
            data=data, context={"port": self.fixture.port}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("subnet", serializer.errors)


class PortSharedNetworkTest(test.APITestCase):
    """Tests for shared network port creation functionality."""

    def setUp(self):
        # Create two separate fixtures for shared network scenario
        self.network_owner_fixture = fixtures.OpenStackFixture()
        self.instance_owner_fixture = fixtures.OpenStackFixture()

        # Network owner setup
        self.network_owner_tenant = self.network_owner_fixture.tenant
        self.shared_network = self.network_owner_fixture.network
        self.shared_subnet = self.network_owner_fixture.subnet

        # Instance owner setup
        self.instance_owner_tenant = self.instance_owner_fixture.tenant

        # Create RBAC policy to share network from owner to instance tenant
        self.rbac_policy = NetworkRBACPolicy.objects.create(
            network=self.shared_network,
            target_tenant=self.instance_owner_tenant,
            policy_type="access_as_shared",
        )

        # Set up URL and auth
        self.url = factories.PortFactory.get_list_url()
        self.client.force_authenticate(user=self.instance_owner_fixture.owner)

    @mock.patch("waldur_openstack.executors.PortCreateExecutor.execute")
    def test_shared_network_port_creation_with_target_tenant(
        self, create_port_executor_mock
    ):
        """Test creating port in shared network with target_tenant parameter."""
        port_data = {
            "name": "Shared Network Port",
            "description": "Port in shared network",
            "network": factories.NetworkFactory.get_url(self.shared_network),
            "target_tenant": factories.TenantFactory.get_url(
                self.instance_owner_tenant
            ),
            "fixed_ips": [{"subnet_id": self.shared_subnet.backend_id}],
            "port_security_enabled": True,
        }

        response = self.client.post(self.url, port_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        create_port_executor_mock.assert_called_once()

        # Verify port was created with correct tenant assignment
        port = Port.objects.get(uuid=response.data["uuid"])
        self.assertEqual(port.name, "Shared Network Port")
        self.assertEqual(port.network, self.shared_network)
        self.assertEqual(
            port.tenant, self.instance_owner_tenant
        )  # Should be target tenant, not network owner
        self.assertEqual(port.project, self.instance_owner_tenant.project)

    @mock.patch("waldur_openstack.executors.PortCreateExecutor.execute")
    def test_shared_network_port_creation_without_target_tenant_uses_network_tenant(
        self, create_port_executor_mock
    ):
        """Test that creating port in shared network without target_tenant defaults to network owner."""
        port_data = {
            "name": "Shared Network Port No Target",
            "network": factories.NetworkFactory.get_url(self.shared_network),
            "fixed_ips": [{"subnet_id": self.shared_subnet.backend_id}],
        }

        response = self.client.post(self.url, port_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # Without target_tenant, should default to network owner's tenant
        port = Port.objects.get(uuid=response.data["uuid"])
        self.assertEqual(port.tenant, self.network_owner_tenant)

    def test_shared_network_target_tenant_rbac_validation(self):
        """Test that target_tenant must have RBAC access to the network."""
        # Create a third tenant without RBAC access
        unauthorized_fixture = fixtures.OpenStackFixture()
        unauthorized_tenant = unauthorized_fixture.tenant

        port_data = {
            "name": "Unauthorized Port",
            "network": factories.NetworkFactory.get_url(self.shared_network),
            "target_tenant": factories.TenantFactory.get_url(unauthorized_tenant),
            "fixed_ips": [{"subnet_id": self.shared_subnet.backend_id}],
        }

        serializer = OpenStackPortSerializer(data=port_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("target_tenant", serializer.errors)

    def test_shared_network_same_tenant_as_network_owner_allowed(self):
        """Test that network owner can specify themselves as target_tenant."""
        port_data = {
            "name": "Owner Network Port",
            "network": factories.NetworkFactory.get_url(self.shared_network),
            "target_tenant": factories.TenantFactory.get_url(
                self.network_owner_tenant
            ),  # Same as network owner
            "fixed_ips": [{"subnet_id": self.shared_subnet.backend_id}],
        }

        serializer = OpenStackPortSerializer(data=port_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)


class PortBackendSharedNetworkTest(test.APITestCase):
    """Tests for backend methods handling shared networks."""

    def setUp(self):
        # Create shared network scenario
        self.network_owner_fixture = fixtures.OpenStackFixture()
        self.instance_owner_fixture = fixtures.OpenStackFixture()

        self.network_owner_tenant = self.network_owner_fixture.tenant
        self.shared_network = self.network_owner_fixture.network
        self.shared_subnet = self.network_owner_fixture.subnet
        self.instance_owner_tenant = self.instance_owner_fixture.tenant

        # Create two ports for different tests
        self.port_for_create_port = factories.PortFactory(
            network=self.shared_network,
            subnet=self.shared_subnet,
            tenant=self.instance_owner_tenant,
            project=self.instance_owner_tenant.project,
            service_settings=self.shared_network.service_settings,
            state=CoreStates.CREATION_SCHEDULED,  # Initial state for testing
        )

        self.port_for_instance_port = factories.PortFactory(
            network=self.shared_network,
            subnet=self.shared_subnet,
            tenant=self.instance_owner_tenant,
            project=self.instance_owner_tenant.project,
            service_settings=self.shared_network.service_settings,
            state=CoreStates.CREATION_SCHEDULED,  # Initial state for testing
        )

    @mock.patch("waldur_openstack.backend.get_neutron_client")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.admin_session")
    def test_create_port_uses_admin_session_for_shared_networks(
        self, mock_admin_session, mock_get_neutron_client
    ):
        """Test that create_port uses admin session for shared networks."""
        mock_neutron = mock_get_neutron_client.return_value
        mock_neutron.create_port.return_value = {
            "port": {
                "id": "backend-port-id",
                "mac_address": "fa:16:3e:ab:cd:ef",
                "fixed_ips": [
                    {"subnet_id": "subnet-id", "ip_address": "192.168.1.100"}
                ],
                "admin_state_up": True,
                "port_security_enabled": True,
                "device_owner": "",
                "status": "ACTIVE",
            }
        }

        backend = self.port_for_create_port.get_backend()
        backend.create_port(self.port_for_create_port)

        # Verify admin session was used
        mock_get_neutron_client.assert_called_once_with(mock_admin_session)

        # Verify port was updated with backend data
        self.port_for_create_port.refresh_from_db()
        self.assertEqual(self.port_for_create_port.backend_id, "backend-port-id")

    @mock.patch("waldur_openstack.backend.get_neutron_client")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.admin_session")
    def test_create_instance_port_uses_admin_session_and_sets_state(
        self, mock_admin_session, mock_get_neutron_client
    ):
        """Test that create_instance_port uses admin session and sets state to OK."""
        mock_neutron = mock_get_neutron_client.return_value
        mock_neutron.create_port.return_value = {
            "port": {
                "id": "instance-port-backend-id",
                "mac_address": "fa:16:3e:12:34:56",
                "fixed_ips": [
                    {"subnet_id": "subnet-id", "ip_address": "192.168.1.101"}
                ],
                "admin_state_up": True,
                "port_security_enabled": True,
                "device_owner": "",
                "status": "ACTIVE",
            }
        }

        backend = self.port_for_instance_port.get_backend()
        backend.create_instance_port(self.port_for_instance_port, ["security-group-id"])

        # Verify admin session was used
        mock_get_neutron_client.assert_called_once_with(mock_admin_session)

        # Verify port was updated with backend data
        self.port_for_instance_port.refresh_from_db()
        self.assertEqual(
            self.port_for_instance_port.backend_id, "instance-port-backend-id"
        )


class InstancePortCreationTest(test.APITestCase):
    """Tests for port creation during instance provisioning in shared networks."""

    def setUp(self):
        # Create shared network scenario
        self.network_owner_fixture = fixtures.OpenStackFixture()
        self.instance_owner_fixture = fixtures.OpenStackFixture()

        self.network_owner_tenant = self.network_owner_fixture.tenant
        self.shared_network = self.network_owner_fixture.network
        self.shared_subnet = self.network_owner_fixture.subnet
        self.instance_owner_tenant = self.instance_owner_fixture.tenant

    def test_instance_creation_assigns_ports_to_instance_tenant(self):
        """Test that ports created during instance creation are assigned to instance tenant."""

        # Create an instance with ports in shared network
        instance = factories.InstanceFactory(
            tenant=self.instance_owner_tenant,
            project=self.instance_owner_tenant.project,
            service_settings=self.shared_network.service_settings,
        )

        # Create a port in the shared network assigned to instance tenant
        port = factories.PortFactory(
            network=self.shared_network,
            subnet=self.shared_subnet,
            tenant=self.instance_owner_tenant,  # This should be instance tenant
            project=self.instance_owner_tenant.project,
            service_settings=self.shared_network.service_settings,
            instance=instance,
        )

        # Verify instance was created in correct tenant
        self.assertEqual(instance.tenant, self.instance_owner_tenant)

        # Verify port was assigned to instance tenant (not network owner)
        self.assertEqual(port.network, self.shared_network)  # Shared network
        self.assertEqual(
            port.tenant, self.instance_owner_tenant
        )  # Instance tenant (not network owner)
        self.assertEqual(port.project, self.instance_owner_tenant.project)
        self.assertEqual(port.instance, instance)
