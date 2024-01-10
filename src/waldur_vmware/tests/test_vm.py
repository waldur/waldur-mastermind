from unittest import mock

import ddt
from rest_framework import status, test

from waldur_vmware import models
from waldur_vmware.tests.utils import override_plugin_settings

from . import factories, fixtures


class VirtualMachineCreateBaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.url = factories.VirtualMachineFactory.get_list_url()

    def get_valid_payload(self):
        return {
            "name": "VMware VM",
            "service_settings": factories.VMwareServiceSettingsFactory.get_url(
                self.fixture.settings
            ),
            "project": factories.ProjectFactory.get_url(self.fixture.project),
            "template": factories.TemplateFactory.get_url(self.fixture.template),
            "cluster": factories.ClusterFactory.get_url(self.fixture.cluster),
            "datastore": factories.DatastoreFactory.get_url(self.fixture.datastore),
        }


class VirtualMachineClusterValidationTest(VirtualMachineCreateBaseTest):
    def test_create_vm(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_cluster_settings_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        payload["cluster"] = factories.ClusterFactory.get_url()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "This cluster is not available for this service.",
        )

    def test_cluster_customer_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        cluster = factories.ClusterFactory(settings=self.fixture.settings)
        payload["cluster"] = factories.ClusterFactory.get_url(cluster)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "This cluster is not available for this customer.",
        )

    def test_default_cluster_label_is_defined(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options[
            "default_cluster_label"
        ] = self.fixture.cluster.name
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload()
        del payload["cluster"]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_default_cluster_label_is_not_defined(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload["cluster"]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "Default cluster is not defined for this service.",
        )

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_cluster_is_matched_by_customer_and_settings(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload["cluster"]

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        vm = models.VirtualMachine.objects.get(uuid=response.data["uuid"])
        self.assertEqual(vm.cluster, self.fixture.cluster)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_be_at_least_one_cluster_for_customer_and_service(
        self,
    ):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload["cluster"]
        self.fixture.cluster.delete()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_not_be_multiple_clusters_for_the_same_customer_and_service(
        self,
    ):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload["cluster"]

        cluster = factories.ClusterFactory(settings=self.fixture.settings)
        factories.CustomerClusterFactory(
            cluster=cluster, customer=self.fixture.customer
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VirtualMachineNetworkValidationTest(VirtualMachineCreateBaseTest):
    def test_create_vm_with_network(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        network = self.fixture.network
        payload["networks"] = [{"url": factories.NetworkFactory.get_url(network)}]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_network_customer_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        network = factories.NetworkFactory(settings=self.fixture.settings)
        payload["networks"] = [{"url": factories.NetworkFactory.get_url(network)}]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "This network is not available for this customer.",
        )

    def test_network_settings_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        network = factories.NetworkFactory()
        payload["networks"] = [{"url": factories.NetworkFactory.get_url(network)}]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "This network is not available for this service.",
        )

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_network_is_matched_by_customer_and_settings(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        vm = models.VirtualMachine.objects.get(uuid=response.data["uuid"])
        self.assertEqual(vm.networks.get(), self.fixture.network)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_be_at_least_one_network_for_customer_and_service(
        self,
    ):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        self.fixture.network.delete()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_not_be_multiple_networks_for_the_same_customer_and_service(
        self,
    ):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()

        network = factories.NetworkFactory(settings=self.fixture.settings)
        factories.CustomerNetworkFactory(
            network=network, customer=self.fixture.customer
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VirtualMachineDatastoreValidationTest(VirtualMachineCreateBaseTest):
    def test_create_vm_with_datastore(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        payload["datastore"] = factories.DatastoreFactory.get_url(
            self.fixture.datastore
        )
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_datastore_customer_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        datastore = factories.DatastoreFactory(settings=self.fixture.settings)
        payload["datastore"] = factories.DatastoreFactory.get_url(datastore)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "This datastore is not available for this customer.",
        )

    def test_datastore_settings_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        datastore = factories.DatastoreFactory()
        payload["datastore"] = factories.DatastoreFactory.get_url(datastore)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "This datastore is not available for this service.",
        )

    def test_datastore_size_validation(self):
        self.fixture.template.disk = 200
        self.fixture.template.save()
        self.fixture.datastore.free_space = 100
        self.fixture.datastore.save()

        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        payload["datastore"] = factories.DatastoreFactory.get_url(
            self.fixture.datastore
        )
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "There is no datastore with enough free space available for current customer.",
        )

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_datastore_is_matched_by_customer_and_settings(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload["datastore"]

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        vm = models.VirtualMachine.objects.get(uuid=response.data["uuid"])
        self.assertEqual(vm.datastore, self.fixture.datastore)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_be_at_least_one_datastore_for_customer_and_service(
        self,
    ):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload["datastore"]
        self.fixture.datastore.delete()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VirtualMachineFolderValidationTest(VirtualMachineCreateBaseTest):
    def test_folder_settings_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        payload["folder"] = factories.FolderFactory.get_url()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "This folder is not available for this service.",
        )

    def test_folder_customer_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        cluster = factories.FolderFactory(settings=self.fixture.settings)
        payload["folder"] = factories.FolderFactory.get_url(cluster)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "This folder is not available for this customer.",
        )

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_folder_is_matched_by_customer_and_settings(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        vm = models.VirtualMachine.objects.get(uuid=response.data["uuid"])
        self.assertEqual(vm.folder, self.fixture.folder)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_be_at_least_one_folder_for_customer_and_service(
        self,
    ):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        self.fixture.folder.delete()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VirtualMachineLimitsValidationTest(VirtualMachineCreateBaseTest):
    def test_max_cpu_is_not_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_cpu"] = 100
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload()
        payload["cores"] = 10
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_cpu_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_cpu"] = 100
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload()
        payload["cores"] = 200
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_cores_per_socket_is_not_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_cores_per_socket"] = 100
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload()
        payload["cores_per_socket"] = 10
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_cores_per_socket_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_cores_per_socket"] = 100
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload()
        payload["cores_per_socket"] = 200
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_ram_is_not_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_ram"] = 100 * 1024
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload()
        payload["ram"] = 10 * 1024
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_ram_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_ram"] = 100
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload()
        payload["ram"] = 200
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_disk_is_not_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_disk"] = 100
        self.fixture.settings.save(update_fields=["options"])
        self.fixture.template.disk = 10
        self.fixture.template.save()
        payload = self.get_valid_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_disk_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_disk"] = 100
        self.fixture.settings.save(update_fields=["options"])
        self.fixture.template.disk = 200
        self.fixture.template.save()
        payload = self.get_valid_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_disk_total_is_not_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_disk_total"] = 100
        self.fixture.settings.save(update_fields=["options"])
        self.fixture.template.disk = 10
        self.fixture.template.save()
        payload = self.get_valid_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_disk_total_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_disk_total"] = 100
        self.fixture.settings.save(update_fields=["options"])
        self.fixture.template.disk = 200
        self.fixture.template.save()
        payload = self.get_valid_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VirtualMachineDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.vm = self.fixture.virtual_machine
        self.url = factories.VirtualMachineFactory.get_url(self.vm)

    def test_when_vm_is_powered_off_deletion_is_allowed(self):
        self.vm.runtime_state = models.VirtualMachine.RuntimeStates.POWERED_OFF
        self.vm.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_when_vm_is_powered_on_deletion_is_not_allowed(self):
        self.vm.runtime_state = models.VirtualMachine.RuntimeStates.POWERED_ON
        self.vm.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class VirtualMachineBackendTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.client = mock.MagicMock()

    def create_vm(self, vm):
        backend = self.fixture.virtual_machine.get_backend()

        backend.client = self.client
        backend.client.deploy_vm_from_template.return_value = "vm-01"
        backend.client.create_vm.return_value = "vm-01"
        backend.client.get_vm.return_value = {"power_state": "POWERED_OFF", "disks": []}

        backend.create_virtual_machine(vm)

    def test_folder_is_used_for_vm_provisioning_from_template(self):
        # Arrange
        vm = self.fixture.virtual_machine
        vm.folder = self.fixture.folder
        vm.save()

        # Act
        self.create_vm(vm)

        # Assert
        spec = self.client.deploy_vm_from_template.mock_calls[0][1][1]
        self.assertEqual(spec["placement"]["folder"], self.fixture.folder.backend_id)

    def test_if_folder_is_not_specified_default_folder_is_found(self):
        # Arrange
        vm = self.fixture.virtual_machine
        vm.folder = None
        vm.save()
        self.client.list_folders.return_value = [
            {"name": "string", "folder": "obj-103"},
            {"name": "string", "folder": "obj-104"},
        ]

        # Act
        self.create_vm(vm)

        # Assert
        spec = self.client.deploy_vm_from_template.mock_calls[0][1][1]
        self.assertEqual(spec["placement"]["folder"], "obj-103")

    def test_cluster_is_used_for_vm_provisioning_from_template(self):
        # Arrange
        vm = self.fixture.virtual_machine

        # Act
        self.create_vm(vm)

        # Assert
        spec = self.client.deploy_vm_from_template.mock_calls[0][1][1]
        self.assertEqual(spec["placement"]["cluster"], self.fixture.cluster.backend_id)

    def test_if_cluster_is_not_specified_default_resource_pool_is_found(self):
        # Arrange
        vm = self.fixture.virtual_machine
        vm.cluster = None
        vm.save()
        self.client.list_resource_pools.return_value = [
            {"name": "string", "resource_pool": "obj-103"},
            {"name": "string", "resource_pool": "obj-104"},
        ]

        # Act
        self.create_vm(vm)

        # Assert
        spec = self.client.deploy_vm_from_template.mock_calls[0][1][1]
        self.assertEqual(spec["placement"]["resource_pool"], "obj-103")

    def test_nic_is_overridden_for_template(self):
        # Arrange
        vm = self.fixture.virtual_machine
        self.client.get_template_library_item.return_value = {
            "nics": [
                {
                    "key": "obj-103",
                    "value": {
                        "backing_type": "STANDARD_PORTGROUP",
                        "mac_type": "MANUAL",
                        "network": "obj-103",
                    },
                }
            ]
        }
        vm.networks.add(self.fixture.network)

        # Act
        self.create_vm(vm)

        # Assert
        spec = self.client.deploy_vm_from_template.mock_calls[0][1][1]
        self.assertEqual(
            spec["hardware_customization"]["nics"],
            [{"key": "obj-103", "value": {"network": self.fixture.network.backend_id}}],
        )

    def test_nic_is_created_from_scratch(self):
        # Arrange
        vm = self.fixture.virtual_machine
        vm.template = None
        vm.networks.add(self.fixture.network)
        vm.save()

        # Act
        self.create_vm(vm)

        # Assert
        self.client.create_nic.assert_called_once_with(
            "vm-01", self.fixture.network.backend_id
        )


class NetworkPortCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.url = factories.VirtualMachineFactory.get_url(
            self.fixture.virtual_machine, "create_port"
        )

    def test_if_customer_network_pair_does_not_exist_port_can_not_be_created(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url,
            {
                "name": "Test",
                "network": factories.NetworkFactory.get_url(self.fixture.network),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_if_customer_network_pair_exists_port_can_be_created(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.customer_network_pair
        response = self.client.post(
            self.url,
            {
                "name": "Test",
                "network": factories.NetworkFactory.get_url(self.fixture.network),
            },
        )
        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.content
        )

    def test_vm_can_have_at_most_10_network_adapters(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.customer_network_pair
        factories.PortFactory.create_batch(
            10,
            vm=self.fixture.virtual_machine,
            network=self.fixture.network,
            service_settings=self.fixture.virtual_machine.service_settings,
            project=self.fixture.virtual_machine.project,
        )
        response = self.client.post(
            self.url,
            {
                "name": "Test",
                "network": factories.NetworkFactory.get_url(self.fixture.network),
            },
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.content
        )


@ddt.ddt
class GuestPowerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.vm = self.fixture.virtual_machine

    @ddt.data("reboot_guest", "shutdown_guest")
    def test_if_vm_tools_are_running_guest_power_management_is_allowed(self, action):
        # Arrange
        self.vm.tools_state = models.VirtualMachine.ToolsStates.RUNNING
        self.vm.save()

        # Act
        self.client.force_authenticate(self.fixture.owner)
        url = factories.VirtualMachineFactory.get_url(self.vm, action)
        response = self.client.post(url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    @ddt.data("reboot_guest", "shutdown_guest")
    def test_if_vm_tools_are_not_running_guest_power_management_is_not_allowed(
        self, action
    ):
        # Arrange
        self.vm.tools_state = models.VirtualMachine.ToolsStates.NOT_RUNNING
        self.vm.save()

        # Act
        self.client.force_authenticate(self.fixture.owner)
        url = factories.VirtualMachineFactory.get_url(self.vm, action)
        response = self.client.post(url)

        # Assert
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
