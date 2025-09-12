from unittest import mock

from rest_framework import status, test

from waldur_openstack.models import Port
from waldur_openstack.serializers import (
    OpenStackPortIPUpdateSerializer,
    OpenStackPortSerializer,
)

from . import factories, fixtures


class BasePortTest(test.APITransactionTestCase):
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

    @mock.patch("waldur_openstack.session.neutron_client.Client")
    @mock.patch("waldur_openstack.backend.get_tenant_session")
    def test_port_creation_passes_fixed_ips_to_backend(
        self, mock_get_tenant_session, mock_get_neutron_client
    ):
        mock_neutron_instance = mock_get_neutron_client.return_value

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


class PortSerializerTest(test.APITransactionTestCase):
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


class PortNetworkValidationTest(test.APITransactionTestCase):
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


class PortExecutorTest(test.APITransactionTestCase):
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
