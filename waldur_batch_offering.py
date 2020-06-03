#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule, text_type

from waldur_client import WaldurClientException, waldur_client_from_module

ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'OpenNode',
}

DOCUMENTATION = '''
---
options:
    api_url:
        description:
            - Fully qualified URL to the Waldur.
        required: true
    access_token:
        description:
            - An access token which has permissions to create offerings.
        required: true
    name:
        description:
            - The name of the new batch offering.
        required: true
    description:
        description:
            - The description of the new batch offering.
    full_description:
        description:
            - The full description of the new batch offering.
    native_name:
        description:
            - The native name of the new batch offering.
    native_description:
        description:
            - The native description of the new batch offering.
    terms_of_service:
        description:
            - The terms of the service.
    provider:
        description:
            - The name or UUID of the provider organization.
    rating:
        description:
            - The rating form 1 to 5.
    category:
        description:
            - The name or UUID of the marketplace category.
        required: true
    attributes:
        description:
            - The attributes of the marketplace category.
    geolocations:
        description:
            - The geolocations of provided service.
    plans:
        description:
            - The list of plans attached to offering.
        required: true
    batch_service:
        description:
            - The batch service of allocation (MOAB or SLURM).
        required: true
    hostname:
        description:
            - Hostname or IP address of master node.
        required: true
    username:
        description:
            - Username for SSH connection.
        required: true
    port:
        description:
            - Port of the master node process.
    gateway:
        description:
            - Hostname or IP address of gateway node.
    use_sudo:
        description:
            - The flag for privilege escalation activation.
    default_account:
        description:
            - Default SLURM account for user.
        required: true
    shared:
        description:
            - The flag of a possible access to all organizations.
    billable:
        description:
            - The flag if purchase and usage is invoiced.
    datacite_doi:
        description:
            - Persistent ID for the service.
'''

EXAMPLES = '''
---
- hosts: localhost
  gather_facts: no
  tasks:
    - name: Create HPC allocation
      check_mode: no
      waldur_batch_offering:
        access_token: 3cecd050b7c1dbce54bc45bf508ec836aecdd5bc
        api_url: http://193.40.155.148:8000/api
        name: Offering sample name
        native_name:  Offering sample native name
        description: Offering description
        native_description: Offering native description
        full_description: Offering full description
        terms_of_service: Sample terms
        category: dfe535350663441e99615b0be014c742
        provider: cba70dd7f75d40dc9650704198e9071a
        batch_service: SLURM
        hostname: localhost
        port: 8080
        username: user
        default_account: root
        gateway: localhost
        shared: true
        plans:
        - name: HPC plan
          unit: month
          prices:
            cpu: 100
            gpu: 70
            ram: 64
        geolocations:
        - latitude: 59.3990796
          longitude: 26.6625565
'''


def format_params(params):
    excluded_keys = ['batch_service', 'hostname', 'username', 'default_account',
                     'port', 'gateway', 'type', 'provider']

    formatted_params = {k: v for (k, v) in params.items() if v and
                        k not in excluded_keys}

    formatted_params['type'] = 'SlurmInvoices.SlurmPackage'

    if params.get('provider'):
        formatted_params['customer'] = params['provider']

    service_attributes = {
        'batch_service': params['batch_service'],
        'hostname': params['hostname'],
        'username': params['username'],
        'default_account': params['default_account'],
    }
    if params.get('port'):
        service_attributes['port'] = params['port']
    if params.get('gateway'):
        service_attributes['gateway'] = params['gateway']
    formatted_params['service_attributes'] = service_attributes

    return formatted_params


def send_request_to_waldur(client, module):
    params = format_params(module.params)
    offering, changed = client.create_offering(params, module.check_mode)
    return offering, changed


def main():
    fields = {
        'api_url': {'required': True, 'type': 'str'},
        'access_token': {'required': True, 'type': 'str'},
        # Overview
        'name': {'required': True, 'type': 'str'},
        'description': {'required': False, 'type': 'str'},
        'full_description': {'required': False, 'type': 'str'},
        'native_name': {'required': False, 'type': 'str'},
        'native_description': {'required': False, 'type': 'str'},
        'terms_of_service': {'required': False, 'type': 'str'},
        # Organization details
        'provider': {'required': False, 'type': 'str'},
        'rating': {'required': False, 'type': 'int', 'choices': list(range(1, 6))},
        # Description
        'category': {'required': True, 'type': 'str'},
        'attributes': {'required': False, 'type': 'dict'},  # category attributes
        'geolocations': {'required': False, 'type': 'list'},
        # Accounting
        'plans': {'required': True, 'type': 'list'},
        # Management
        'batch_service': {
            'required': True,
            'type': 'str',
            'choices': ['SLURM', 'MOAB'],
        },
        'hostname': {'required': True, 'type': 'str'},
        'username': {'required': True, 'type': 'str'},
        'port': {'required': False, 'type': 'int'},
        'gateway': {'required': False, 'type': 'str'},
        'use_sudo': {'required': False, 'type': 'str'},
        'default_account': {'required': True, 'type': 'str'},
        'shared': {'required': False, 'type': 'bool'},
        'billable': {'required': False, 'type': 'bool'},
        'datacite_doi': {'required': False, 'type': 'str'},
    }

    module = AnsibleModule(argument_spec=fields, supports_check_mode=True)

    client = waldur_client_from_module(module)

    try:
        offering, changed = send_request_to_waldur(client, module)

        module.exit_json(offering=offering, changed=changed)
    except WaldurClientException as e:
        module.fail_json(msg=text_type(e))


if __name__ == '__main__':
    main()
