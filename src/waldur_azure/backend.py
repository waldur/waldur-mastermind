import logging

from waldur_azure.client import AzureClient, AzureBackendError
from waldur_core.structure import ServiceBackend

from . import models

logger = logging.getLogger(__name__)


class AzureBackend(ServiceBackend):
    def __init__(self, settings):
        self.settings = settings
        self.client = AzureClient(settings)

    def ping(self, raise_exception=False):
        try:
            self.client.list_locations()
        except AzureBackendError:
            return False
        else:
            return True

    def sync(self):
        self.pull_locations()

        location = models.Location.objects.filter(settings=self.settings).last()
        self.pull_sizes(location)

    def pull_locations(self):
        cached_locations = {
            location.backend_id: location
            for location in models.Location.objects.filter(settings=self.settings)
        }

        backend_locations = {
            location.name: location
            for location in self.client.list_locations()
        }

        new_locations = {
            location for name, location in backend_locations.items()
            if name not in cached_locations
        }

        stale_locations = {
            location for name, location in cached_locations.items()
            if name not in backend_locations
        }

        for backend_location in new_locations:
            models.Location.objects.create(
                backend_id=backend_location.name,
                name=backend_location.display_name,
                latitude=backend_location.latitude,
                longitude=backend_location.longitude,
                settings=self.settings,
            )

        for cached_location in stale_locations:
            cached_location.delete()

    def pull_sizes(self, location):
        cached_sizes = {
            size.backend_id: size
            for size in models.Size.objects.filter(settings=self.settings)
        }

        backend_sizes = {
            size.name: size
            for size in self.client.list_virtual_machine_sizes(location.backend_id)
        }

        new_sizes = {
            size for name, size in backend_sizes.items()
            if name not in cached_sizes
        }

        stale_sizes = {
            size for name, size in cached_sizes.items()
            if name not in backend_sizes
        }

        for backend_size in new_sizes:
            models.Size.objects.create(
                backend_id=backend_size.name,
                settings=self.settings,
                **backend_size.as_dict()
            )

        for cached_size in stale_sizes:
            cached_size.delete()

    def pull_resource_groups(self, service_project_link):
        cached_groups = {
            group.backend_id: group
            for group in models.ResourceGroup.objects.filter(service_project_link=service_project_link)
        }

        backend_groups = {
            group.name: group
            for group in self.client.list_resource_groups()
        }

        new_groups = {
            group for name, group in backend_groups.items()
            if name not in cached_groups
        }

        stale_groups = {
            group for name, group in cached_groups.items()
            if name not in backend_groups
        }

        locations = {
            location.backend_id: location
            for location in models.Location.objects.filter(settings=self.settings)
        }

        for backend_group in new_groups:
            models.ResourceGroup.objects.create(
                backend_id=backend_group.id,
                name=backend_group.name,
                service_project_link=service_project_link,
                location=locations.get(backend_group.location),
                state=models.ResourceGroup.States.OK,
            )

        for cached_group in stale_groups:
            cached_group.delete()

    def create_resource_group(self, resource_group):
        backend_resource_group = self.client.create_resource_group(
            location=resource_group.location.backend_id,
            resource_group_name=resource_group.name
        )
        resource_group.backend_id = backend_resource_group.id
        resource_group.save()

    def create_storage_account(self, storage_account):
        poller = self.client.create_storage_account(
            location=storage_account.resource_group.location.backend_id,
            resource_group_name=storage_account.resource_group.name,
            account_name=storage_account.name,
        )
        backend_storage_account = poller.result()
        storage_account.backend_id = backend_storage_account.id
        storage_account.save()

    def delete_resource_group(self, resource_group):
        self.client.delete_resource_group(resource_group.backend_id)

    def create_network(self, network):
        poller = self.client.create_network(
            location=network.resource_group.location.backend_id,
            resource_group_name=network.resource_group.name,
            network_name=network.name,
            cidr=network.cidr,
        )
        backend_network = poller.result()
        network.backend_id = backend_network.id
        network.save()

    def create_subnet(self, subnet):
        poller = self.client.create_subnet(
            resource_group_name=subnet.resource_group.name,
            network_name=subnet.network.name,
            subnet_name=subnet.name,
            cidr=subnet.cidr,
        )
        backend_subnet = poller.result()
        subnet.backend_id = backend_subnet.id
        subnet.save()

    def create_network_interface(self, nic):
        poller = self.client.create_network_interface(
            location=nic.resource_group.location.backend_id,
            resource_group_name=nic.resource_group.name,
            interface_name=nic.name,
            subnet_id=nic.subnet.backend_id,
            config_name=nic.config_name,
        )
        backend_nic = poller.result()
        nic.backend_id = backend_nic.id
        nic.save()

    def create_virtual_machine(self, vm):
        poller = self.client.create_virtual_machine(
            location=vm.resource_group.location.backend_id,
            resource_group_name=vm.resource_group.name,
            vm_name=vm.name,
            size_name=vm.size.name,
            nic_id=vm.network_interface.backend_id,
            image_reference={
                'sku': vm.image.sku,
                'publisher': vm.image.publisher,
                'version': vm.image.version,
                'offer': vm.image.name,
            },
            username=vm.username,
            password=vm.password,
            ssh_key=vm.ssh_key and vm.ssh_key.public_key or None,
            custom_data=vm.user_data,
        )
        backend_vm = poller.result()
        vm.backend_id = backend_vm.id
        vm.runtime_state = backend_vm.provisioning_state
        vm.save()

    def start_virtual_machine(self, virtual_machine):
        poller = self.client.start_virtual_machine(
            resource_group_name=virtual_machine.resource_group.backend_id,
            vm_name=virtual_machine.backend_id,
        )
        poller.wait()

    def restart_virtual_machine(self, virtual_machine):
        poller = self.client.restart_virtual_machine(
            resource_group_name=virtual_machine.resource_group.backend_id,
            vm_name=virtual_machine.backend_id,
        )
        poller.wait()

    def stop_virtual_machine(self, virtual_machine):
        poller = self.client.stop_virtual_machine(
            resource_group_name=virtual_machine.resource_group.backend_id,
            vm_name=virtual_machine.backend_id,
        )
        poller.wait()

    def delete_virtual_machine(self, virtual_machine):
        poller = self.client.delete_virtual_machine(
            resource_group_name=virtual_machine.resource_group.backend_id,
            vm_name=virtual_machine.backend_id,
        )
        poller.wait()

    def create_pgsql_server(self, server):
        poller = self.client.create_sql_server(
            location=server.resource_group.location.backend_id,
            resource_group_name=server.resource_group.backend_id,
            server_name=server.name,
            username=server.username,
            password=server.password,
            storage_mb=server.storage_mb,
        )
        poller.wait()

    def delete_pgsql_server(self, server):
        poller = self.client.delete_sql_server(
            resource_group_name=server.resource_group.backend_id,
            server_name=server.backend_id,
        )
        poller.wait()

    def create_pgsql_database(self, database):
        poller = self.client.create_sql_database(
            location=database.resource_group.location.backend_id,
            resource_group_name=database.resource_group.backend_id,
            server_name=database.server.backend_id,
            database_name=database.name,
        )
        poller.wait()

    def delete_pgsql_database(self, database):
        poller = self.client.delete_sql_database(
            resource_group_name=database.resource_group.backend_id,
            server_name=database.server.backend_id,
            database_name=database.backend_id,
        )
        poller.wait()
