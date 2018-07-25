from __future__ import unicode_literals

import copy
from xml.etree.ElementTree import iselement  # nosec

from defusedxml.ElementTree import parse as parse_xml, ParseError as XMLParseError
from libcloud.utils.py3 import httplib
from libcloud.common.azure import AzureServiceManagementConnection as _AzureServiceManagementConnection
from libcloud.common.azure import AzureResponse as _AzureResponse
from libcloud.compute.drivers.azure import AzureNodeDriver as _AzureNodeDriver
from libcloud.common.types import InvalidCredsError
from libcloud.common.types import LibcloudError, MalformedResponseError


def fixxpath(root, xpath):
    """ElementTree wants namespaces in its xpaths, so here we add them."""
    namespace, root_tag = root.tag[1:].split("}", 1)
    fixed_xpath = "/".join(["{%s}%s" % (namespace, e)
                            for e in xpath.split("/")])
    return fixed_xpath


def parse_error(body):
    code = body.findtext(fixxpath(body, 'Code'))
    message = body.findtext(fixxpath(body, 'Message'))
    message = message.split('\n')[0]
    error_msg = '%s: %s' % (code, message)
    return error_msg


"""
actual VM configs are not supported:
https://azure.microsoft.com/en-gb/pricing/details/virtual-machines/linux/

previous and currently supported virtual machine configs:
https://azure.microsoft.com/en-gb/pricing/details/virtual-machines/linux-previous/
"""
AZURE_COMPUTE_INSTANCE_TYPES = {
    'A0': {
        'id': 'ExtraSmall',
        'name': 'Extra Small Instance',
        'ram': 768,
        'disk': 20,
        'bandwidth': None,
        'price': '0.017',
        'max_data_disks': 1,
        'cores': 'Shared'
    },
    'A1': {
        'id': 'Small',
        'name': 'Small Instance',
        'ram': 1792,
        'disk': 70,
        'bandwidth': None,
        'price': '0.051',
        'max_data_disks': 2,
        'cores': 1
    },
    'A2': {
        'id': 'Medium',
        'name': 'Medium Instance',
        'ram': 3584,
        'disk': 135,
        'bandwidth': None,
        'price': '0.102',
        'max_data_disks': 4,
        'cores': 2
    },
    'A3': {
        'id': 'Large',
        'name': 'Large Instance',
        'ram': 7168,
        'disk': 285,
        'bandwidth': None,
        'price': '0.203',
        'max_data_disks': 8,
        'cores': 4
    },
    'A4': {
        'id': 'ExtraLarge',
        'name': 'Extra Large Instance',
        'ram': 14336,
        'disk': 605,
        'bandwidth': None,
        'price': '0.405',
        'max_data_disks': 16,
        'cores': 8
    },
    'A5': {
        'id': 'A5',
        'name': 'Memory Intensive Instance',
        'ram': 14336,
        'disk': 135,
        'bandwidth': None,
        'price': '0.228',
        'max_data_disks': 4,
        'cores': 2
    },
    'A6': {
        'id': 'A6',
        'name': 'A6 Instance',
        'ram': 28672,
        'disk': 285,
        'bandwidth': None,
        'price': '0.456',
        'max_data_disks': 8,
        'cores': 4
    },
    'A7': {
        'id': 'A7',
        'name': 'A7 Instance',
        'ram': 57344,
        'disk': 605,
        'bandwidth': None,
        'price': '0.911',
        'max_data_disks': 16,
        'cores': 8
    },
    'A8': {
        'id': 'A8',
        'name': 'A8 Instance',
        'ram': 57344,
        'disk': 382,
        'bandwidth': None,
        'price': '0.946',
        'max_data_disks': 16,
        'cores': 8
    },
    'A9': {
        'id': 'A9',
        'name': 'A9 Instance',
        'ram': 114688,
        'disk': 382,
        'bandwidth': None,
        'price': '1.892',
        'max_data_disks': 16,
        'cores': 16
    },
    'A10': {
        'id': 'A10',
        'name': 'A10 Instance',
        'ram': 57344,
        'disk': 382,
        'bandwidth': None,
        'price': '0.757',
        'max_data_disks': 16,
        'cores': 8
    },
    'A11': {
        'id': 'A11',
        'name': 'A11 Instance',
        'ram': 114688,
        'disk': 382,
        'bandwidth': None,
        'price': '1.513',
        'max_data_disks': 16,
        'cores': 16
    },
    'D1': {
        'id': 'Standard_D1',
        'name': 'D1 Faster Compute Instance',
        'ram': 3584,
        'disk': 50,
        'bandwidth': None,
        'price': '0.071',
        'max_data_disks': 2,
        'cores': 1
    },
    'D2': {
        'id': 'Standard_D2',
        'name': 'D2 Faster Compute Instance',
        'ram': 7168,
        'disk': 100,
        'bandwidth': None,
        'price': '0.142',
        'max_data_disks': 4,
        'cores': 2
    },
    'D3': {
        'id': 'Standard_D3',
        'name': 'D3 Faster Compute Instance',
        'ram': 14336,
        'disk': 200,
        'bandwidth': None,
        'price': '0.284',
        'max_data_disks': 8,
        'cores': 4
    },
    'D4': {
        'id': 'Standard_D4',
        'name': 'D4 Faster Compute Instance',
        'ram': 28672,
        'disk': 400,
        'bandwidth': None,
        'price': '0.567',
        'max_data_disks': 16,
        'cores': 8
    },
    'D11': {
        'id': 'Standard_D11',
        'name': 'D11 Faster Compute Instance',
        'ram': 14336,
        'disk': 100,
        'bandwidth': None,
        'price': '0.189',
        'max_data_disks': 4,
        'cores': 2
    },
    'D12': {
        'id': 'Standard_D12',
        'name': 'D12 Faster Compute Instance',
        'ram': 28672,
        'disk': 200,
        'bandwidth': None,
        'price': '0.379',
        'max_data_disks': 8,
        'cores': 4
    },
    'D13': {
        'id': 'Standard_D13',
        'name': 'D13 Faster Compute Instance',
        'ram': 57344,
        'disk': 400,
        'bandwidth': None,
        'price': '0.757',
        'max_data_disks': 16,
        'cores': 8
    },
    'D14': {
        'id': 'Standard_D14',
        'name': 'D14 Faster Compute Instance',
        'ram': 114688,
        'disk': 800,
        'bandwidth': None,
        'price': '1.492',
        'max_data_disks': 32,
        'cores': 16
    }
}


