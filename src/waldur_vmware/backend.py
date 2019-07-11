import sys
import ssl

from django.conf import settings
from django.utils import six, timezone
from django.utils.functional import cached_property
import pyVim.task
import pyVim.connect
from pyVmomi import vim

from waldur_core.structure import ServiceBackend, ServiceBackendError, log_backend_action
from waldur_core.structure.utils import update_pulled_fields
from waldur_mastermind.common.utils import parse_datetime
from waldur_vmware.client import VMwareClient
from waldur_vmware.exceptions import VMwareError

from . import models, signals


class VMwareBackendError(ServiceBackendError):
    pass


def reraise(exc):
    """
    Reraise VMwareBackendError while maintaining traceback.
    """
    six.reraise(VMwareBackendError, exc, sys.exc_info()[2])


class VMwareBackend(ServiceBackend):
    def __init__(self, settings):
        """
        :type settings: :class:`waldur_core.structure.models.ServiceSettings`
        """
        self.settings = settings

    @cached_property
    def host(self):
        return self.settings.backend_url.split('https://')[-1]

    @cached_property
    def client(self):
        """
        Construct VMware REST API client using credentials specified in the service settings.
        """
        client = VMwareClient(self.host, verify_ssl=False)
        client.login(self.settings.username, self.settings.password)
        return client

    @cached_property
    def soap_client(self):
        """
        Construct VMware SOAP API client using credentials specified in the service settings.
        """
        context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        context.verify_mode = ssl.CERT_NONE
        return pyVim.connect.SmartConnect(
            host=self.host,
            user=self.settings.username,
            pwd=self.settings.password,
            port=443,
            sslContext=context
        )

    def ping(self, raise_exception=False):
        """
        Check if backend is ok.
        """
        try:
            self.client.list_vms()
        except VMwareError as e:
            if raise_exception:
                reraise(e)
            return False
        else:
            return True

    def pull_service_properties(self):
        self.pull_folders()
        self.pull_templates()
        self.pull_clusters()
        self.pull_networks()
        self.pull_datastores()

    def pull_templates(self):
        """
        Pull VMware templates for virtual machine provisioning from content library
        using VMware REST API to the local database.
        """
        try:
            backend_templates = self.client.list_all_templates()
        except VMwareError as e:
            reraise(e)
            return

        backend_templates_map = {
            item['library_item']['id']: item
            for item in backend_templates
        }

        frontend_templates_map = {
            p.backend_id: p
            for p in models.Template.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_templates_map.keys()) - set(backend_templates_map.keys())
        new_ids = set(backend_templates_map.keys()) - set(frontend_templates_map.keys())

        for library_item_id in new_ids:
            item = backend_templates_map[library_item_id]
            library_item = item['library_item']
            template = item['template']
            total_disk = self._get_total_disk(template['disks'])
            models.Template.objects.create(
                settings=self.settings,
                backend_id=library_item_id,
                name=library_item['name'],
                description=library_item['description'],
                created=parse_datetime(library_item['creation_time']),
                modified=parse_datetime(library_item['last_modified_time']),
                cores=template['cpu']['count'],
                cores_per_socket=template['cpu']['cores_per_socket'],
                ram=template['memory']['size_MiB'],
                disk=total_disk,
                guest_os=template['guest_OS'],
            )

        models.Template.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def _get_total_disk(self, backend_disks):
        # Convert disk size from bytes to MiB
        return sum([disk['value']['capacity'] / 1024 / 1024 for disk in backend_disks])

    @log_backend_action()
    def pull_virtual_machine(self, vm, update_fields=None):
        """
        Pull virtual machine from REST API and update its information in local database.

        :param vm: Virtual machine database object.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        :param update_fields: iterable of fields to be updated
        """
        import_time = timezone.now()
        imported_vm = self.import_virtual_machine(vm.backend_id, save=False)

        vm.refresh_from_db()
        if vm.modified < import_time:
            if not update_fields:
                update_fields = models.VirtualMachine.get_backend_fields()

            update_pulled_fields(vm, imported_vm, update_fields)

    def import_virtual_machine(self, backend_id, save=True, service_project_link=None):
        """
        Import virtual machine by its ID.

        :param backend_id: Virtual machine identifier
        :type backend_id: str
        :param save: Save object in the database
        :type save: bool
        :param service_project_link: Optional service project link model object
        :rtype: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            backend_vm = self.client.get_vm(backend_id)
        except VMwareError as e:
            reraise(e)
            return

        vm = self._backend_vm_to_vm(backend_vm, backend_id)
        if service_project_link is not None:
            vm.service_project_link = service_project_link
        if save:
            vm.save()

        return vm

    def _backend_vm_to_vm(self, backend_vm, backend_id):
        """
        Build database model object for virtual machine from REST API spec.

        :param backend_vm: virtual machine specification
        :type backend_vm: dict
        :param backend_id: Virtual machine identifier
        :type backend_id: str
        :rtype: :class:`waldur_vmware.models.VirtualMachine`
        """
        return models.VirtualMachine(
            backend_id=backend_id,
            name=backend_vm['name'],
            state=models.VirtualMachine.States.OK,
            runtime_state=backend_vm['power_state'],
            cores=backend_vm['cpu']['count'],
            cores_per_socket=backend_vm['cpu']['cores_per_socket'],
            ram=backend_vm['memory']['size_MiB'],
            disk=self._get_total_disk(backend_vm['disks']),
        )

    def pull_clusters(self):
        try:
            backend_clusters = self.client.list_clusters()
        except VMwareError as e:
            reraise(e)
            return

        backend_clusters_map = {
            item['cluster']: item
            for item in backend_clusters
        }

        frontend_clusters_map = {
            p.backend_id: p
            for p in models.Cluster.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_clusters_map.keys()) - set(backend_clusters_map.keys())
        new_ids = set(backend_clusters_map.keys()) - set(frontend_clusters_map.keys())

        for item_id in new_ids:
            item = backend_clusters_map[item_id]
            models.Cluster.objects.create(
                settings=self.settings,
                backend_id=item_id,
                name=item['name'],
            )

        models.Cluster.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def pull_networks(self):
        try:
            backend_networks = self.client.list_networks()
        except VMwareError as e:
            reraise(e)
            return

        backend_networks_map = {
            item['network']: item
            for item in backend_networks
        }

        frontend_networks_map = {
            p.backend_id: p
            for p in models.Network.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_networks_map.keys()) - set(backend_networks_map.keys())
        new_ids = set(backend_networks_map.keys()) - set(frontend_networks_map.keys())

        for item_id in new_ids:
            item = backend_networks_map[item_id]
            models.Network.objects.create(
                settings=self.settings,
                backend_id=item_id,
                name=item['name'],
                type=item['type'],
            )

        models.Network.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def pull_datastores(self):
        try:
            backend_datastores = self.client.list_datastores()
        except VMwareError as e:
            reraise(e)
            return

        backend_datastores_map = {
            item['datastore']: item
            for item in backend_datastores
        }

        frontend_datastores_map = {
            p.backend_id: p
            for p in models.Datastore.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_datastores_map.keys()) - set(backend_datastores_map.keys())
        new_ids = set(backend_datastores_map.keys()) - set(frontend_datastores_map.keys())

        for item_id in new_ids:
            item = backend_datastores_map[item_id]

            capacity = item.get('capacity')
            # Convert from bytes to MB
            if capacity:
                capacity /= 1024 * 1024

            free_space = item.get('free_space')
            # Convert from bytes to MB
            if free_space:
                free_space /= 1024 * 1024

            models.Datastore.objects.create(
                settings=self.settings,
                backend_id=item_id,
                name=item['name'],
                type=item['type'],
                capacity=capacity,
                free_space=free_space,
            )

        models.Datastore.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def pull_folders(self):
        try:
            backend_folders = self.client.list_folders(folder_type='VIRTUAL_MACHINE')
        except VMwareError as e:
            reraise(e)
            return

        backend_folders_map = {
            item['folder']: item
            for item in backend_folders
        }

        frontend_folders_map = {
            p.backend_id: p
            for p in models.Folder.objects.filter(settings=self.settings)
        }

        stale_ids = set(frontend_folders_map.keys()) - set(backend_folders_map.keys())
        new_ids = set(backend_folders_map.keys()) - set(frontend_folders_map.keys())

        for item_id in new_ids:
            item = backend_folders_map[item_id]
            models.Folder.objects.create(
                settings=self.settings,
                backend_id=item_id,
                name=item['name'],
            )

        models.Folder.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

    def create_virtual_machine(self, vm):
        """
        Creates a virtual machine.

        :param vm: Virtual machine to be created
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        if vm.template:
            backend_id = self.create_virtual_machine_from_template(vm)
        else:
            backend_id = self.create_virtual_machine_from_scratch(vm)

        try:
            backend_vm = self.client.get_vm(backend_id)
        except VMwareError as e:
            reraise(e)
            return

        vm.backend_id = backend_id
        vm.runtime_state = backend_vm['power_state']
        vm.save(update_fields=['backend_id', 'runtime_state'])

        for disk in backend_vm['disks']:
            disk = self._backend_disk_to_disk(disk['value'], disk['key'])
            disk.vm = vm
            disk.service_project_link = vm.service_project_link
            disk.save()

        signals.vm_created.send(self.__class__, vm=vm)
        return vm

    def _get_vm_placement(self, vm):
        placement = {}

        try:
            customer = vm.service_project_link.project.customer
            folder = models.Folder.objects.filter(customerfolder__customer=customer).get()
            placement['folder'] = folder.backend_id
        except models.Folder.DoesNotExist:
            placement['folder'] = settings.WALDUR_VMWARE['VM_FOLDER']

        if vm.cluster:
            placement['cluster'] = vm.cluster.backend_id
        else:
            placement['resource_pool'] = settings.WALDUR_VMWARE['VM_RESOURCE_POOL']

        return placement

    def create_virtual_machine_from_template(self, vm):
        spec = {
            'name': vm.name,
            'description': vm.description,
            'hardware_customization': {
                'cpu_update': {
                    'num_cpus': vm.cores,
                    'num_cores_per_socket': vm.cores_per_socket,
                },
                'memory_update': {
                    'memory': vm.ram,
                },
            },
            'placement': self._get_vm_placement(vm),
        }

        if vm.datastore:
            spec['disk_storage'] = {'datastore': vm.datastore.backend_id}

        if vm.networks.count():
            """We need to get the NIC keys from the template to overwrite the networks.
            We can't rewrite the networks more than there is in the template."""
            backend_template = self.client.get_template_library_item(vm.template.backend_id)
            keys = [k['key'] for k in backend_template['nics']]
            nics = []

            for network in vm.networks.all():
                try:
                    key = keys.pop()
                except IndexError:
                    break
                nics.append({
                    "key": key,
                    "value": {
                        "network": network.backend_id
                    }
                })
            spec['hardware_customization']['nics'] = nics

        try:
            return self.client.deploy_vm_from_template(vm.template.backend_id, spec)
        except VMwareError as e:
            reraise(e)

    def create_virtual_machine_from_scratch(self, vm):
        spec = {
            'name': vm.name,
            'guest_OS': vm.guest_os,
            'cpu': {
                'count': vm.cores,
                'cores_per_socket': vm.cores_per_socket,
                'hot_add_enabled': True,
                'hot_remove_enabled': True
            },
            'memory': {
                'size_MiB': vm.ram,
                'hot_add_enabled': True,
            },
            'placement': self._get_vm_placement(vm),
        }

        if vm.datastore:
            spec['placement']['datastore'] = vm.datastore.backend_id
        else:
            spec['placement']['datastore'] = settings.WALDUR_VMWARE['VM_DATASTORE']

        try:
            return self.client.create_vm(spec)
        except VMwareError as e:
            reraise(e)

    def delete_virtual_machine(self, vm):
        """
        Deletes a virtual machine.

        :param vm: Virtual machine to be deleted
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.delete_vm(vm.backend_id)
        except VMwareError as e:
            reraise(e)

    def start_virtual_machine(self, vm):
        """
        Powers on a powered-off or suspended virtual machine.

        :param vm: Virtual machine to be started
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.start_vm(vm.backend_id)
        except VMwareError as e:
            reraise(e)

    def stop_virtual_machine(self, vm):
        """
        Powers off a powered-on or suspended virtual machine.

        :param vm: Virtual machine to be stopped
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.stop_vm(vm.backend_id)
        except VMwareError as e:
            reraise(e)

    def reset_virtual_machine(self, vm):
        """
        Resets a powered-on virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.reset_vm(vm.backend_id)
        except VMwareError as e:
            reraise(e)

    def suspend_virtual_machine(self, vm):
        """
        Suspends a powered-on virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.suspend_vm(vm.backend_id)
        except VMwareError as e:
            reraise(e)

    def update_virtual_machine(self, vm):
        """
        Updates CPU and RAM of virtual machine.
        """
        self.update_cpu(vm)
        self.update_memory(vm)
        signals.vm_updated.send(self.__class__, vm=vm)

    def update_cpu(self, vm):
        """
        Updates CPU of virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            cpu_spec = self.client.get_cpu(vm.backend_id)
            if cpu_spec['cores_per_socket'] != vm.cores_per_socket or cpu_spec['count'] != vm.cores:
                self.client.update_cpu(vm.backend_id, {
                    'cores_per_socket': vm.cores_per_socket,
                    'count': vm.cores,
                })
        except VMwareError as e:
            reraise(e)

    def update_memory(self, vm):
        """
        Updates RAM of virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            memory_spec = self.client.get_memory(vm.backend_id)
            if memory_spec['size_MiB'] != vm.ram:
                self.client.update_memory(vm.backend_id, {
                    'size_MiB': vm.ram
                })
        except VMwareError as e:
            reraise(e)

    def create_disk(self, disk):
        """
        Creates a virtual disk.

        :param disk: Virtual disk to be created
        :type disk: :class:`waldur_vmware.models.Disk`
        """
        spec = {
            'new_vmdk': {
                # Convert from mebibytes to bytes because VMDK is specified in bytes
                'capacity': 1024 * 1024 * disk.size,
            }
        }

        try:
            backend_id = self.client.create_disk(disk.vm.backend_id, spec)
        except VMwareError as e:
            reraise(e)
        else:
            disk.backend_id = backend_id
            disk.save(update_fields=['backend_id'])
            signals.vm_updated.send(self.__class__, vm=disk.vm)
            return disk

    def delete_disk(self, disk, delete_vmdk=True):
        """
        Deletes a virtual disk.

        :param disk: Virtual disk to be deleted
        :type disk: :class:`waldur_vmware.models.Disk`
        :param delete_vmdk: Delete backing VMDK file.
        """
        backend_disk = self.get_backend_disk(disk)

        try:
            self.client.delete_disk(disk.vm.backend_id, disk.backend_id)
        except VMwareError as e:
            reraise(e)

        if delete_vmdk:
            vdm = self.soap_client.content.virtualDiskManager
            task = vdm.DeleteVirtualDisk(
                name=backend_disk.backing.fileName,
                datacenter=self.get_disk_datacenter(backend_disk),
            )
            pyVim.task.WaitForTask(task)
            signals.vm_updated.send(self.__class__, vm=disk.vm)

    def extend_disk(self, disk):
        """
        Increase disk capacity.

        :param disk: Virtual disk to be extended.
        :type disk: :class:`waldur_vmware.models.Disk`
        """
        backend_disk = self.get_backend_disk(disk)
        vdm = self.soap_client.content.virtualDiskManager
        task = vdm.ExtendVirtualDisk(
            name=backend_disk.backing.fileName,
            datacenter=self.get_disk_datacenter(backend_disk),
            newCapacityKb=disk.size * 1024
        )
        pyVim.task.WaitForTask(task)
        signals.vm_updated.send(self.__class__, vm=disk.vm)

    def get_object(self, vim_type, vim_id):
        """
        Get object by type and ID from SOAP client.
        """
        content = self.soap_client.content
        items = [item for item in content.viewManager.CreateContainerView(
            content.rootFolder, [vim_type], recursive=True
        ).view]
        for item in items:
            if item._moId == vim_id:
                return item

    def get_backend_vm(self, vm):
        """
        Get virtual machine object from SOAP client.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        :rtype: :class:`pyVmomi.VmomiSupport.vim.VirtualMachine`
        """
        return self.get_object(vim.VirtualMachine, vm.backend_id)

    def get_backend_disk(self, disk):
        """
        Get virtual disk object from SOAP client.

        :param disk: Virtual disk.
        :type disk: :class:`waldur_vmware.models.Disk`
        :rtype: :class:`pyVmomi.VmomiSupport.vim.vm.device.VirtualDisk`
        """
        backend_vm = self.get_backend_vm(disk.vm)
        for device in backend_vm.config.hardware.device:
            if isinstance(device, vim.VirtualDisk) and str(device.key) == disk.backend_id:
                return device

    def get_disk_datacenter(self, backend_disk):
        """
        Find the datacenter where virtual disk is located.

        :param backend_disk: Virtual disk object returned by SOAP API.
        :type backend_disk: :class:`pyVmomi.VmomiSupport.vim.vm.device.VirtualDisk`
        :return: VMware datacenter where disk is located.
        :rtype: :class:`pyVmomi.VmomiSupport.vim.Datacenter`
        """
        parent = backend_disk.backing.datastore.parent
        while parent and not isinstance(parent, vim.Datacenter):
            parent = parent.parent
        return parent

    @log_backend_action()
    def pull_disk(self, disk, update_fields=None):
        """
        Pull virtual disk from REST API and update its information in local database.

        :param disk: Virtual disk database object.
        :type disk: :class:`waldur_vmware.models.Disk`
        :param update_fields: iterable of fields to be updated
        :return: None
        """
        import_time = timezone.now()
        imported_disk = self.import_disk(disk.vm.backend_id, disk.backend_id, save=False)

        disk.refresh_from_db()
        if disk.modified < import_time:
            if not update_fields:
                update_fields = models.Disk.get_backend_fields()

            update_pulled_fields(disk, imported_disk, update_fields)

    def import_disk(self, backend_vm_id, backend_disk_id, save=True, service_project_link=None):
        """
        Import virtual disk by its ID.

        :param backend_vm_id: Virtual machine identifier
        :type backend_vm_id: str
        :param backend_disk_id: Virtual disk identifier
        :type backend_disk_id: str
        :param save: Save object in the database
        :type save: bool
        :param service_project_link: Service project link model object
        :rtype: :class:`waldur_vmware.models.Disk`
        """
        try:
            backend_disk = self.client.get_disk(backend_vm_id, backend_disk_id)
        except VMwareError as e:
            reraise(e)
            return

        disk = self._backend_disk_to_disk(backend_disk, backend_disk_id)
        if service_project_link is not None:
            disk.service_project_link = service_project_link
        if save:
            disk.save()

        return disk

    def _backend_disk_to_disk(self, backend_disk, backend_disk_id):
        """
        Build database model object for virtual disk from REST API spec.

        :param backend_disk: virtual disk specification
        :type backend_disk: dict
        :param backend_disk_id: Virtual disk identifier
        :type backend_disk_id: str
        :rtype: :class:`waldur_vmware.models.Disk`
        """
        return models.Disk(
            backend_id=backend_disk_id,
            name=backend_disk['label'],
            # Convert disk size from bytes to MiB
            size=backend_disk['capacity'] / 1024 / 1024,
            state=models.Disk.States.OK,
        )

    def get_console_url(self, vm):
        """
        Generates a virtual machine's remote console URL (VMRC)

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        ticket = self.soap_client.content.sessionManager.AcquireCloneTicket()
        return 'vmrc://clone:{ticket}@{host}/?moid={vm}'.format(
            ticket=ticket, host=self.host, vm=vm.backend_id)
