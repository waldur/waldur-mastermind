import logging

import requests

from waldur_vmware.exceptions import VMwareError

logger = logging.getLogger(__name__)


class VMwareClient:
    """
    Lightweight VMware vCenter Automation API client.
    See also: https://vmware.github.io/vsphere-automation-sdk-rest/vsphere/
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
        self._base_url = f'https://{self._host}/rest'
        self._session = requests.Session()
        self._session.verify = verify_ssl

    def _request(self, method, endpoint, json=None, **kwargs):
        url = f'{self._base_url}/{endpoint}'
        if json:
            json = {'spec': json}
        try:
            response = self._session.request(method, url, json=json, **kwargs)
        except requests.RequestException as e:
            raise VMwareError(e)

        status_code = response.status_code
        if status_code in (
            requests.codes.ok,
            requests.codes.created,
            requests.codes.accepted,
            requests.codes.no_content,
        ):
            if response.content:
                data = response.json()
                if isinstance(data, dict) and 'value' in data:
                    return data['value']
                return data
        else:
            raise VMwareError(response.content)

    def _get(self, endpoint, **kwargs):
        return self._request('get', endpoint, **kwargs)

    def _post(self, endpoint, **kwargs):
        return self._request('post', endpoint, **kwargs)

    def _patch(self, endpoint, **kwargs):
        return self._request('patch', endpoint, **kwargs)

    def _delete(self, endpoint, **kwargs):
        return self._request('delete', endpoint, **kwargs)

    def login(self, username, password):
        """
        Login to vCenter server using username and password.

        :param username: user to connect
        :type username: string
        :param password: password of the user
        :type password: string
        :raises Unauthorized: raised if credentials are invalid.
        """
        self._post('com/vmware/cis/session', auth=(username, password))
        logger.info(f'Successfully logged in as {username}')

    def list_clusters(self):
        return self._get('vcenter/cluster')

    def get_cluster(self, cluster_id):
        return self._get(f'vcenter/cluster/{cluster_id}')

    def list_datacenters(self):
        return self._get('vcenter/datacenter')

    def list_datastores(self):
        return self._get('vcenter/datastore')

    def list_resource_pools(self):
        return self._get('vcenter/resource-pool')

    def list_networks(self):
        return self._get('vcenter/network')

    def list_folders(self, folder_type=None):
        """
        Returns information about folders in vCenter.
        :param folder_type: Type (DATACENTER, DATASTORE, HOST, NETWORK, VIRTUAL_MACHINE) of the vCenter Server folder.
        :rtype: List[Dict]
        """
        params = {}
        if folder_type:
            params['filter.type'] = folder_type
        return self._get('vcenter/folder', params=params)

    def list_vms(self):
        """
        Get all the VMs from vCenter inventory.
        """
        return self._get('vcenter/vm')

    def get_vm(self, vm_id):
        """
        Returns information about a virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._get(f'vcenter/vm/{vm_id}')

    def create_vm(self, spec):
        """
        Creates a virtual machine.

        :param spec: new virtual machine specification
        :type spec: dict
        :return: Virtual machine identifier
        :rtype: string
        """
        return self._post('vcenter/vm', json=spec)

    def delete_vm(self, vm_id):
        """
        Deletes a virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._delete(f'vcenter/vm/{vm_id}')

    def start_vm(self, vm_id):
        """
        Power on given virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._post(f'vcenter/vm/{vm_id}/power/start')

    def stop_vm(self, vm_id):
        """
        Power off given virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._post(f'vcenter/vm/{vm_id}/power/stop')

    def reset_vm(self, vm_id):
        """
        Resets a powered-on virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._post(f'vcenter/vm/{vm_id}/power/reset')

    def suspend_vm(self, vm_id):
        """
        Suspends a powered-on virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._post(f'vcenter/vm/{vm_id}/power/suspend')

    def get_guest_power(self, vm_id):
        """
        Returns information about the guest operating system power state.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._get(f'vcenter/vm/{vm_id}/guest/power')

    def shutdown_guest(self, vm_id):
        """
        Issues a request to the guest operating system asking
        it to perform a clean shutdown of all services.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._post(f'vcenter/vm/{vm_id}/guest/power?action=shutdown')

    def reboot_guest(self, vm_id):
        """
        Issues a request to the guest operating system asking it to perform a reboot.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._post(f'vcenter/vm/{vm_id}/guest/power?action=reboot')

    def get_cpu(self, vm_id):
        """
        Returns the CPU-related settings of a virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._get(f'vcenter/vm/{vm_id}/hardware/cpu')

    def update_cpu(self, vm_id, spec):
        """
        Updates the CPU-related settings of a virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        :param spec: CPU specification
        :type spec: dict
        """
        return self._patch(f'vcenter/vm/{vm_id}/hardware/cpu', json=spec)

    def get_memory(self, vm_id):
        """
        Returns the memory-related settings of a virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        """
        return self._get(f'vcenter/vm/{vm_id}/hardware/memory')

    def update_memory(self, vm_id, spec):
        """
        Updates the memory-related settings of a virtual machine.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        :param spec: CPU specification
        :type spec: dict
        """
        return self._patch(f'vcenter/vm/{vm_id}/hardware/memory', json=spec)

    def create_disk(self, vm_id, spec):
        """
        Adds a virtual disk to the virtual machine

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        :param spec: new virtual disk specification
        :type spec: dict
        """
        return self._post(f'vcenter/vm/{vm_id}/hardware/disk', json=spec)

    def get_disk(self, vm_id, disk_id):
        """
        Returns information about a virtual disk.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param disk_id: Virtual disk identifier.
        :type disk_id: string
        """
        return self._get(f'vcenter/vm/{vm_id}/hardware/disk/{disk_id}')

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
        return self._delete(f'vcenter/vm/{vm_id}/hardware/disk/{disk_id}')

    def connect_cdrom(self, vm_id, cdrom_id):
        """
        Connects a virtual CD-ROM device of a powered-on virtual machine to its backing.

        :param vm_id: Virtual machine identifier
        :type vm_id: string
        :param cdrom_id: Virtual CD-ROM device identifier.
        :type cdrom_id: string
        """
        return self._post(f'vcenter/vm/{vm_id}/hardware/cdrom/{cdrom_id}/connect')

    def disconnect_cdrom(self, vm_id, cdrom_id):
        """
        Disconnects a virtual CD-ROM device of a powered-on virtual machine from its backing.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param cdrom_id: Virtual CD-ROM device identifier.
        :type cdrom_id: string
        """
        return self._post(f'vcenter/vm/{vm_id}/hardware/cdrom/{cdrom_id}/disconnect')

    def create_nic(self, vm_id, network_id):
        """
        Adds a virtual Ethernet adapter to the virtual machine.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param network_id: Identifier of the network that backs the virtual Ethernet adapter.
        :type network_id: string
        """
        spec = {
            'backing': {
                'network': network_id,
                'type': 'DISTRIBUTED_PORTGROUP',
            },
            'start_connected': True,
        }
        return self._post(f'vcenter/vm/{vm_id}/hardware/ethernet', json=spec)

    def delete_nic(self, vm_id, nic_id):
        """
        Removes a virtual Ethernet adapter from the virtual machine.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param nic_id: Virtual Ethernet adapter identifier.
        :type nic_id: string
        """
        return self._delete(f'vcenter/vm/{vm_id}/hardware/ethernet/{nic_id}')

    def list_nics(self, vm_id):
        """
        Returns list of Ethernet adapters by virtual machine ID.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :rtype: List[Dict]
        """
        result = []
        for nic in self.list_nic_ids(vm_id):
            nic_payload = self.get_nic(vm_id, nic['nic'])
            nic_payload['nic'] = nic['nic']
            result.append(nic_payload)
        return result

    def list_nic_ids(self, vm_id):
        """
        Returns list of Ethernet adapter IDs by virtual machine ID.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        """
        return self._get(f'vcenter/vm/{vm_id}/hardware/ethernet')

    def get_nic(self, vm_id, nic_id):
        """
        Returns information about a virtual Ethernet adapter.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param nic_id: Virtual Ethernet adapter identifier.
        :type nic_id: string
        """
        return self._get(f'vcenter/vm/{vm_id}/hardware/ethernet/{nic_id}')

    def connect_nic(self, vm_id, nic_id):
        """
        Connects a virtual Ethernet adapter of a powered-on virtual machine to its backing.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param nic_id: Virtual Ethernet adapter identifier.
        :type nic_id: string
        """
        return self._post(f'vcenter/vm/{vm_id}/hardware/ethernet/{nic_id}/connect')

    def disconnect_nic(self, vm_id, nic_id):
        """
        Disconnects a virtual Ethernet adapter of a powered-on virtual machine from its backing.

        :param vm_id: Virtual machine identifier.
        :type vm_id: string
        :param nic_id: Virtual Ethernet adapter identifier.
        :type nic_id: string
        """
        return self._post(f'vcenter/vm/{vm_id}/hardware/ethernet/{nic_id}/disconnect')

    def list_libraries(self):
        return self._get('com/vmware/content/library')

    def list_library_items(self, library_id):
        params = {'library_id': library_id}
        return self._get('com/vmware/content/library/item', params=params)

    def get_library_item(self, library_item_id):
        return self._get(f'com/vmware/content/library/item/id:{library_item_id}')

    def get_template_library_item(self, library_item_id):
        return self._get(f'vcenter/vm-template/library-items/{library_item_id}')

    def list_all_templates(self):
        items = []
        for library_id in self.list_libraries():
            for library_item_id in self.list_library_items(library_id):
                library_item = self.get_library_item(library_item_id)
                if library_item['type'] == 'vm-template':
                    template = self.get_template_library_item(library_item_id)
                    items.append(
                        {
                            'library_item': library_item,
                            'template': template,
                        }
                    )
        return items

    def deploy_vm_from_template(self, library_item_id, spec):
        """
        Deploys a virtual machine as a copy of the source virtual machine
        template contained in the library item specified by library_item_id.

        :param library_item_id: identifier of the content library item containing the source virtual machine template to be deployed.
        :param spec: deployment specification
        :return: Identifier of the deployed virtual machine.
        :rtype: str
        """
        url = 'vcenter/vm-template/library-items/{}?action=deploy'.format(
            library_item_id
        )
        return self._post(url, json=spec)
