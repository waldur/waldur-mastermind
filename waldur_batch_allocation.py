#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule, text_type

from waldur_client import WaldurClientException, waldur_client_from_module

ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'OpenNode',
}

DOCUMENTATION = '''
---
module: waldur_batch_allocation
short_description: Creates an order for batch allocation
options:
  access_token:
    description:
      - An access token which has permissions to manage an allocation.
    required: true
  api_url:
    description:
      - Fully qualified URL to the api endpoint.
    required: true
  project:
    description:
      - The name or UUID of the project to add an allocation to.
    required: true
  offering:
    description:
      - The name or UUID of the marketplace offering.
    required: true
  plan:
    description:
      - The name or UUID of the marketplace plan.
    required: true
  cpu_hours:
    description:
      - The requested number of CPU hours
    required: true
  gpu_hours:
    description:
      - The requested number of GPU hours
    required: true
  ram_gb:
    description:
      - The requested number of RAM capacity (GB)
    required: true
  name:
    description:
      - The name of allocation
    required: true
  description:
    description:
      - The description of allocation
    required: true
'''

EXAMPLES = '''
---
- hosts: localhost
  gather_facts: no
  tasks:
  - name: Create sample batch allocation
    waldur_batch_allocation:
      access_token: 5046870cf37bfe3f347d5cbcebed48f752912c9a
      api_url: https://waldur.example.com:8000/api
      project: Project
      offering: Offering name
      plan: Plan name
      cpu_hours: 1
      gpu_hours: 1
      ram_gb: 1
      name: Sample name
      description: Sample description
'''


def format_params(params):
    project = params['project']
    offering = params['offering']

    # 'module.params' contains each fields key
    # regardless if it is mentioned in a playbook or not

    plan = params['plan']

    limits = {'cpu': params['cpu_hours'], 'gpu': params['gpu_hours'],
              'ram': params['ram_gb']}

    attributes = {'name': params['name'], 'description': params['description']}

    return project, offering, plan, attributes, limits


def send_request_to_waldur(client, module):
    project, offering, plan, attributes, limits = format_params(module.params)

    response = client.create_marketplace_order(
        project, offering, plan, attributes, limits
    )
    order_item = response['items'][0]
    return order_item, True


def main():
    fields = {
        'api_url': {'required': True, 'type': 'str'},
        'access_token': {'required': True, 'type': 'str'},
        'project': {'required': True, 'type': 'str'},
        'offering': {'required': True, 'type': 'str'},
        'plan': {'required': True, 'type': 'str'},
        'cpu_hours': {'required': True, 'type': 'int'},
        'gpu_hours': {'required': True, 'type': 'int'},
        'ram_gb': {'required': True, 'type': 'int'},
        'name': {'required': True, 'type': 'str'},
        'description': {'required': True, 'type': 'str'},
    }
    module = AnsibleModule(argument_spec=fields)

    client = waldur_client_from_module(module)

    try:
        order_item, has_changed = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=text_type(e))
    else:
        module.exit_json(order=order_item, changed=has_changed)


if __name__ == '__main__':
    main()
