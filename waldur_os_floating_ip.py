#!/usr/bin/python
# has to be a full import due to ansible 2.0 compatibility
from ansible.module_utils.basic import *
from waldur_client import WaldurClient, WaldurClientException

DOCUMENTATION = '''
--- 
module: waldur_os_floating_ip
short_description: Assign floating IPs
version_added: 0.1
requirements:
  - "python = 2.7"
  - "requests"
  - "python-waldur-client"
options: 
  access_token: 
    description: 
      - An access token which has permissions to create an OpenStack instances.
    required: true
  address: 
    description: 
      - an IP address of the floating IP to be assigned to the instance.
    required: if 'floating_ips' are not provided.
  api_url: 
    description: 
      - Fully qualified url to the Waldur.
    required: true
  floating_ips: 
    description: 
      - A list of floating IPs to be assigned to the instance. 
      A floating ip consists of 'subnet' and 'address'.
    required: if 'floating_ips' are not provided.
  subnet: 
    description: 
      - A subnet to be assigned to the instance.
    required: if 'floating_ips' are not provided.
  instance: 
    description: 
      - The name of the virtual machine to assign floating IPs to.
    required: True
'''

EXAMPLES = '''
- name: assign multipe floating IPs
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

- name: assign floating ip
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
    fields = {
        'api_url': {'required': True, 'type': 'str'},
        'access_token': {'required': True, 'type': 'str'},
        'instance': {'type': 'str'},
        'floating_ips': {'type': 'list'},
        'address': {'type': 'str'},
        'subnet': {'type': 'str'},
    }
    required_together = [['address', 'subnet']]
    mutually_exclusive = [['floating_ips', 'subnet'],
                          ['floating_ips', 'address']]
    required_one_of = mutually_exclusive
    module = AnsibleModule(
        argument_spec=fields,
        required_together=required_together,
        required_one_of=required_one_of,
        mutually_exclusive=mutually_exclusive)

    client = WaldurClient(module.params['api_url'], module.params['access_token'])
    floating_ips = module.params.get('floating_ips') or [{
        'address': module.params['address'],
        'subnet': module.params['subnet'],
    }]
    instance = module.params['instance']

    try:
        response = client.assign_floating_ips(instance=instance, floating_ips=floating_ips)
    except WaldurClientException as e:
        module.fail_json(msg=e.message)
    else:
        module.exit_json(meta=response)


if __name__ == '__main__':
    main()
