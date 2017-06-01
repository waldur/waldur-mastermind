#!/usr/bin/python
# has to be a full import due to ansible 2.0 compatibility
from ansible.module_utils.basic import *
from waldur_client import WaldurClient, WaldurClientException

DOCUMENTATION = '''
--- 
module: waldur_os_add_instance
short_description: Create OpenStack instance
version_added: 0.1
description: 
  - Create an OpenStack instance
requirements:
  - python = 2.7
  - requests
  - python-waldur-client
options: 
  access_token: 
    description: 
      - An access token which has permissions to create an OpenStack instances.
    required: true
  api_url: 
    description: 
      - Fully qualified url to the Waldur.
    required: true
  data_volume_size: 
    default: volume is not created.
    description: 
      - The size of the data volume in GB.
    required: false
  flavor: 
    description: 
      - The name or id of the flavor to use.
    required: true
  floating_ip: 
    description: 
      - An id or address of the existing floating IP to use. 
      Not assigned if not specified. Use `auto` to allocate new floating IP or reuse available one.
    required: 
      - If a `networks` parameter is not provided.
  image: 
    description: 
      - The name or id of the image to use.
    required: true
  interval: 
    default: 20
    description: 
      - An interval of the instance state polling.
    required: false
  name: 
    description: 
      - The name of the new OpenStack instance.
    required: true
  networks: 
    description: 
      - A list of networks an instance has to be attached to. 
      A network object consists of 'floating_ip' and 'subnet' fields.
  project: 
    description: 
      - The name or id of the project to add an instance to.
    required: true
  provider: 
    description: 
      - The name or id of the instance provider.
    required: true
  security_groups: 
    default: default
    description: 
      - A list of ids or names of security groups to apply to the newly created instance.
    required: false
  ssh_key: 
    description: 
      - The name or id of the SSH key to attach to the newly created instance.
    required: false
  subnet: 
    description: 
      - The name or id of the subnet to use.
    required: 
      - If a `networks` parameter is not provided.
  system_volume_size: 
    description: 
      - The size of the system volume in GBs.
    required: true
  timeout: 
    default: "60 * 10"
    description: 
      - The maximum amount of seconds to wait until the instance provisioning is finished.
    required: false
  user_data: 
    description: 
      - An additional data that will be added to the instance on provisioning.
    required: false
  wait: 
    default: true
    description: 
      - A boolean value that defines whether client has to wait until the instance 
      provisioning is finished.
    required: false
    '''

EXAMPLES = '''
  name: Provision a warehouse instance
  waldur_os_add_instance: 
    access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
    api_url: https://waldur.example.com:8000
    data_volume_size: 100
    flavor: m1.micro
    image: Ubuntu 14.04
    name: Warehouse instance
    networks: 
      - 
        floating_ip: auto
        subnet: vpc-1-tm-sub-net
      - 
        floating_ip: 192.101.13.124
        subnet: vpc-1-tm-sub-net-2
    project: OpenStack Project
    provider: VPC
    security_groups: 
      - web
        
  name: Provision build instance
  waldur_os_add_instance: 
    access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
    api_url: https://waldur.example.com:8000
    flavor: m1.micro
    floating_ip: auto
    image: CentOS 7
    name: Build instance
    project: OpenStack Project
    provider: VPC
    ssh_key: ssh1.pub
    subnet: vpc-1-tm-sub-net-2
    system_volume_size: 40
    user_data: |-
        #cloud-config
        chpasswd:
          list: |
            ubuntu:{{ default_password }}
          expire: False
        
  name: Trigger master instance
  waldur_os_add_instance: 
    access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
    api_url: https://waldur.example.com:8000
    flavor: m1.micro
    floating_ip: auto
    image: CentOS 7
    name: Build instance
    project: OpenStack Project
    provider: VPC
    ssh_key: ssh1.pub
    subnet: vpc-1-tm-sub-net-2
    system_volume_size: 40
    wait: false
    '''


def main():
    fields = {
        'api_url': {'required': True, 'type': 'str'},
        'access_token': {'required': True, 'type': 'str'},
        'name': {'required': True, 'type': 'str'},
        'provider': {'required': True, 'type': 'str'},
        'project': {'required': True, 'type': 'str'},
        'flavor': {'required': True, 'type': 'str'},
        'image': {'required': True, 'type': 'str'},
        'system_volume_size': {'required': True, 'type': 'int'},
        'security_groups': {'type': 'list'},
        'networks': {'type': 'list'},
        'subnet': {'type': 'str'},
        'floating_ip': {'type': 'str'},
        'data_volume_size': {'type': 'int'},
        'ssh_key': {'type': 'str'},
        'user_data': {'type': 'str'},
        'wait': {'default': True, 'type': 'bool'},
        'timeout': {'default': 60 * 10, 'type': 'int'},
        'interval': {'default': 20, 'type': 'int'}
    }
    required_together = [['wait', 'timeout'], ['subnet', 'floating_ip']]
    mutually_exclusive = [['subnet', 'networks'], ['floating_ip', 'networks']]
    required_one_of = [['subnet', 'networks'], ['floating_ip', 'networks']]
    module = AnsibleModule(
        argument_spec=fields,
        required_together=required_together,
        required_one_of=required_one_of,
        mutually_exclusive=mutually_exclusive)

    client = WaldurClient(module.params['api_url'], module.params['access_token'])
    networks = module.params.get('networks') or [{
        'subnet': module.params['subnet'],
        'floating_ip': module.params['floating_ip']
        }]
    try:
        instance = client.create_instance(
            name=module.params['name'],
            provider=module.params['provider'],
            project=module.params['project'],
            networks=networks,
            security_groups=module.params['security_groups'],
            flavor=module.params['flavor'],
            image=module.params['image'],
            system_volume_size=module.params['system_volume_size'],
            data_volume_size=module.params['data_volume_size'],
            ssh_key=module.params['ssh_key'],
            wait=module.params['wait'],
            interval=module.params['interval'],
            timeout=module.params['timeout'],
            user_data=module.params['user_data'])
    except WaldurClientException as error:
        module.fail_json(msg=error.message)
    else:
        module.exit_json(meta=instance['url'])


if __name__ == '__main__':
    main()
