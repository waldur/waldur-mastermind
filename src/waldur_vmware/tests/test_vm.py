from rest_framework import status, test

from waldur_vmware.tests.utils import override_plugin_settings

from .. import models
from . import factories, fixtures


class VirtualMachineCreateBaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.fixture.spl
        self.fixture.customer_cluster
        self.url = factories.VirtualMachineFactory.get_list_url()

    def get_valid_payload(self):
        return {
            'name': 'VMware VM',
            'service_project_link': factories.VMwareServiceProjectLinkFactory.get_url(self.fixture.spl),
            'template': factories.TemplateFactory.get_url(self.fixture.template),
            'cluster': factories.ClusterFactory.get_url(self.fixture.cluster),
        }


class VirtualMachineClusterValidationTest(VirtualMachineCreateBaseTest):
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

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_cluster_is_matched_by_customer_and_settings(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload['cluster']

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        vm = models.VirtualMachine.objects.get(uuid=response.data['uuid'])
        self.assertEqual(vm.cluster, self.fixture.cluster)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_be_at_least_one_cluster_for_customer_and_service(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload['cluster']
        self.fixture.cluster.delete()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_not_be_multiple_clusters_for_the_same_customer_and_service(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload['cluster']

        cluster = factories.ClusterFactory(settings=self.fixture.settings)
        factories.CustomerClusterFactory(cluster=cluster, customer=self.fixture.customer)

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VirtualMachineNetworkValidationTest(VirtualMachineCreateBaseTest):

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

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_network_is_matched_by_customer_and_settings(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        vm = models.VirtualMachine.objects.get(uuid=response.data['uuid'])
        self.assertEqual(vm.networks.get(), self.fixture.network)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_be_at_least_one_network_for_customer_and_service(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        self.fixture.network.delete()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_not_be_multiple_networks_for_the_same_customer_and_service(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()

        network = factories.NetworkFactory(settings=self.fixture.settings)
        factories.CustomerNetworkFactory(network=network, customer=self.fixture.customer)

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VirtualMachineDatastoreValidationTest(VirtualMachineCreateBaseTest):

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

    def test_datastore_size_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        datastore = factories.DatastoreFactory()
        payload['datastore'] = factories.DatastoreFactory.get_url(datastore)
        datastore.free_space = 100
        datastore.save()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'][0],
                         'There is no datastore with enough free space available for current customer.')

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_datastore_is_matched_by_customer_and_settings(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload['datastore']

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        vm = models.VirtualMachine.objects.get(uuid=response.data['uuid'])
        self.assertEqual(vm.datastore, self.fixture.datastore)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_be_at_least_one_datastore_for_customer_and_service(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload['datastore']
        self.fixture.datastore.delete()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VirtualMachineFolderValidationTest(VirtualMachineCreateBaseTest):

    def test_folder_settings_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        payload['folder'] = factories.FolderFactory.get_url()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'][0], 'This folder is not available for this service.')

    def test_folder_customer_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        cluster = factories.FolderFactory(settings=self.fixture.settings)
        payload['folder'] = factories.FolderFactory.get_url(cluster)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'][0], 'This folder is not available for this customer.')

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_folder_is_matched_by_customer_and_settings(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload['folder']

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        vm = models.VirtualMachine.objects.get(uuid=response.data['uuid'])
        self.assertEqual(vm.folder, self.fixture.folder)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_be_at_least_one_folder_for_customer_and_service(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload['folder']
        self.fixture.folder.delete()

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_plugin_settings(BASIC_MODE=True)
    def test_with_basic_mode_there_should_not_be_multiple_folders_for_the_same_customer_and_service(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        del payload['folder']

        folder = factories.FolderFactory(settings=self.fixture.settings)
        factories.CustomerFolderFactory(folder=folder, customer=self.fixture.customer)

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VirtualMachinLimitsValidationTest(VirtualMachineCreateBaseTest):

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
