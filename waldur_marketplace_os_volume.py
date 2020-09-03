#!/usr/bin/python
# has to be a full import due to Ansible 2.0 compatibility
import six
from ansible.module_utils.basic import AnsibleModule

from waldur_client import (
    MultipleObjectsReturned,
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
module: waldur_marketplace_os_volume
short_description: Create/Update/Delete OpenStack volume via marketplace
version_added: 0.8
description:
  - "Create/Update/Delete OpenStack volume"
requirements:
  - "python = 3.6"
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
        It is required if is state is 'present'.
  offering:
    description:
      - The name or id of the marketplace offering.
        It is required if is state is 'present'.
  size:
    description:
      - The size of the volume in GBs.
        It is required if is state is 'present'.
  type:
    description:
      - UUID or name of volume type.
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
      waldur_marketplace_os_volume:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        name: test volume
        project: OpenStack Project
        offering: Volume in Tenant
        size: 40
        type: lvm
        state: present

- name: remove volume
  hosts: localhost
  tasks:
    - name: remove existing volume
      waldur_marketplace_os_volume:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        name: test volume
        project: OpenStack Project
        state: absent

- name: update volume
  hosts: localhost
  tasks:
    - name: update volume description
      waldur_marketplace_os_volume:
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
    offering = module.params['offering']
    size = module.params['size']
    volume_type = module.params['type']

    try:
        volume = client.get_volume_via_marketplace(name, project)
    except (ObjectDoesNotExist, MultipleObjectsReturned):
        volume = None
        pass
    present = module.params['state'] == 'present'
    if volume:
        if present:
            if volume['description'] != module.params.get('description'):
                client.update_volume(
                    volume, description=module.params.get('description')
                )
                has_changed = True
        else:
            client.delete_volume_via_marketplace(volume['uuid'])
            has_changed = True
    elif present:
        client.create_volume_via_marketplace(
            name=module.params['name'],
            project=project,
            offering=offering,
            size=size,
            volume_type=volume_type,
            description=module.params.get('description'),
            tags=module.params.get('tags'),
            wait=module.params['wait'],
            interval=module.params['interval'],
            timeout=module.params['timeout'],
        )
        has_changed = True

    return has_changed


def main():
    fields = waldur_resource_argument_spec(
        project=dict(type='str', default=None),
        offering=dict(type='str', default=None),
        size=dict(type='int', default=None),
        type=dict(type='str', default=None),
    )
    module = AnsibleModule(argument_spec=fields)

    state = module.params['state']
    project = module.params['project']
    offering = module.params['offering']
    size = module.params['size']

    if state == 'present':
        if not project:
            module.fail_json(
                msg="Parameter 'project' is required if state == 'present'"
            )
        if not offering:
            module.fail_json(
                msg="Parameter 'offering' is required if state == 'present'"
            )
        if not size:
            module.fail_json(msg="Parameter 'size' is required if state == 'present'")

    client = waldur_client_from_module(module)

    try:
        has_changed = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=six.text_type(e))
    else:
        module.exit_json(changed=has_changed)


if __name__ == '__main__':
    main()
