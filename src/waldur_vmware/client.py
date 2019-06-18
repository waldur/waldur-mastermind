import logging

import requests

from waldur_vmware.exceptions import Unauthorized

logger = logging.getLogger(__name__)


class VMwareClient(object):
    """
    Lightweight VMware vCenter Automation API client.
    See also: https://code.vmware.com/apis/191/vsphere-automation
    """

    def __init__(self, host, verify_ssl=True):
        """
        Initialize client with connection options.

        :param host: VMware vCenter server IP address / FQDN
        :type host: string
        :param verify_ssl: verify SSL certificates for HTTPS requests
        :type verify_ssl: bool
        """
        self._host = host
        self._base_url = 'https://{0}/rest'.format(self._host)
        self._session = requests.Session()
        self._session.verify = verify_ssl

    def login(self, username, password):
        """
        Login to vCenter server using username and password.

        :param username: user to connect
        :type username: string
        :param password: password of the user
        :type password: string
        :raises Unauthorized: raised if credentials are invalid.
        """
        login_url = '{0}/com/vmware/cis/session'.format(self._base_url)
        response = self._session.post(login_url, auth=(username, password))

        if not response.ok:
            raise Unauthorized(response.content)

        logger.info('Successfully logged in as {0}'.format(username))

    def get_vms(self):
        """
        Get all the VMs from vCenter inventory.
        """
        url = '{0}/vcenter/vm'.format(self._base_url)
        response = self._session.get(url)
        if response.ok:
            return response.json()['value']

    def create_vm(self, spec):
        """
        Creates a virtual machine.

        :param spec: new virtual machine specification
        :type spec: dict
        """
        url = '{0}/vcenter/vm'.format(self._base_url)
        return self._session.post(url, json=spec)

    def delete_vm(self, vm_id):
        """
        Deletes a virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        url = '{0}/vcenter/vm/{1}'.format(self._base_url, vm_id)
        return self._session.delete(url)

    def start_vm(self, vm_id):
        """
        Power on given virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        url = '{0}/vcenter/vm/{1}/power/start'.format(self._base_url, vm_id)
        return self._session.post(url)

    def stop_vm(self, vm_id):
        """
        Power off given virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        url = '{0}/vcenter/vm/{1}/power/stop'.format(self._base_url, vm_id)
        return self._session.post(url)

    def reset_vm(self, vm_id):
        """
        Resets a powered-on virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        url = '{0}/vcenter/vm/{1}/power/reset'.format(self._base_url, vm_id)
        return self._session.post(url)

    def suspend_vm(self, vm_id):
        """
        Suspends a powered-on virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        url = '{0}/vcenter/vm/{1}/power/suspend'.format(self._base_url, vm_id)
        return self._session.post(url)

    def update_cpu(self, vm_id, spec):
        """
        Updates the CPU-related settings of a virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        :param spec: CPU specification
        :type spec: dict
        """
        url = '{0}/vcenter/vm/{1}/hardware/cpu'.format(self._base_url, vm_id)
        return self._session.patch(url, json=spec)

    def update_memory(self, vm_id, spec):
        """
        Updates the memory-related settings of a virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        :param spec: CPU specification
        :type spec: dict
        """
        url = '{0}/vcenter/vm/{1}/hardware/memory'.format(self._base_url, vm_id)
        return self._session.patch(url, json=spec)

    def create_disk(self, vm_id, spec):
        """
        Adds a virtual disk to the virtual machine

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        :param spec: new virtual disk specification
        :type spec: dict
        """
        url = '{0}/vcenter/vm/{1}/hardware/disk'.format(self._base_url, vm_id)
        return self._session.post(url, json=spec)

    def delete_disk(self, vm_id, disk_id):
        """
        Removes a virtual disk from the virtual machine.
        This operation does not destroy the VMDK file that backs the virtual disk.
        It only detaches the VMDK file from the virtual machine.
        Once detached, the VMDK file will not be destroyed when the virtual machine
        to which it was associated is deleted.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param disk_id: Virtual disk identifier.
        :type disk_id: string
        """
        url = '{0}/vcenter/vm/{1}/hardware/disk/{2}'.format(self._base_url, vm_id, disk_id)
        return self._session.delete(url)

    def connect_cdrom(self, vm_id, cdrom_id):
        """
        Connects a virtual CD-ROM device of a powered-on virtual machine to its backing.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        :param cdrom_id: Virtual CD-ROM device identifier.
        :type cdrom_id: string
        """
        url = '{0}/vcenter/vm/{1}/hardware/cdrom/{2}/connect'.format(self._base_url, vm_id, cdrom_id)
        return self._session.post(url)

    def disconnect_cdrom(self, vm_id, cdrom_id):
        """
        Disconnects a virtual CD-ROM device of a powered-on virtual machine from its backing.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param cdrom_id: Virtual CD-ROM device identifier.
        :type cdrom_id: string
        """
        url = '{0}/vcenter/vm/{1}/hardware/cdrom/{2}/disconnect'.format(self._base_url, vm_id, cdrom_id)
        return self._session.post(url)

    def connect_nic(self, vm_id, nic_id):
        """
        Connects a virtual Ethernet adapter of a powered-on virtual machine to its backing.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param nic_id: Virtual Ethernet adapter identifier.
        :type nic_id: string
        """
        url = '{0}/vcenter/vm/{1}/hardware/ethernet/{2}/connect'.format(self._base_url, vm_id, nic_id)
        return self._session.post(url)

    def disconnect_nic(self, vm_id, nic_id):
        """
        Disconnects a virtual Ethernet adapter of a powered-on virtual machine from its backing.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param nic_id: Virtual Ethernet adapter identifier.
        :type nic_id: string
        """
        url = '{0}/vcenter/vm/{1}/hardware/ethernet/{2}/disconnect'.format(self._base_url, vm_id, nic_id)
        return self._session.post(url)
