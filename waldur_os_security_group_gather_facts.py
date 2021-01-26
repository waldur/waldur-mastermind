#!/usr/bin/python
# has to be a full import due to Ansible 2.0 compatibility
import six
from ansible.module_utils.basic import AnsibleModule

from waldur_client import WaldurClientException, waldur_client_from_module

ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'OpenNode',
}

DOCUMENTATION = '''
---
module: waldur_os_security_group_gather_facts
short_description: Get OpenStack tenant security group
version_added: 0.1
description:
  - "Get OpenStack tenant security group"
requirements:
  - "python = 3.6"
  - "requests"
  - "python-waldur-client"
options:
  access_token:
    description:
      - An access token which has permission to read a security group.
    required: true
  api_url:
    description:
      - Fully qualified URL to the Waldur.
    required: true
  name:
    description:
      - The name of the security group.
    required: false
  tenant:
    description:
      - The name of the tenant.
    required: true
'''

EXAMPLES = '''
- name: get security group
  hosts: localhost
  tasks:
    - name: get security group
      waldur_os_security_group_gather_facts:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: VPC #1
        name: classic-web

- name: list tenant security groups
  hosts: localhost
  tasks:
    - name: list all security groups belonging to the tenant
      waldur_os_security_group_gather_facts:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: VPC #1
'''


def send_request_to_waldur(client, module):
    tenant = module.params['tenant']
    name = module.params['name']
    if name:
        return [client.get_security_group(tenant, name)]
    else:
        return client.list_security_group(tenant)


def main():
    fields = dict(
        api_url=dict(required=True, type='str'),
        access_token=dict(required=True, type='str', no_log=True),
        name=dict(type='str', required=False),
        tenant=dict(type='str', required=True),
    )
    module = AnsibleModule(argument_spec=fields)

    client = waldur_client_from_module(module)

    try:
        security_groups = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=six.text_type(e))
    else:

        module.exit_json(security_groups=security_groups)


if __name__ == '__main__':
    main()
