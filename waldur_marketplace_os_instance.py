#!/usr/bin/python
# has to be a full import due to Ansible 2.0 compatibility
import six
from ansible.module_utils.basic import AnsibleModule

from waldur_client import (
    ObjectDoesNotExist,
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
module: waldur_marketplace_os_instance
short_description: Create, update or delete OpenStack instance via marketplace
version_added: 0.8
description:
  - Create, update or delete OpenStack compute instance via Waldur API.
requirements:
  - python = 3.6
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
    description:
      - The size of the data volume in GB. Data volume is not created if value is empty.
  data_volume_type:
    description:
      - UUID or name of data volume type.
  delete_volumes:
    description:
      - If true, delete volumes when deleting instance.
    default: true
  flavor:
    description:
      - The name or id of the flavor to use.
        If this is not declared, flavor_min_cpu and/or flavor_min_ram must be declared.
  flavor_min_cpu:
    description:
      - The minimum cpu count.
  flavor_min_ram:
    description:
      - The minimum ram size (MB).
  floating_ip:
    description:
      - An id or address of the existing floating IP to use.
        Not assigned if not specified. Use `auto` to allocate new floating IP or reuse available one.
        It is required if a `networks` parameter is not provided.
  image:
    description:
      - The name or id of the image to use.
        It is required if is state is 'present'.
  interval:
    default: 20
    description:
      - An interval of the instance state polling.
  name:
    description:
      - The name of the new OpenStack instance or UUID for existing instance.
    required: true
  networks:
    description:
      - A list of networks an instance has to be attached to.
        A network object consists of 'floating_ip' and 'subnet' fields.
        It is required if neither 'floating_ip' nor 'subnet' provided.
  project:
    description:
      - The name or UUID of the project to add an instance to.
        It is required if is state is 'present'.
  offering:
    description:
      - The name or UUID of the marketplace offering.
        It is  required if is state is 'present'.
  release_floating_ips:
    description:
      - When state is absent and this option is true, any floating IP
        associated with the instance will be deleted along with the instance.
    default: true
  security_groups:
    default: default
    description:
      - A list of ids or names of security groups to apply to the newly created instance.
  ssh_key:
    description:
      - The name or id of the SSH key to attach to the newly created instance.
  state:
    choices:
      - present
      - absent
    default: present
    description:
      - Should the resource be present or absent.
  subnet:
    description:
      - The subnet name or id or list of subnet names or subnet ids.
        It is required if a `networks` parameter is not provided.
  system_volume_size:
    description:
      - The size of the system volume in GBs.
        It is required if is state is 'present'.
  system_volume_type:
    description:
      - UUID or name of system volume type.
  timeout:
    default: 600
    description:
      - The maximum amount of seconds to wait until the instance provisioning is finished.
  user_data:
    description:
      - An additional data that will be added to the instance on provisioning.
  tags:
    description:
      - List of tags that will be added to the instance on provisioning.
  wait:
    default: true
    description:
      - A boolean value that defines whether client has to wait until the instance
      provisioning is finished.
'''

EXAMPLES = '''
- name: provision a warehouse instance
  hosts: localhost
  tasks:
    - name: add instance
      waldur_marketplace_os_instance:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        data_volume_size: 100
        data_volume_type: lvm
        flavor: m1.micro
        image: Ubuntu 16.04 x86_64
        name: Warehouse instance
        networks:
          - floating_ip: auto
            subnet: vpc-1-tm-sub-net
          - floating_ip: 192.101.13.124
            subnet: vpc-1-tm-sub-net-2
        project: OpenStack Project
        offering: Instance in Tenant
        security_groups:
          - web

- name: Provision instance with user data
  hosts: localhost
  tasks:
    - name: add instance
      waldur_marketplace_os_instance:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        flavor: m1.micro
        floating_ip: auto
        image: CentOS 7 x86_64
        name: Build instance
        project: OpenStack Project
        offering: Instance in Tenant
        ssh_key: ssh1.pub
        subnet: vpc-1-tm-sub-net-2
        system_volume_size: 40
        system_volume_type: lvm
        user_data: |-
            #cloud-config
            chpasswd:
              list: |
                ubuntu:{{ default_password }}
              expire: False

- name: Trigger master instance
  hosts: localhost
  tasks:
    - name: add instance
      waldur_marketplace_os_instance:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        flavor: m1.micro
        floating_ip: auto
        image: CentOS 7 x86_64
        name: Build instance
        project: OpenStack Project
        offering: Instance in Tenant
        ssh_key: ssh1.pub
        subnet: vpc-1-tm-sub-net-2
        system_volume_size: 40
        tags:
            - ansible_application_id
        wait: false

- name: Find flavor by CPU and RAM parameters
  hosts: localhost
  tasks:
    - name: add instance
      waldur_marketplace_os_instance:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        data_volume_size: 100
        flavor_min_cpu: 2
        flavor_min_ram: 1024
        image: Ubuntu 16.04 x86_64
        name: Warehouse instance
        networks:
          - floating_ip: auto
            subnet: vpc-1-tm-sub-net
          - floating_ip: 192.101.13.124
            subnet: vpc-1-tm-sub-net-2
        project: OpenStack Project
        offering: Instance in Tenant
        security_groups:
          - web

- name: create OpenStack instance with predefined floating IP
  hosts: localhost
  tasks:
    - name: create instance
      waldur_marketplace_os_instance:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        project: OpenStack Project
        offering: Instance in Tenant
        name: Warehouse instance
        image: CentOS 7
        flavor: m1.small
        subnet: vpc-1-tm-sub-net-2
        floating_ip: 1.1.1.1
        system_volume_size: 10

- name: delete existing OpenStack compute instance
  hosts: localhost
  tasks:
    - name: delete instance
      waldur_marketplace_os_instance:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        project: OpenStack Project
        name: Warehouse instance
        state: absent

- name: update security groups of instance
  hosts: localhost
  tasks:
    - name: update security groups of mysql server
      waldur_marketplace_os_instance:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        name: mysql-server
        project: OpenStack Project
        state: present
        security_groups:
          - ssh
          - icmp

- name: connect the instance to multiple subnets
  hosts: localhost
  tasks:
    - name: connect to multiple subnets
      waldur_marketplace_os_instance:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        project: OpenStack Project
        name: Warehouse instance
        subnet:
          - vpc-1-tm-sub-net-1
          - vpc-1-tm-sub-net-2
'''


def send_request_to_waldur(client, module):
    name = module.params['name']
    project = module.params['project']
    subnet = module.params.get('subnet')
    present = module.params['state'] == 'present'
    delete_volumes = module.params.get('delete_volumes')
    release_floating_ips = module.params.get('release_floating_ips')

    instance = None
    has_changed = False

    try:
        instance = client.get_instance_via_marketplace(name, project)
        if not present:
            if instance['state'] == 'OK' and instance['runtime_state'] == 'ACTIVE':
                client.stop_instance(
                    instance['uuid'],
                    wait=True,
                    interval=module.params['interval'],
                    timeout=module.params['timeout'],
                )
            client.delete_instance_via_marketplace(
                instance['uuid'],
                delete_volumes=delete_volumes,
                release_floating_ips=release_floating_ips,
            )
            has_changed = True
        else:
            actual_groups = [
                group['name'] for group in instance.get('security_groups') or []
            ]
            requested_groups = module.params.get('security_groups') or []
            if actual_groups != requested_groups:
                client.update_instance_security_groups(
                    instance_uuid=instance['uuid'],
                    settings_uuid=instance['service_settings_uuid'],
                    security_groups=requested_groups,
                    wait=module.params['wait'],
                    interval=module.params['interval'],
                    timeout=module.params['timeout'],
                )
                has_changed = True
            networks = module.params.get('networks')
            # if update is defined using network syntax, extract expected subnets
            if networks:
                subnet = [net['subnet'] for net in networks]
            if subnet:
                if not isinstance(subnet, list):
                    subnet = [subnet]
                instance_subnets = instance.get('internal_ips_set')
                needed_update_subnets = False

                for s in instance_subnets:
                    if not (s['subnet_name'] in subnet or s['subnet_uuid'] in subnet):
                        needed_update_subnets = True
                        break

                if not needed_update_subnets:
                    instance_subnet_names = {s['subnet_name'] for s in instance_subnets}
                    instance_subnets_ids = {s['subnet_uuid'] for s in instance_subnets}
                    for s in subnet:
                        if not (
                            s in instance_subnet_names or s in instance_subnets_ids
                        ):
                            needed_update_subnets = True
                            break

                if needed_update_subnets:
                    client.update_instance_internal_ips_set(
                        instance_uuid=instance['uuid'],
                        subnet_set=subnet,
                        wait=True,
                        interval=module.params['interval'],
                        timeout=module.params['timeout'],
                    )
                    has_changed = True
    except ObjectDoesNotExist:
        if present:
            if isinstance(subnet, list):
                subnet = subnet[0]
            networks = module.params.get('networks') or [
                {'subnet': subnet, 'floating_ip': module.params.get('floating_ip')}
            ]

            instance = client.create_instance_via_marketplace(
                name=module.params['name'],
                description=module.params['description'],
                offering=module.params['offering'],
                project=module.params['project'],
                networks=networks,
                image=module.params['image'],
                system_volume_size=module.params['system_volume_size'],
                security_groups=module.params.get('security_groups'),
                flavor=module.params.get('flavor'),
                flavor_min_cpu=module.params.get('flavor_min_cpu'),
                flavor_min_ram=module.params.get('flavor_min_ram'),
                data_volume_size=module.params.get('data_volume_size'),
                ssh_key=module.params.get('ssh_key'),
                wait=module.params['wait'],
                interval=module.params['interval'],
                timeout=module.params['timeout'],
                user_data=module.params.get('user_data'),
                tags=module.params.get('tags'),
                check_mode=module.check_mode,
                system_volume_type=module.params.get('system_volume_type'),
                data_volume_type=module.params.get('data_volume_type'),
            )
            has_changed = True

    return instance, has_changed


def main():
    module = AnsibleModule(
        argument_spec=waldur_resource_argument_spec(
            data_volume_size=dict(type='int', default=None),
            delete_volumes=dict(type='bool', default=True),
            flavor_min_cpu=dict(type='int', default=None),
            flavor_min_ram=dict(type='int', default=None),
            flavor=dict(type='str', default=None),
            floating_ip=dict(type='str', default=None),
            image=dict(type='str', default=None),
            networks=dict(type='list', default=None),
            project=dict(type='str', default=None),
            offering=dict(type='str', default=None),
            release_floating_ips=dict(type='bool', default=True),
            security_groups=dict(type='list', default=None),
            ssh_key=dict(type='str', default=None),
            subnet=dict(type='list', default=None),
            system_volume_size=dict(type='int', default=None),
            user_data=dict(type='str', default=None),
            system_volume_type=dict(type='str', default=None),
            data_volume_type=dict(type='str', default=None),
        ),
        mutually_exclusive=[
            ['subnet', 'networks'],
            ['floating_ip', 'networks'],
            ['flavor_min_cpu', 'flavor'],
            ['flavor_min_ram', 'flavor'],
        ],
        supports_check_mode=True,
    )

    name = module.params['name']
    state = module.params['state']
    project = module.params['project']
    offering = module.params['offering']
    image = module.params['image']
    flavor = module.params['flavor']
    flavor_min_cpu = module.params['flavor_min_cpu']
    flavor_min_ram = module.params['flavor_min_ram']
    subnet = module.params['subnet']
    networks = module.params['networks']
    system_volume_size = module.params['system_volume_size']

    instance_exists = True
    client = waldur_client_from_module(module)
    try:
        client.get_instance_via_marketplace(name, project)
    except ObjectDoesNotExist:
        instance_exists = False

    if state == 'present' and not instance_exists:
        if not project:
            module.fail_json(
                msg="Parameter 'project' is required if state == 'present'"
            )
        if not offering:
            module.fail_json(
                msg="Parameter 'offering' is required if state == 'present'"
            )
        if not image:
            module.fail_json(msg="Parameter 'image' is required if state == 'present'")
        if not (flavor or (flavor_min_cpu and flavor_min_ram)):
            module.fail_json(
                msg="Parameter 'flavor' or ('flavor_min_cpu' and 'flavor_min_ram')"
                " is required if state == 'present'"
            )
        if not system_volume_size:
            module.fail_json(
                msg="Parameter 'system_volume_size' is required if state == 'present'"
            )
        if not networks and not subnet:
            module.fail_json(
                msg="Parameter 'networks' or 'subnet' is required if state == 'present'"
            )
    try:
        instance, has_changed = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=six.text_type(e))
    else:
        module.exit_json(instance=instance, changed=has_changed)


if __name__ == '__main__':
    main()
