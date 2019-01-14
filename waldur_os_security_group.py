#!/usr/bin/python
# has to be a full import due to Ansible 2.0 compatibility
from ansible.module_utils.basic import *
import six

from waldur_client import WaldurClientException, waldur_client_from_module, \
    waldur_resource_argument_spec

ANSIBLE_METADATA = {'metadata_version': '1.1',
                    'status': ['preview'],
                    'supported_by': 'OpenNode'}

DOCUMENTATION = '''
---
module: waldur_os_security_group
short_description: Add/Update/Remove OpenStack tenant security group
version_added: 0.1
description:
  - "Add/Update/Remove OpenStack tenant security group"
requirements:
  - "python = 2.7"
  - "requests"
  - "python-waldur-client"
options:
  access_token:
    description:
      - An access token which has permissions to create a security group.
    required: true
  api_url:
    description:
      - Fully qualified URL to the Waldur.
    required: true
  cidr:
    description:
      - A CIDR the security group rule is applied to.
       It is required if 'rules' are not provided.
  description:
    description:
      - A description of the security group.
    required: false
  from_port:
    description:
      - The lowest port value the security group rule is applied to.
        It is required if 'rules' are not provided.
  interval:
    default: 20
    description:
      - An interval of the security group state polling.
  protocol:
    description:
      - A protocol the security group rule is applied to.
        It is required if 'rules' are not provided.
  name:
    description:
      - The name of the security group.
    required: true
  project:
    description:
      - Name or UUID of the Waldur project where OpenStack tenant is located.
    required: false
  rules:
    description:
      - A list of security group rules to be applied to the security group.
        A rule consists of 4 fields: 'to_port', 'from_port', 'cidr' and 'protocol'
        It is required if 'to_port', 'from_port', 'cidr' and 'protocol' are not specified.
  state:
    choices:
      - present
      - absent
    default: present
    description:
      - Should the resource be present or absent.
  to_port:
    description:
      - The highest port value the security group rule is applied to.
        It is required if 'rules' are not provided.
  tags:
    description:
      - List of tags that will be added to the security group on provisioning.
  tenant:
    description:
      - The name of the tenant to create a security group for.
    required: true
  timeout:
    default: 600
    description:
      - The maximum amount of seconds to wait until the security group provisioning is finished.
  wait:
    default: true
    description:
      - A boolean value that defines whether client has to wait until the security group is provisioned.
'''

EXAMPLES = '''
- name: add security group
  hosts: localhost
  tasks:
    - name: create security group
      waldur_os_security_group:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: VPC #1
        description: http and https ports group
        rules:
          - from_port: 80
            to_port: 80
            cidr: 0.0.0.0/0
            protocol: tcp
          - from_port: 443
            to_port: 443
            cidr: 0.0.0.0/0
            protocol: tcp
        state: present
        name: classic-web

- name: remove security group
  hosts: localhost
  tasks:
    - name: remove previous security group
      waldur_os_security_group:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        name: classic-web
        tenant: VPC #1
        state: absent

- name: add security group
  hosts: localhost
  tasks:
    - name: create security group with 1 security rule
      waldur_os_security_group:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: VPC #1
        description: http only
        from_port: 80
        to_port: 80
        cidr: 0.0.0.0/0
        protocol: tcp
        state: present
        name: classic-web
        tags:
            - ansible_application_id

- name: update rules of security group
  hosts: localhost
  tasks:
    - name: update rules of security group
      waldur_os_security_group:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: VPC #1
        state: present
        name: postgresql
        rules:
          - to_port: 22
            cidr: 0.0.0.0/0
            from_port: 22
            protocol: tcp

          - to_port: -1
            cidr: 0.0.0.0/0
            from_port: -1
            protocol: icmp

          - to_port: 5432
            cidr: 0.0.0.0/0
            from_port: 5432
            protocol: tcp
'''


def send_request_to_waldur(client, module):
    has_changed = False
    project = module.params.get('project')
    tenant = module.params['tenant']
    name = module.params['name']
    description = module.params.get('description') or ''
    rules = module.params.get('rules') or [{
        'from_port': module.params['from_port'],
        'to_port': module.params['to_port'],
        'cidr': module.params['cidr'],
        'protocol': module.params['protocol'],
    }]

    security_group = client.get_security_group(tenant, name)
    present = module.params['state'] == 'present'

    if security_group:
        if present:
            if security_group['description'] != description:
                client.update_security_group_description(security_group, description)
                has_changed = True

            if security_group['rules'] != rules:
                client.update_security_group_rules(security_group, rules)
                has_changed = True
        else:
            client.delete_security_group(security_group['uuid'])
            has_changed = True
    elif present:
        client.create_security_group(
            project=project,
            tenant=tenant,
            name=name,
            description=description,
            rules=rules,
            tags=module.params.get('tags'),
            wait=module.params['wait'],
            interval=module.params['interval'],
            timeout=module.params['timeout'])
        has_changed = True

    return has_changed


def main():
    fields = waldur_resource_argument_spec(
        rules=dict(type='list'),
        from_port=dict(type='str'),
        to_port=dict(type='str'),
        cidr=dict(type='str'),
        protocol=dict(type='str', choices=['tcp', 'udp', 'icmp']),
        project=dict(type='str', required=False),
        tenant=dict(type='str', required=True),
    )
    required_together = [['from_port', 'to_port', 'cidr', 'protocol']]
    mutually_exclusive = [['from_port', 'rules'],
                          ['to_port', 'rules'],
                          ['cidr', 'rules'],
                          ['protocol', 'rules']]
    required_one_of = mutually_exclusive
    module = AnsibleModule(
        argument_spec=fields,
        required_together=required_together,
        required_one_of=required_one_of,
        mutually_exclusive=mutually_exclusive)

    client = waldur_client_from_module(module)

    try:
        has_changed = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=six.text_type(e))
    else:
        module.exit_json(has_changed=has_changed)


if __name__ == '__main__':
    main()
