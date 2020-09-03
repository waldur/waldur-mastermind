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
module: waldur_os_snapshot
short_description: Create/Delete OpenStack snapshot
version_added: 0.8
description:
  - "Create/Delete OpenStack snapshot"
requirements:
  - "python = 3.6"
  - "requests"
  - "python-waldur-client"
options:
  access_token:
    description:
      - An access token which has permissions to create a snapshot.
    required: true
  api_url:
    description:
      - Fully qualified URL to the Waldur.
    required: true
  description:
    description:
      - A description of the snapshot.
  interval:
    default: 20
    description:
      - An interval of the snapshot state polling.
  kept_until:
    description:
      - Guaranteed time of snapshot retention. If null - keep forever.
  name:
    description:
      - The name of the snapshot.
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
      - List of tags that will be added to the snapshot on provisioning.
  timeout:
    default: 600
    description:
      - The maximum amount of seconds to wait until the snapshot provisioning is finished.
  volume:
    description:
      - The name or id of the OpenStack volume.
        It is required if is state is 'present'.
  wait:
    default: true
    description:
      - A boolean value that defines whether client has to wait until the snapshot is provisioned.
'''

EXAMPLES = '''
- name: create snapshot
  hosts: localhost
  tasks:
    - name: create snapshot
      waldur_os_snapshot:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        volume: test volume
        name: test snapshot
        state: present
        kept_until: 2018-12-31

- name: remove snapshot
  hosts: localhost
  tasks:
    - name: remove existing snapshot
      waldur_os_snapshot:
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        api_url: https://waldur.example.com:8000/api
        name: test snapshot
        volume: test volume
        state: absent
'''


def send_request_to_waldur(client, module):
    has_changed = False
    name = module.params['name']
    try:
        snapshot = client.get_snapshot(name)
    except (ObjectDoesNotExist, MultipleObjectsReturned):
        snapshot = None
        pass
    present = module.params['state'] == 'present'
    if snapshot and not present:
        client.delete_snapshot(snapshot['uuid'])
        has_changed = True
    elif present:
        client.create_snapshot(
            name=module.params['name'],
            description=module.params.get('description'),
            interval=module.params['interval'],
            kept_until=module.params.get('kept_until'),
            tags=module.params.get('tags'),
            timeout=module.params['timeout'],
            volume=module.params['volume'],
            wait=module.params['wait'],
        )
        has_changed = True

    return has_changed


def main():
    fields = waldur_resource_argument_spec(
        kept_until=dict(type='str', default=None),
        volume=dict(type='str', default=None),
    )
    module = AnsibleModule(argument_spec=fields)

    state = module.params['state']
    volume = module.params['volume']

    if state == 'present':
        if not volume:
            module.fail_json(msg="Parameter 'volume' is required if state == 'present'")

    client = waldur_client_from_module(module)

    try:
        has_changed = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=six.text_type(e))
    else:
        module.exit_json(changed=has_changed)


if __name__ == '__main__':
    main()
