#!/usr/bin/python
# has to be a full import due to Ansible 2.0 compatibility
from ansible.module_utils.basic import *
from waldur_client import WaldurClient, WaldurClientException, ObjectDoesNotExist, MultipleObjectsReturned

DOCUMENTATION = '''
---
module: waldur_os_volume
short_description: Create/Update/Delete OpenStack volume
version_added: 0.8
description:
  - "Create/Update/Delete OpenStack volume"
requirements:
  - "python = 2.7"
  - "requests"
  - "python-waldur-client"
options:
  access_token:
    description:
      - An access token which has permissions to create a volume.
    required: true
  api_url:
    description:
      - Fully qualified URL to the Waldur.
    required: true
  description:
    description:
      - A description of the volume.
    required: false
  interval:
    default: 20
    description:
      - An interval of the volume state polling.
  name:
    description:
      - The name of the volume.
    required: true
  project:
    description:
      - The name or id of the project to add volume to.
    required: true
  provider:
    description:
      - The name or id of the OpenStack provider.
    required: true
  size:
    description:
      - The size of the volume in GBs.
    required: true
  state:
    choices:
      - present
      - absent
    default: present
    description:
      - Should the resource be present or absent.
  tags:
    description:
      - List of tags that will be added to the volume on provisioning.
    required: false
  timeout:
    default: 600
    description:
      - The maximum amount of seconds to wait until the volume provisioning is finished.
  wait:
    default: true
    description:
      - A boolean value that defines whether client has to wait until the volume is provisioned.
'''

EXAMPLES = '''
- name: add volume
  hosts: localhost
  tasks:
    - name: create volume
      waldur_os_volume:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        name: test volume
        project: OpenStack Project
        provider: VPC
        size: 40
        state: present

- name: remove volume
  hosts: localhost
  tasks:
    - name: remove existing volume
      waldur_os_volume:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        name: test volume
        project: OpenStack Project
        state: absent

- name: update volume
  hosts: localhost
  tasks:
    - name: update volume description
      waldur_os_volume:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        name: test volume
        project: OpenStack Project
        description: do not delete this volume
'''


def send_request_to_waldur(client, module):
    has_changed = False
    name = module.params['name']
    project = module.params['project']
    provider = module.params['provider']
    size = module.params['size']
    try:
        volume = client.get_volume(name, project)
    except (ObjectDoesNotExist, MultipleObjectsReturned):
        volume = None
        pass
    present = module.params['state'] == 'present'
    if volume:
        if present:
            if volume['description'] != module.params.get('description'):
                client.update_volume(
                    volume,
                    description=module.params.get('description'))
                has_changed = True
        else:
            client.delete_volume(volume['uuid'])
            has_changed = True
    elif present:
        client.create_volume(
            name=module.params['name'],
            project=project,
            provider=provider,
            size=size,
            description=module.params.get('description'),
            tags=module.params.get('tags'),
            wait=module.params['wait'],
            interval=module.params['interval'],
            timeout=module.params['timeout'])
        has_changed = True

    return has_changed


def main():
    fields = {
        'access_token': {'required': True, 'type': 'str'},
        'api_url': {'required': True, 'type': 'str'},
        'description': {'type': 'str'},
        'interval': {'default': 20, 'type': 'int'},
        'name': {'required': True, 'type': 'str'},
        'project': {'required': True, 'type': 'str'},
        'provider': {'required': True, 'type': 'str'},
        'size': {'required': True, 'type': 'int'},
        'state': {'default': 'present', 'choices': ['absent', 'present']},
        'tags': {'type': 'list'},
        'timeout': {'default': 600, 'type': 'int'},
        'wait': {'default': True, 'type': 'bool'},
    }
    module = AnsibleModule(argument_spec=fields)

    client = WaldurClient(module.params['api_url'], module.params['access_token'])

    try:
        has_changed = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=e.message)
    else:
        module.exit_json(has_changed=has_changed)


if __name__ == '__main__':
    main()
