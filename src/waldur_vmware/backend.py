import sys

import requests
from django.conf import settings
from django.utils import six
from django.utils.functional import cached_property

from waldur_core.structure import ServiceBackend, ServiceBackendError
from waldur_vmware.client import VMwareClient


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
    def client(self):
        hostname = self.settings.backend_url.split('https://')[-1]
        client = VMwareClient(hostname, verify_ssl=False)
        client.login(self.settings.username, self.settings.password)
        return client

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

    def create_virtual_machine(self, vm):
        """
        Creates a virtual machine.

        :param vm: Virtual machine to be created
        :type vm: :class:`waldur_vmware.models.VirtualMachine`
        """
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
            backend_id = self.client.create_vm({'spec': spec})
        except requests.RequestException as e:
            reraise(e)
        else:
            backend_vm = self.client.get_vm(backend_id)
            vm.backend_id = backend_id
            vm.runtime_state = backend_vm['power_state']
            vm.save(update_fields=['backend_id', 'runtime_state'])
            return vm

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
