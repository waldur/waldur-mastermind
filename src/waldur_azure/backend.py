import logging

from waldur_azure.client import AzureClient, AzureBackendError, reraise
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
        except AzureBackendError as e:
            if raise_exception:
                reraise(e)
            return False
        else:
            return True

    def pull_service_properties(self):
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

        resource_group_locations = self.client.get_resource_group_locations()

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

        models.Location.objects.filter(name__in=resource_group_locations).update(enabled=True)
        models.Location.objects.exclude(name__in=resource_group_locations).update(enabled=False)

    def pull_public_ips(self, service_project_link):
        locations = {
            location.backend_id: location
            for location in models.Location.objects.filter(settings=self.settings)
        }

        cached_public_ips = {
            public_ip.backend_id: public_ip
            for public_ip in models.PublicIP.objects.filter(service_project_link=service_project_link)
        }

        backend_public_ips = {
            public_ip.name: public_ip
            for public_ip in self.client.list_all_public_ips()
        }

        new_public_ips = {
            public_ip for name, public_ip in backend_public_ips.items()
            if name not in cached_public_ips
        }

        stale_public_ips = {
            public_ip for name, public_ip in cached_public_ips.items()
            if name not in backend_public_ips
        }

        for backend_public_ip in new_public_ips:
            models.PublicIP.objects.create(
                backend_id=backend_public_ip.name,
                name=backend_public_ip.name,
                service_project_link=service_project_link,
                location=locations.get(backend_public_ip.location),
                state=models.PublicIP.States.OK,
            )

        for cached_public_ip in stale_public_ips:
            cached_public_ip.delete()

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

    def pull_network_interface(self, nic):
        backend_nic = self.client.get_network_interface(
            resource_group_name=nic.resource_group.name,
            network_interface_name=nic.name,
        )
        nic.ip_address = backend_nic.ip_configurations[0].private_ip_address
        nic.save()

    def create_network_interface(self, nic):
        poller = self.client.create_network_interface(
            location=nic.resource_group.location.backend_id,
            resource_group_name=nic.resource_group.name,
            interface_name=nic.name,
            subnet_id=nic.subnet.backend_id,
            config_name=nic.config_name,
            public_ip_id=nic.public_ip and nic.public_ip.backend_id,
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
            resource_group_name=virtual_machine.resource_group.name,
            vm_name=virtual_machine.name,
        )
        poller.wait()

    def restart_virtual_machine(self, virtual_machine):
        poller = self.client.restart_virtual_machine(
            resource_group_name=virtual_machine.resource_group.name,
            vm_name=virtual_machine.name,
        )
        poller.wait()

    def stop_virtual_machine(self, virtual_machine):
        poller = self.client.stop_virtual_machine(
            resource_group_name=virtual_machine.resource_group.name,
            vm_name=virtual_machine.name,
        )
        poller.wait()

    def delete_virtual_machine(self, virtual_machine):
        poller = self.client.delete_virtual_machine(
            resource_group_name=virtual_machine.resource_group.name,
            vm_name=virtual_machine.name,
        )
        poller.wait()

    def create_ssh_security_group(self, network_security_group):
        poller = self.client.create_ssh_security_group(
            location=network_security_group.resource_group.location.backend_id,
            resource_group_name=network_security_group.resource_group.name,
            network_security_group_name=network_security_group.name,
        )
        backend_group = poller.result()
        network_security_group.backend_id = backend_group.id
        network_security_group.save()

    def create_public_ip(self, public_ip):
        poller = self.client.create_public_ip(
            location=public_ip.resource_group.location.backend_id,
            resource_group_name=public_ip.resource_group.name,
            public_ip_address_name=public_ip.name,
        )
        backend_public_ip = poller.result()
        public_ip.backend_id = backend_public_ip.id
        public_ip.runtime_state = backend_public_ip.provisioning_state
        public_ip.save()

    def delete_public_ip(self, public_ip):
        poller = self.client.delete_public_ip(
            resource_group_name=public_ip.resource_group.name,
            public_ip_address_name=public_ip.name,
        )
        poller.wait()

    def pull_public_ip_address(self, public_ip):
        backend_public_ip = self.client.get_public_ip(
            resource_group_name=public_ip.resource_group.name,
            public_ip_address_name=public_ip.name,
        )
        public_ip.ip_address = backend_public_ip.ip_address
        public_ip.runtime_state = backend_public_ip.provisioning_state
        public_ip.save()

    def create_pgsql_server(self, server):
        backend_server = self.client.create_sql_server(
            location=server.resource_group.location.backend_id,
            resource_group_name=server.resource_group.name,
            server_name=server.name,
            username=server.username,
            password=server.password,
            storage_mb=server.storage_mb,
            sku={
                'name': 'B_Gen5_1',
                'tier': 'Basic',
                'family': 'Gen5',
                'capacity': 1,
            },
        )
        server.backend_id = backend_server.id
        server.fqdn = backend_server.fully_qualified_domain_name
        server.save()

        self.client.create_sql_firewall_rule(
            resource_group_name=server.resource_group.name,
            server_name=server.name,
            firewall_rule_name='firewall{}'.format(server.name),
            start_ip_address='0.0.0.0',  # nosec
            end_ip_address='255.255.255.255',  # nosec
        )

    def delete_pgsql_server(self, server):
        poller = self.client.delete_sql_server(
            resource_group_name=server.resource_group.name,
            server_name=server.name,
        )
        poller.wait()

    def create_pgsql_database(self, database):
        poller = self.client.create_sql_database(
            resource_group_name=database.server.resource_group.name,
            server_name=database.server.name,
            database_name=database.name,
            charset=database.charset,
            collation=database.collation,
        )
        backend_database = poller.result()
        database.backend_id = backend_database.id
        database.save()

    def delete_pgsql_database(self, database):
        poller = self.client.delete_sql_database(
            resource_group_name=database.server.resource_group.name,
            server_name=database.server.name,
            database_name=database.name,
        )
        poller.wait()