class AzureResponse(_AzureResponse):
    """
    Fix error parsing for Azure
    """

    def parse_error(self, msg=None):
        error_msg = 'Unknown error'

        try:
            # Azure does give some meaningful errors, but is inconsistent
            # Some APIs respond with an XML error. Others just dump HTML
            body = self.parse_body()

            if iselement(body):
                error_msg = parse_error(body)

        except MalformedResponseError:
            pass

        if msg:
            error_msg = '%s - %s' % (msg, error_msg)

        if self.status in [httplib.UNAUTHORIZED, httplib.FORBIDDEN]:
            raise InvalidCredsError(error_msg)

        raise LibcloudError(
            '%s Status code: %d.' % (error_msg, self.status),
            driver=self
        )


class AzureServiceManagementConnection(_AzureServiceManagementConnection):
    responseCls = AzureResponse


class AzureNodeDriver(_AzureNodeDriver):
    connectionCls = AzureServiceManagementConnection

    def raise_for_response(self, response, valid_response):
        if response.status != valid_response:
            error_msg = response.body
            try:
                body = parse_xml(error_msg)
                if iselement(body):
                    error_msg = parse_error(body)
            except XMLParseError:
                pass

            values = (response.error, error_msg, response.status)
            message = 'Message: %s, Body: %s, Status code: %s' % (values)
            raise LibcloudError(message, driver=self)

    def _parse_response_body_from_xml_text(self, response, return_type):
        """
        parse the xml and fill all the data into a class of return_type
        """
        # required to support libcloud v[2.0, 2.1]
        if isinstance(response.body, unicode):
            response.body = response.body.encode('utf-8')

        return super(AzureNodeDriver, self)._parse_response_body_from_xml_text(response, return_type)

    def list_sizes(self):
        """
        Replaces AzureNodeDriver's list_sizes due to price change in Azure.
        """
        sizes = []

        for _, values in AZURE_COMPUTE_INSTANCE_TYPES.items():
            node_size = self._to_node_size(copy.deepcopy(values))
            sizes.append(node_size)

        return sizes
