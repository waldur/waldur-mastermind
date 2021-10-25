import logging
from itertools import islice

from azure.core.exceptions import HttpResponseError
from django.core.exceptions import ObjectDoesNotExist

from waldur_azure.client import AzureBackendError, AzureClient, AzureImage
from waldur_core.structure.backend import ServiceBackend

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
                raise AzureBackendError(e)
            return False
        else:
            return True

    def pull_service_properties(self):
        self.pull_locations()
        self.pull_all_sizes()
        self.pull_all_images()
        self.pull_all_size_availability()

    def pull_locations(self):
        cached_locations = {
            location.backend_id: location
            for location in models.Location.objects.filter(settings=self.settings)
        }

        backend_locations = {
            location.name: location for location in self.client.list_locations()
        }

        resource_group_locations = self.client.get_resource_group_locations()

        new_locations = [
            location
            for name, location in backend_locations.items()
            if name not in cached_locations
        ]

        stale_locations = [
            location
            for name, location in cached_locations.items()
            if name not in backend_locations
        ]

        for backend_location in new_locations:
            models.Location.objects.create(
                backend_id=backend_location.name,
                name=backend_location.display_name,
                latitude=backend_location.metadata.latitude,
                longitude=backend_location.metadata.longitude,
                settings=self.settings,
            )

        for cached_location in stale_locations:
            cached_location.delete()

        models.Location.objects.filter(name__in=resource_group_locations).update(
            enabled=True
        )
        models.Location.objects.exclude(name__in=resource_group_locations).update(
            enabled=False
        )

    def pull_public_ips(self, service_settings, project):
        locations = {
            location.backend_id: location
            for location in models.Location.objects.filter(settings=self.settings)
        }

        cached_public_ips = {
            public_ip.backend_id: public_ip
            for public_ip in models.PublicIP.objects.filter(
                service_settings=service_settings, project=project,
            )
        }

        backend_public_ips = {
            public_ip.name: public_ip for public_ip in self.client.list_all_public_ips()
        }

        new_public_ips = {
            public_ip
            for name, public_ip in backend_public_ips.items()
            if name not in cached_public_ips
        }

        stale_public_ips = {
            public_ip
            for name, public_ip in cached_public_ips.items()
            if name not in backend_public_ips
        }

        for backend_public_ip in new_public_ips:
            models.PublicIP.objects.create(
                backend_id=backend_public_ip.name,
                name=backend_public_ip.name,
                service_settings=service_settings,
                project=project,
                location=locations.get(backend_public_ip.location),
                state=models.PublicIP.States.OK,
            )

        for cached_public_ip in stale_public_ips:
            cached_public_ip.delete()

    def pull_all_images(self):
        for location in models.Location.objects.filter(settings=self.settings):
            self.pull_images(location)

    def pull_images(self, location):
        cached_images = {
            image.backend_id: image
            for image in models.Image.objects.filter(
                settings=self.settings, location=location
            )
        }

        try:
            backend_images = {
                image_wrapper.image.id: image_wrapper
                for image_wrapper in islice(
                    self.client.list_virtual_machine_images(
                        location.backend_id,
                        ['MicrosoftSQLServer', 'Debian', 'Canonical'],
                    ),
                    10,
                )
            }
        except HttpResponseError as e:
            if e.error.code == 'NoRegisteredProviderFound':
                backend_images = {}
            else:
                raise AzureBackendError(e)

        new_images = {
            backend_id: image_wrapper
            for backend_id, image_wrapper in backend_images.items()
            if backend_id not in cached_images
        }

        stale_images = {
            backend_id: image
            for backend_id, image in cached_images.items()
            if backend_id not in backend_images
        }

        for backend_image_name in new_images:
            backend_image: AzureImage = new_images[backend_image_name]
            models.Image.objects.create(
                backend_id=backend_image.image.id,
                offer=backend_image.offer_name,
                publisher=backend_image.publisher_name,
                sku=backend_image.sku_name,
                version=backend_image.version_name,
                name=f'{backend_image.offer_name} {backend_image.version_name}',
                settings=self.settings,
                location=location,
            )

        for cached_image_name in stale_images:
            stale_images[cached_image_name].delete()

    def pull_all_sizes(self):
        for location in models.Location.objects.filter(settings=self.settings):
            self.pull_sizes(location)

    def pull_sizes(self, location):
        cached_sizes = {
            size.backend_id: size
            for size in models.Size.objects.filter(settings=self.settings)
        }

        try:
            backend_sizes = {
                size.name: size
                for size in self.client.list_virtual_machine_sizes(location.backend_id)
            }
        except HttpResponseError as e:
            if e.error.code == 'NoRegisteredProviderFound':
                logger.warning('Unable to fetch sizes for Azure, %s', e)
                return
            else:
                raise AzureBackendError(e)

        new_sizes = {
            name: size
            for name, size in backend_sizes.items()
            if name not in cached_sizes
        }

        stale_sizes = {
            name: size
            for name, size in cached_sizes.items()
            if name not in backend_sizes
        }

        for backend_size_name in new_sizes:
            backend_size = new_sizes[backend_size_name]
            models.Size.objects.create(
                backend_id=backend_size.name,
                settings=self.settings,
                **backend_size.as_dict(),
            )

        for cached_size_name in stale_sizes:
            stale_sizes[cached_size_name].delete()

    def pull_all_size_availability(self):
        for location in models.Location.objects.filter(settings=self.settings):
            self.pull_size_availability(location)

    def pull_size_availability(self, location):
        zones_map = self.client.list_virtual_machine_size_availability_zones(
            location.backend_id
        )
        for size_name, backend_zones in zones_map.items():
            try:
                size = models.Size.objects.get(settings=self.settings, name=size_name)
            except ObjectDoesNotExist:
                continue
            cached_zones = models.SizeAvailabilityZone.objects.filter(
                size__name=size_name, location=location
            ).values_list('zone', flat=True)
            new_zones = set(backend_zones) - set(cached_zones)
            for zone in new_zones:
                models.SizeAvailabilityZone.objects.create(
                    location=location, size=size, zone=zone
                )
            models.SizeAvailabilityZone.objects.filter(
                size__name=size_name, location=location
            ).exclude(zone__in=backend_zones).delete()

    def pull_resource_groups(self, service_settings, project):
        cached_groups = {
            group.backend_id: group
            for group in models.ResourceGroup.objects.filter(
                service_settings=service_settings, project=project,
            )
        }

        backend_groups = {
            group.name: group for group in self.client.list_resource_groups()
        }

        new_groups = {
            group for name, group in backend_groups.items() if name not in cached_groups
        }

        stale_groups = {
            group for name, group in cached_groups.items() if name not in backend_groups
        }

        locations = {
            location.backend_id: location
            for location in models.Location.objects.filter(settings=self.settings)
        }

        for backend_group in new_groups:
            models.ResourceGroup.objects.create(
                backend_id=backend_group.id,
                name=backend_group.name,
                service_settings=service_settings,
                project=project,
                location=locations.get(backend_group.location),
                state=models.ResourceGroup.States.OK,
            )

        for cached_group in stale_groups:
            cached_group.delete()

    def create_resource_group(self, resource_group):
        backend_resource_group = self.client.create_resource_group(
            location=resource_group.location.backend_id,
            resource_group_name=resource_group.name,
        )
        resource_group.backend_id = backend_resource_group.id
        resource_group.save()

    def create_storage_account(self, storage_account):
        # Storage SDK does not support create_or_update logic, so reimplementing it
        exists = not self.client.storage_client.storage_accounts.check_name_availability(
            {'name': storage_account.name}
        ).name_available
        if not exists:
            poller = self.client.create_storage_account(
                location=storage_account.resource_group.location.backend_id,
                resource_group_name=storage_account.resource_group.name,
                account_name=storage_account.name,
            )
            backend_storage_account = poller.result()
        else:
            backend_storage_account = self.client.storage_client.storage_accounts.get_properties(
                storage_account.resource_group.name, storage_account.name
            )
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

    def create_virtual_machine(self, vm: models.VirtualMachine):
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
                'offer': vm.image.offer,
            },
            username=vm.username,
            password=vm.password,
            ssh_key=vm.ssh_key and vm.ssh_key.public_key or None,
            custom_data=vm.user_data,
        )
        backend_vm = poller.result()
        vm.backend_id = backend_vm.id
        vm.runtime_state = self.get_virtual_machine_runtime_state(backend_vm)
        vm.save(update_fields=['backend_id', 'runtime_state'])

    def start_virtual_machine(self, virtual_machine):
        poller = self.client.start_virtual_machine(
            resource_group_name=virtual_machine.resource_group.name,
            vm_name=virtual_machine.name,
        )
        poller.wait()
        self.pull_virtual_machine(virtual_machine)

    def restart_virtual_machine(self, virtual_machine):
        poller = self.client.restart_virtual_machine(
            resource_group_name=virtual_machine.resource_group.name,
            vm_name=virtual_machine.name,
        )
        poller.wait()
        self.pull_virtual_machine(virtual_machine)

    def stop_virtual_machine(self, virtual_machine):
        poller = self.client.stop_virtual_machine(
            resource_group_name=virtual_machine.resource_group.name,
            vm_name=virtual_machine.name,
        )
        poller.wait()
        self.pull_virtual_machine(virtual_machine)

    def delete_virtual_machine(self, virtual_machine):
        poller = self.client.delete_virtual_machine(
            resource_group_name=virtual_machine.resource_group.name,
            vm_name=virtual_machine.name,
        )
        poller.wait()

    def get_importable_virtual_machines(self):
        virtual_machines = [
            {'name': vm.name, 'backend_id': vm.id,}
            for vm in self.client.list_all_virtual_machines()
        ]
        return self.get_importable_resources(models.VirtualMachine, virtual_machines)

    def import_virtual_machine(self, backend_id, project):
        resource_group_name = backend_id.split('/')[4]
        vm_name = backend_id.split('/')[-1]
        backend_vm = self.client.get_virtual_machine(resource_group_name, vm_name)

        location = models.Location.objects.get(
            settings=self.settings, backend_id=backend_vm.location
        )
        resource_group, _ = models.ResourceGroup.objects.get_or_create(
            service_settings=self.settings,
            backend_id=resource_group_name,
            project=project,
            defaults=dict(
                name=resource_group_name,
                location=location,
                state=models.ResourceGroup.States.OK,
            ),
        )

        size = models.Size.objects.get(
            settings=self.settings, backend_id=backend_vm.hardware_profile.vm_size
        )

        image_ref = backend_vm.storage_profile.image_reference
        image = models.Image.objects.get(
            settings=self.settings,
            offer=image_ref.offer,
            version=image_ref.exact_version,
        )

        network_interface = self.import_network_interface(
            backend_vm, project, resource_group, location
        )

        return models.VirtualMachine.objects.create(
            service_settings=self.settings,
            project=project,
            resource_group=resource_group,
            name=vm_name,
            backend_id=vm_name,
            network_interface=network_interface,
            size=size,
            image=image,
            state=models.VirtualMachine.States.OK,
        )

    def import_network_interface(self, backend_vm, project, resource_group, location):
        network_interface_id = backend_vm.network_profile.network_interfaces[0].id
        try:
            return models.NetworkInterface.objects.get(
                resource_group=resource_group, backend_id=network_interface_id,
            )
        except ObjectDoesNotExist:
            network_interface_name = network_interface_id.split('/')[-1]
            backend_interface = self.client.get_network_interface(
                resource_group.name, network_interface_name
            )

            subnet_id = backend_interface.ip_configurations[0].subnet.id
            subnet_name = subnet_id.split('/')[-1]
            network_name = subnet_id.split('/')[-3]

            backend_network = self.client.get_network(resource_group.name, network_name)
            network = models.Network.objects.create(
                name=network_name,
                resource_group=resource_group,
                service_settings=self.settings,
                project=project,
                backend_id=network_name,
                cidr=backend_network.address_space.address_prefixes[0],
                state=models.Network.States.OK,
            )

            backend_subnet = self.client.get_subnet(
                resource_group.name, network_name, subnet_name
            )
            subnet = models.SubNet.objects.create(
                name=subnet_name,
                resource_group=resource_group,
                service_settings=self.settings,
                project=project,
                backend_id=subnet_name,
                cidr=backend_subnet.address_prefix,
                network=network,
                state=models.SubNet.States.OK,
            )

            public_ip_address_name = backend_interface.ip_configurations[
                0
            ].public_ip_address.id.split('/')[-1]
            backend_public_ip = self.client.get_public_ip(
                resource_group.name, public_ip_address_name
            )
            public_ip = models.PublicIP.objects.create(
                name=public_ip_address_name,
                resource_group=resource_group,
                service_settings=self.settings,
                project=project,
                backend_id=public_ip_address_name,
                ip_address=backend_public_ip.ip_address,
                location=location,
                state=models.PublicIP.States.OK,
            )

            return models.NetworkInterface.objects.create(
                name=network_interface_name,
                resource_group=resource_group,
                service_settings=self.settings,
                project=project,
                backend_id=network_interface_id,
                public_ip=public_ip,
                subnet=subnet,
                state=models.NetworkInterface.States.OK,
            )

    def pull_virtual_machine(self, local_vm: models.VirtualMachine):
        backend_vm = self.client.get_virtual_machine(
            local_vm.resource_group.name, local_vm.name
        )
        new_runtime_state = self.get_virtual_machine_runtime_state(backend_vm)
        if new_runtime_state != local_vm.runtime_state:
            local_vm.runtime_state = new_runtime_state
            local_vm.save(update_fields=['runtime_state'])

    def get_virtual_machine_runtime_state(self, backend_vm):
        if backend_vm.instance_view is None:
            return ''
        for status in backend_vm.instance_view.statuses:
            key, val = status.code.split('/')
            if key == 'PowerState':
                return val

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
            sku={'name': 'B_Gen5_1', 'tier': 'Basic', 'family': 'Gen5', 'capacity': 1,},
        )
        server.backend_id = backend_server.id
        server.fqdn = backend_server.fully_qualified_domain_name
        server.save()

        self.client.create_sql_firewall_rule(
            resource_group_name=server.resource_group.name,
            server_name=server.name,
            firewall_rule_name='firewall{}'.format(server.name),
            start_ip_address='0.0.0.0',  # noqa: S104
            end_ip_address='255.255.255.255',
        )

    def delete_pgsql_server(self, server):
        poller = self.client.delete_sql_server(
            resource_group_name=server.resource_group.name, server_name=server.name,
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
