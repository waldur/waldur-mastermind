#!/usr/bin/python
# has to be a full import due to Ansible 2.0 compatibility
from ipaddress import AddressValueError, IPv4Interface, IPv6Interface, NetmaskValueError

import six
from ansible.module_utils.basic import AnsibleModule

from waldur_client import (
    WaldurClientException,
    waldur_client_from_module,
    waldur_resource_argument_spec,
)

ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'OpenNode',
}

DOCUMENTATION = '''
---
module: waldur_os_security_group
short_description: Add/Update/Remove OpenStack tenant security group
version_added: 0.1
description:
  - "Add/Update/Remove OpenStack tenant security group"
requirements:
  - "python = 3.6"
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
  description:
    description:
      - A description of the security group.
    required: false
  interval:
    default: 20
    description:
      - An interval of the security group state polling.
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
        A rule consists of 5 fields: 'to_port', 'from_port', 'protocol', 'ethertype',
        'direction' and either 'cidr' or 'remote_group' (remote group name).
        'ethertype' (IPv4 by default) and 'direction' (ingress by default)
        parameters are optional.
  state:
    choices:
      - present
      - absent
    default: present
    description:
      - Should the resource be present or absent.
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
        rules:
          - from_port: 80
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

- name: add security group with empty rules
  hosts: localhost
  tasks:
    - name: create security group
      waldur_os_security_group:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: VPC
        description: empty group
        state: present
        name: empty

- name: add security group using remote group
  hosts: localhost
  tasks:
    - name: create security group with a link to remote group
      waldur_os_security_group:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: VPC
        description: group depending on a remote group
        rules:
        - from_port: 80
          to_port: 80
          remote_group: web
          protocol: tcp
        state: present
        name: group_remote_group

- name: add security group with cidr ethertype
  hosts: localhost
  gather_facts: no
  tasks:
    - name: create security group with cidr ethertype
      waldur_os_security_group:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: waldur-dev-infra
        description: some descr
        rules:
        - from_port: 80
          to_port: 80
          cidr: 0.0.0.0/00
          protocol: tcp
          ethertype: IPv4
        state: present
        name: sec-group-with-ethertype

- name: add security group with IPv6 cidr
  hosts: localhost
  gather_facts: no
  tasks:
    - name: create security group with cidr ethertype
      waldur_os_security_group:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: waldur-dev-infra
        description: some descr
        rules:
        - from_port: 80
          to_port: 80
          cidr: 2002::/16
          protocol: tcp
          ethertype: IPv6
        state: present
        name: sec-group-with-ethertype

- name: add security group with ingress direction
  hosts: localhost
  gather_facts: no
  tasks:
    - name: create security group with direction
      waldur_os_security_group:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: waldur-dev-infra
        description: some descr
        rules:
        - from_port: 80
          to_port: 80
          cidr: 0.0.0.0/00
          protocol: tcp
          direction: ingress
        state: present
        name: sec-group-with-direction

- name: add security group with egress direction
  hosts: localhost
  gather_facts: no
  tasks:
    - name: create security group with direction
      waldur_os_security_group:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        tenant: waldur-dev-infra
        description: some descr
        rules:
        - from_port: 80
          to_port: 80
          cidr: 0.0.0.0/00
          protocol: tcp
          direction: egress
        state: present
        name: sec-group-with-direction
'''


def send_request_to_waldur(client, module):
    has_changed = False
    project = module.params.get('project')
    tenant = module.params['tenant']
    name = module.params['name']
    description = module.params.get('description') or ''
    rules = module.params['rules']
    for rule in rules:
        for item in ['from_port', 'to_port', 'protocol']:
            if item not in rule:
                module.fail_json(msg='A rule must contain %s parameter.' % item)

        if 'cidr' in rule and 'remote_group' in rule:
            module.fail_json(
                msg='Either cidr or remote_group must be specified, not both.'
            )

        if 'remote_group' in rule:
            remote_group = client.get_security_group(tenant, rule['remote_group'])
            rule['remote_group'] = remote_group['url']
        elif 'cidr' in rule:
            address = rule['cidr']
            if 'ethertype' not in rule or rule['ethertype'] == 'IPv4':
                try:
                    IPv4Interface(address)
                except (AddressValueError, NetmaskValueError) as e:
                    module.fail_json(msg='Invalid IPv4 address %s: %s' % (address, e))
                else:
                    rule['ethertype'] = 'IPv4'
            elif rule['ethertype'] == 'IPv6':
                try:
                    IPv6Interface(address)
                except (AddressValueError, NetmaskValueError) as e:
                    module.fail_json(msg='Invalid IPv6 address %s: %s' % (address, e))
            else:
                module.fail_json(msg='Invalid ethertype: %s' % rule['ethertype'])
        else:
            module.fail_json(msg='Either cidr or remote_group must be specified.')

        if 'direction' not in rule:
            rule['direction'] = 'ingress'
        else:
            if rule['direction'] not in ['ingress', 'egress']:
                module.fail_json(
                    msg='Invalid direction %s expected ingress or egress'
                    % rule['direction']
                )

    security_group = client.get_security_group(tenant, name)
    present = module.params['state'] == 'present'

    if security_group:
        if present:
            rules_comp = [
                {
                    k: v
                    for k, v in rule.items()
                    if k
                    in [
                        'from_port',
                        'to_port',
                        'cidr',
                        'protocol',
                        'direction',
                        'description',
                        'ethertype',
                    ]
                }
                for rule in security_group['rules']
            ]
            if security_group['description'] == description and rules_comp == rules:
                has_changed = False
            else:

                if security_group['description'] != description:
                    client.update_security_group_description(
                        security_group, description
                    )
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
            timeout=module.params['timeout'],
        )
        has_changed = True

    return has_changed


def main():
    fields = waldur_resource_argument_spec(
        rules=dict(type='list', required=False, default=[]),
        project=dict(type='str', required=False),
        tenant=dict(type='str', required=True),
    )
    module = AnsibleModule(
        argument_spec=fields,
    )

    client = waldur_client_from_module(module)

    try:
        has_changed = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=six.text_type(e))
    else:
        module.exit_json(changed=has_changed)


if __name__ == '__main__':
    main()
