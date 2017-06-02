#!/usr/bin/python
# has to be a full import due to Ansible 2.0 compatibility
from ansible.module_utils.basic import *
from waldur_client import WaldurClient, WaldurClientException

DOCUMENTATION = '''
---
module: waldur_os_security_group
short_description: Add/Update/Remove OpenStack tenant security group
version_added: "0.1"
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
    required: if 'rules' are not provided.
  tenant:
    description:
      - The name of the tenant to create a security group for.
    required: false
  description:
    description:
      - A description of the security group.
    required: true
  from_port:
    description:
      - The lowest port value the security group rule is applied to.
    required: if 'rules' are not provided.
  protocol:
    description:
      - A protocol the security group rule is applied to.
    name:
      description:
        - The name of the security group.
      required: true
    required: if 'rules' are not provided.
  rules:
    description:
      - A list of security group rules to be applied to the security group.
        A rule consists of 4 fields: 'to_port', 'from_port', 'cidr' and 'protocol'
    required: if 'to_port', 'from_port', 'cidr' and 'protocol' are not specified.
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
    required: if 'rules' are not provided.
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
        tenant: VPC #1
        rules:
          - from_port: 80
            to_port: 80
            cidr: 0.0.0.0/0
            protocol: tcp
          - from_port: 443
            to_port: 443
            cidr: 0.0.0.0/0
            protocol: tcp
        state: absent
        name: classic-web

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
'''


def send_request_to_waldur(client, module):
    has_changed = False
    name = module.params['name']
    security_group = client.get_security_group(tenant=module.params['tenant'], name=name)
    present = module.params['state'] == 'present'
    if security_group:
        if present:
            if security_group['description'] != module.params.get('description'):
                client.update_security_group_description(
                    security_group,
                    description=module.params.get('description'))
                has_changed = True
        else:
            client.delete_security_group(security_group['uuid'])
            has_changed = True
    elif present:
        rules = module.params.get('rules') or [{
            'from_port': module.params['from_port'],
            'to_port': module.params['to_port'],
            'cidr': module.params['cidr'],
            'protocol': module.params['protocol'],
        }]
        client.create_security_group(
            tenant=module.params['tenant'],
            name=module.params['name'],
            description=module.params.get('description'),
            rules=rules)
        has_changed = True

    return has_changed


def main():
    fields = {
        'api_url': {'required': True, 'type': 'str'},
        'access_token': {'required': True, 'type': 'str'},
        'description': {'type': 'str'},
        'rules': {'type': 'list'},
        'from_port': {'type': 'str'},
        'to_port': {'type': 'str'},
        'cidr': {'type': 'str'},
        'protocol': {'type': 'str', 'choices': ['tcp', 'udp', 'icmp']},
        'state': {'default': 'present', 'choices': ['absent', 'present']},
        'name': {'required': True, 'type': 'str'},
        'tenant': {'required': True, 'type': 'str'},
    }
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

    client = WaldurClient(module.params['api_url'], module.params['access_token'])

    try:
        has_changed = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=e.message)
    else:
        module.exit_json(has_changed=has_changed)


if __name__ == '__main__':
    main()
