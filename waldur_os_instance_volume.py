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
module: waldur_os_instance_volume
short_description: Attach and detach Volumes from OpenStack VMs
version_added: 0.8
description:
  - Attach and detach Volumes from OpenStack VMs.
requirements:
  - "python = 3.6"
  - "requests"
  - "python-waldur-client"
options:
  access_token:
    description:
      - An access token which has permissions to manage a volume.
    required: true
  api_url:
    description:
      - Fully qualified URL to the Waldur.
    required: true
  device:
    description:
      - Name of volume as instance device e.g. /dev/vdb.
    required: true
  instance:
    description:
      - Name or ID of virtual machine you want to attach a volume to.
    required: true
  interval:
    default: 20
    description:
      - An interval of the volume state polling.
  project:
    description:
      - The name or id of the Waldur project which has volume and instance.
        It should be specified only if volume name or instance name is provided.
  state:
    choices:
      - present
      - absent
    default: present
    description:
      - Should the resource be present or absent.
  timeout:
    default: 600
    description:
      - The maximum amount of seconds to wait until the volume provisioning is finished.
  volume:
    description:
      - Name or id of volume you want to attach to the virtual machine.
    required: true
  wait:
    default: true
    description:
      - A boolean value that defines whether client has to wait until the operation is complete.
'''

EXAMPLES = '''
- name: attach volume to the instance
  hosts: localhost
  tasks:
    - name: attach volume
      waldur_os_instance_volume:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        project: database management
        volume: postgresql-data
        instance: postgresql-server
        device: /dev/vdb

- name: detach volume from the instance
  hosts: localhost
  tasks:
    - name: detach volume
      waldur_os_instance_volume:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        project: database management
        volume: postgresql-data
        state: absent
'''


def send_request_to_waldur(client, module):
    device = module.params['device']
    instance = module.params['instance']
    project = module.params['project']
    state = module.params['state']
    volume = module.params['volume']
    wait = module.params['wait']
    interval = module.params['interval']
    timeout = module.params['timeout']
    params = dict(wait=wait, interval=interval, timeout=timeout,)

    # Get volume by ID or name and project
    volume = client.get_volume(volume, project)
    runtime_state = volume['runtime_state']

    if state == 'absent':
        if runtime_state == 'available':
            # Volume is already detached so there's nothing to do
            return False
        elif runtime_state == 'in-use':
            # Volume should be detached
            client.detach_volume(volume['uuid'], **params)
            return True
    elif state == 'present':
        # Get instance by ID or name and project
        instance = client.get_instance(instance, project)
        if runtime_state == 'in-use':
            # Volume is already attached to target instance so there's nothing to do
            if volume['instance'] == instance['url']:
                return False
            else:
                # Volume is attached to another instance, so we should detach and attach
                client.detach_volume(volume['uuid'])
                client.attach_volume(volume['uuid'], instance['uuid'], device, **params)
                return True
        elif runtime_state == 'available':
            # Volume should be attached to the instance
            client.attach_volume(volume['uuid'], instance['uuid'], device, **params)
            return True


def main():
    fields = waldur_full_argument_spec(
        device=dict(type='int', default=None),
        instance=dict(type='str', default=None),
        project=dict(type='str', default=None),
        state=dict(default='present', choices=['absent', 'present']),
        volume=dict(type='str', required=True),
    )
    module = AnsibleModule(argument_spec=fields)

    state = module.params['state']
    instance = module.params['instance']
    device = module.params['instance']

    if state == 'present':
        if not instance:
            module.fail_json(
                msg="Parameter 'instance' is required if state == 'present'"
            )

        if not device:
            module.fail_json(msg="Parameter 'device' is required if state == 'present'")

    client = waldur_client_from_module(module)

    try:
        has_changed = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=six.text_type(e))
    else:
        module.exit_json(changed=has_changed)


if __name__ == '__main__':
    main()
