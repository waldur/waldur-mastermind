import sys
import ssl

import requests
from django.conf import settings
from django.utils import six
from django.utils.functional import cached_property
import pyVim.task
import pyVim.connect
from pyVmomi import vim

from waldur_core.structure import ServiceBackend, ServiceBackendError
from waldur_mastermind.common.utils import parse_datetime
from waldur_vmware.client import VMwareClient

from . import models


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
        client = VMwareClient(self.host, verify_ssl=False)
        client.login(self.settings.username, self.settings.password)
        return client

    @cached_property
    def soap_client(self):
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
        except requests.RequestException as e:
            if raise_exception:
                reraise(e)
            return False
        else:
            return True

    def pull_service_properties(self):
        self.pull_templates()

    def pull_templates(self):
        try:
            backend_templates = self.client.list_all_templates()
        except requests.RequestException as e:
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
                guest_os=template['guest_OS'],
            )

        models.Template.objects.filter(settings=self.settings, backend_id__in=stale_ids).delete()

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

        backend_vm = self.client.get_vm(backend_id)
        vm.backend_id = backend_id
        vm.runtime_state = backend_vm['power_state']
        vm.save(update_fields=['backend_id', 'runtime_state'])

        for disk in backend_vm['disks']:
            disk_backend_id = disk['key']
            disk_name = disk['value']['label']
            # Convert disk size from bytes to MiB
            disk_size = disk['value']['capacity'] / 1024 / 1024
            models.Disk.objects.create(
                vm=vm,
                service_project_link=vm.service_project_link,
                backend_id=disk_backend_id,
                name=disk_name,
                size=disk_size,
                state=models.Disk.States.OK,
            )

        return vm

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
            'placement': {
                'folder': settings.WALDUR_VMWARE['VM_FOLDER'],
                'resource_pool': settings.WALDUR_VMWARE['VM_RESOURCE_POOL'],
            }
        }

        try:
            return self.client.deploy_vm_from_template(vm.template.backend_id, {'spec': spec})
        except requests.RequestException as e:
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
            'placement': {
                'datastore': settings.WALDUR_VMWARE['VM_DATASTORE'],
                'folder': settings.WALDUR_VMWARE['VM_FOLDER'],
                'resource_pool': settings.WALDUR_VMWARE['VM_RESOURCE_POOL'],
            }
        }

        try:
            return self.client.create_vm({'spec': spec})
        except requests.RequestException as e:
            reraise(e)

    def delete_virtual_machine(self, vm):
        """
        Deletes a virtual machine.

        :param vm: Virtual machine to be deleted
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.delete_vm(vm.backend_id)
        except requests.RequestException as e:
            reraise(e)

    def start_virtual_machine(self, vm):
        """
        Powers on a powered-off or suspended virtual machine.

        :param vm: Virtual machine to be started
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.start_vm(vm.backend_id)
        except requests.RequestException as e:
            reraise(e)

    def stop_virtual_machine(self, vm):
        """
        Powers off a powered-on or suspended virtual machine.

        :param vm: Virtual machine to be stopped
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.stop_vm(vm.backend_id)
        except requests.RequestException as e:
            reraise(e)

    def reset_virtual_machine(self, vm):
        """
        Resets a powered-on virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.reset_vm(vm.backend_id)
        except requests.RequestException as e:
            reraise(e)

    def suspend_virtual_machine(self, vm):
        """
        Suspends a powered-on virtual machine.

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        try:
            self.client.suspend_vm(vm.backend_id)
        except requests.RequestException as e:
            reraise(e)

    def update_virtual_machine(self, vm):
        """
        Updates CPU and RAM of virtual machine.
        """
        self.update_cpu(vm)
        self.update_memory(vm)

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
                    'spec': {
                        'cores_per_socket': vm.cores_per_socket,
                        'count': vm.cores,
                    }
                })
        except requests.RequestException as e:
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
                    'spec': {
                        'size_MiB': vm.ram
                    }
                })
        except requests.RequestException as e:
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
            backend_id = self.client.create_disk(disk.vm.backend_id, {'spec': spec})
        except requests.RequestException as e:
            reraise(e)
        else:
            disk.backend_id = backend_id
            disk.save(update_fields=['backend_id'])
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
        except requests.RequestException as e:
            reraise(e)

        if delete_vmdk:
            vdm = self.soap_client.content.virtualDiskManager
            task = vdm.DeleteVirtualDisk(
                name=backend_disk.backing.fileName,
                datacenter=self.get_disk_datacenter(backend_disk),
            )
            pyVim.task.WaitForTask(task)

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
        parent = backend_disk.backing.datastore.parent
        while parent and not isinstance(parent, vim.Datacenter):
            parent = parent.parent
        return parent

    def get_console_url(self, vm):
        """
        Generates a virtual machine's remote console URL (VMRC)

        :param vm: Virtual machine.
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
        ticket = self.soap_client.content.sessionManager.AcquireCloneTicket()
        return 'vmrc://clone:{ticket}@{host}/?moid={vm}'.format(
            ticket=ticket, host=self.host, vm=vm.backend_id)
