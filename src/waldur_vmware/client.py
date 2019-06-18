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

    def start_vm(self, vm_id):
        """
        Power on given virtual machine.

        :param vm_id: virtual machine ID
        :type vm_id: string
        """
        url = '{0}/vcenter/vm/{1}/power/start'.format(self._base_url, vm_id)
        return self._session.post(url)

    def stop_vm(self, vm_id):
        """
        Power off given virtual machine.

        :param vm_id: virtual machine ID
        :type vm_id: string
        """
        url = '{0}/vcenter/vm/{1}/power/stop'.format(self._base_url, vm_id)
        return self._session.post(url)

    def create_vm(self, spec):
        """
        Creates a virtual machine.
        """
        url = '{0}/vcenter/vm'.format(self._base_url)
        return self._session.post(url, json=spec)

    def delete_vm(self, vm_id):
        """
        Deletes a virtual machine.
        """
        url = '{0}/vcenter/vm/{1}'.format(self._base_url, vm_id)
        return self._session.delete(url)
