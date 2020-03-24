#!/usr/bin/python
# has to be a full import due to Ansible 2.0 compatibility
import six
from ansible.module_utils.basic import AnsibleModule

from waldur_client import (
    WaldurClientException,
    waldur_client_from_module,
    waldur_full_argument_spec,
)

ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'OpenNode',
}

DOCUMENTATION = '''
---
module: waldur_os_floating_ip
short_description: Assign floating IPs
version_added: 0.1
requirements:
  - "python = 3.6"
  - "requests"
  - "python-waldur-client"
options:
  access_token:
    description:
      - An access token which has permissions to create an OpenStack instances.
    required: true
  address:
    description:
      - IP address of the floating IP to be assigned to the instance.
        It is required if 'floating_ips' are not provided.
  api_url:
    description:
      - Fully qualified url to the Waldur.
    required: true
  floating_ips:
    description:
      - A list of floating IPs to be assigned to the instance.
        A floating IP consists of 'subnet' and 'address'.
        It is required if 'floating_ips' are not provided.
  subnet:
    description:
      - A subnet to be assigned to the instance.
        It is required if 'floating_ips' are not provided.
  instance:
    description:
      - The name of the virtual machine to assign floating IPs to.
    required: true
  interval:
    default: 20
    description:
      - An interval of the instance state polling.
  timeout:
    default: 600
    description:
      - The maximum amount of seconds to wait until the floating IP is assigned to instance.
  wait:
    default: true
    description:
      - A boolean value that defines whether client has to wait until the floating IP is assigned to instance.
'''

EXAMPLES = '''
- name: assign multiple floating IPs
  hosts: localhost
  tasks:
    - name: assign single floating IP
      waldur_os_floating_ip:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000
        instance: VM #1
        floating_ips:
            - address: 10.30.201.18
              subnet: vpc-1-tm-sub-net
            - address: 10.30.201.177
              subnet: vpc-2-tm-sub-net

- name: assign floating IP
  hosts: localhost
  tasks:
    - name: assign single floating IP
      waldur_os_floating_ip:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000
        instance: VM #3
        address: 10.30.201.19
        subnet: vpc-3-tm-sub-net
'''


def main():
    fields = waldur_full_argument_spec(
        instance=dict(type='str'),
        floating_ips=dict(type='list'),
        address=dict(type='str'),
        subnet=dict(type='str'),
    )
    required_together = [['address', 'subnet']]
    mutually_exclusive = [['floating_ips', 'subnet'], ['floating_ips', 'address']]
    required_one_of = mutually_exclusive
    module = AnsibleModule(
        argument_spec=fields,
        required_together=required_together,
        required_one_of=required_one_of,
        mutually_exclusive=mutually_exclusive,
    )

    client = waldur_client_from_module(module)
    floating_ips = module.params.get('floating_ips') or [
        {'address': module.params['address'], 'subnet': module.params['subnet'],}
    ]
    instance = module.params['instance']

    try:
        response = client.assign_floating_ips(
            instance=instance,
            floating_ips=floating_ips,
            wait=module.params['wait'],
            timeout=module.params['timeout'],
            interval=module.params['interval'],
        )
    except WaldurClientException as e:
        module.fail_json(msg=six.text_type(e))
    else:
        module.exit_json(meta=response)


if __name__ == '__main__':
    main()
