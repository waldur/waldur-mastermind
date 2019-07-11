import mock
from rest_framework import status, test

from waldur_vmware.tests.utils import override_plugin_settings
from . import factories, fixtures


class VirtualMachineCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.fixture.spl
        self.fixture.customer_cluster
        self.url = factories.VirtualMachineFactory.get_list_url()

    def test_create_vm(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_cluster_settings_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        payload['cluster'] = factories.ClusterFactory.get_url()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'][0], 'This cluster is not available for this service.')

    def test_cluster_customer_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        cluster = factories.ClusterFactory(settings=self.fixture.settings)
        payload['cluster'] = factories.ClusterFactory.get_url(cluster)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'][0], 'This cluster is not available for this customer.')

    def test_default_cluster_id_is_defined(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options['default_cluster_id'] = self.fixture.cluster.backend_id
        self.fixture.settings.save(update_fields=['options'])
        payload = self.get_valid_payload()
        del payload['cluster']
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_default_cluster_id_is_not_defined(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload['cluster']
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'][0], 'Default cluster is not defined for this service.')

    def test_create_vm_with_network(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        network = self.fixture.network
        payload['networks'] = [{'url': factories.NetworkFactory.get_url(network)}]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['networks'][0]['uuid'], network.uuid.hex)

    def test_network_customer_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        network = factories.NetworkFactory(settings=self.fixture.settings)
        payload['networks'] = [{'url': factories.NetworkFactory.get_url(network)}]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'][0], 'This network is not available for this customer.')

    def test_network_settings_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        network = factories.NetworkFactory()
        payload['networks'] = [{'url': factories.NetworkFactory.get_url(network)}]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'][0], 'This network is not available for this service.')

    def test_create_vm_with_datastore(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        payload['datastore'] = factories.DatastoreFactory.get_url(self.fixture.datastore)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_datastore_customer_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        datastore = factories.DatastoreFactory(settings=self.fixture.settings)
        payload['datastore'] = factories.DatastoreFactory.get_url(datastore)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'][0], 'This datastore is not available for this customer.')

    def test_datastore_settings_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        datastore = factories.DatastoreFactory()
        payload['datastore'] = factories.DatastoreFactory.get_url(datastore)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'][0], 'This datastore is not available for this service.')

    def test_max_cpu_is_not_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options['max_cpu'] = 100
        self.fixture.settings.save(update_fields=['options'])
        payload = self.get_valid_payload()
        payload['cores'] = 10
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_cpu_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options['max_cpu'] = 100
        self.fixture.settings.save(update_fields=['options'])
        payload = self.get_valid_payload()
        payload['cores'] = 200
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_ram_is_not_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options['max_ram'] = 100
        self.fixture.settings.save(update_fields=['options'])
        payload = self.get_valid_payload()
        payload['ram'] = 10
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_ram_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options['max_ram'] = 100
        self.fixture.settings.save(update_fields=['options'])
        payload = self.get_valid_payload()
        payload['ram'] = 200
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_disk_is_not_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options['max_disk'] = 100
        self.fixture.settings.save(update_fields=['options'])
        self.fixture.template.disk = 10
        self.fixture.template.save()
        payload = self.get_valid_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_disk_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options['max_disk'] = 100
        self.fixture.settings.save(update_fields=['options'])
        self.fixture.template.disk = 200
        self.fixture.template.save()
        payload = self.get_valid_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def get_valid_payload(self):
        return {
            'name': 'VMware VM',
            'service_project_link': factories.VMwareServiceProjectLinkFactory.get_url(self.fixture.spl),
            'template': factories.TemplateFactory.get_url(self.fixture.template),
            'cluster': factories.ClusterFactory.get_url(self.fixture.cluster),
        }


class VirtualMachineBackendTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.client = mock.MagicMock()

    def create_vm(self, vm):
        backend = self.fixture.virtual_machine.get_backend()

        backend.client = self.client
        backend.client.deploy_vm_from_template.return_value = 'vm-01'
        backend.client.create_vm.return_value = 'vm-01'
        backend.client.get_vm.return_value = {
            'power_state': 'POWERED_OFF',
            'disks': []
        }

        backend.create_virtual_machine(vm)

    def test_customer_folder_is_used_for_vm_provisioning_from_template(self):
        # Arrange
        folder = self.fixture.customer_folder.folder
        vm = self.fixture.virtual_machine

        # Act
        self.create_vm(vm)

        # Assert
        spec = self.client.deploy_vm_from_template.mock_calls[0][1][1]
        self.assertEqual(spec['placement']['folder'], folder.backend_id)

    def test_customer_folder_is_used_for_vm_provisioning_from_scratch(self):
        # Arrange
        folder = self.fixture.customer_folder.folder
        vm = self.fixture.virtual_machine
        vm.template = None
        vm.save()

        # Act
        self.create_vm(vm)

        # Assert
        spec = self.client.create_vm.mock_calls[0][1][0]
        self.assertEqual(spec['placement']['folder'], folder.backend_id)

    @override_plugin_settings(VM_FOLDER='folder-01')
    def test_default_folder_is_used_otherwise(self):
        vm = self.fixture.virtual_machine

        # Act
        self.create_vm(vm)

        # Assert
        spec = self.client.deploy_vm_from_template.mock_calls[0][1][1]
        self.assertEqual(spec['placement']['folder'], 'folder-01')
