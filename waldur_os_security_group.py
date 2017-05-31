#!/usr/bin/python
from ansible.module_utils.basic import AnsibleModule
from waldur_client import WaldurClient, WaldurClientException

DOCUMENTATION = '''
--- 
module: waldur_os_security_group
short_description: "Add/Update/Remove OpenStack cloud security group"
version_added: "0.1"
description: 
  - "Add/Update/Remove OpenStack cloud security group"
requirements:
  - "python = 2.7"
  - "requests"
options: 
  access_token: 
    description: 
      - An access token which has permissions to create an OpenStack instances.
    required: true
  api_url: 
    description: 
      - Fully qualified url to the Waldur.
    required: true
  cidr: 
    description: 
      - A cidr the security group rule is applied to.
    required: if 'rules' are not provided.
  cloud: 
    description: 
      - The name of the tenant to create a security group for.
    required: false
  description: 
    description: 
      - A description of the security group.
    required: true
  from_port: 
    description: 
      - The lowest port value the security group rule is applied to.
    required: if 'rules' are not provided.
  protocol: 
    description: 
      - A protocol the security group rule is applied to.
    name: 
      description: 
        - The name of the security group.
      required: true
    required: if 'rules' are not provided.
  rules: 
    cidr: 
      description: 
        - A cidr the security group rule is applied to.
      required: true
    description: 
      - A list of security group rules to be applied to the security group.
    from_port: 
      description: 
        - The lowest port value the security group rule is applied to.
      required: true
    protocol: 
      description: 
        - A protocol the security group rule is applied to.
      required: true
    required: Only if 'from_ip', 'to_ip', 'cidr', 'protocol' are not provided
    to_port: 
      description: 
        - The highest port value the security group rule is applied to.
      required: true
  state: 
    choices: 
      - present
      - absent
    default: present
    description: 
      - Should the resource be present or absent.
  to_port: 
    description: 
      - The highest port value the security group rule is applied to.
    required: if 'rules' are not provided.
'''

EXAMPLES = '''
  Create security group

  waldur_os_security_group: 
    access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
    api_url: https://waldur.example.com:8000
    cloud: VPC #1
    description: first and last TCP quartiles
    rules: 
      - 
        from_port: 20
        to_port: 60
        cidr: 192.168.63.24/24
        protocol: tcp
      - 
        from_port: 70
        to_port: 80
        cidr: 192.168.63.24/24
        protocol: tcp
    state: present
    name: classic


  Remove previous security group

    access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
    api_url: https://waldur.example.com:8000
    cloud: VPC #1
    rules: 
      - 
        from_port: 20
        to_port: 60
        cidr: 192.168.63.24/24
        protocol: tcp
      - 
        from_port: 70
        to_port: 80
        cidr: 192.168.63.24/24
        protocol: tcp
    state: absent
    name: classic


 Create security group with 1 security rule

  waldur_os_security_group: 
    access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
    api_url: https://waldur.example.com:8000
    cloud: VPC #1
    description: first and last TCP quartiles
    from_port: 70
    to_port: 80
    cidr: 192.168.63.24/24
    protocol: tcp
    state: present
    name: classic
'''


def send_request_to_waldur(client, module):
    has_changed = False
    name = module.params['name']
    security_group = client.get_security_group(cloud=module.params['cloud'], name=name)
    present = module.params['state'] == 'present'
    if security_group:
        if present:
            if security_group['description'] != module.params.get('description'):
                client.update_security_group_description(
                    security_group,
                    description=module.params.get('description'))
                has_changed = True
        else:
            client.delete_security_group(security_group['uuid'])
            has_changed = True
    elif present:
        rules = module.params.get('rules', [{
            'from_port': module.params['from_port'],
            'to_port': module.params['to_port'],
            'cidr': module.params['cidr'],
            'protocol': module.params['protocol'],
        }])
        client.create_security_group(
            cloud=module.params['cloud'],
            name=module.params['name'],
            description=module.params.get('description'),
            rules=rules)
        has_changed = True

    return has_changed


def main():
    fields = {
        'api_url': {'required': True, 'type': 'str'},
        'access_token': {'required': True, 'type': 'str'},
        'description': {'type': 'str'},
        'rules': {'type': 'list'},
        'from_port': {'type': 'str'},
        'to_port': {'type': 'str'},
        'cidr': {'type': 'str'},
        'protocol': {'type': 'str', 'choices': ['tcp', 'udp', 'icmp']},
        'state': {'default': 'present', 'choices': ['absent', 'present']},
        'name': {'required': True, 'type': 'str'},
        'cloud': {'required': True, 'type': 'str'},
    }
    required_together = [['from_port', 'to_port', 'cidr', 'protocol']]
    mutually_exclusive = [['from_port', 'rules'],
                          ['to_port', 'rules'],
                          ['cidr', 'rules'],
                          ['protocol', 'rules']]
    required_one_of = mutually_exclusive
    module = AnsibleModule(
        argument_spec=fields,
        required_together=required_together,
        required_one_of=required_one_of,
        mutually_exclusive=mutually_exclusive)

    client = WaldurClient(module.params['api_url'], module.params['access_token'])

    try:
        has_changed = send_request_to_waldur(client, module)
    except WaldurClientException as e:
        module.fail_json(msg=e.message)

    module.exit_json(has_changed=has_changed)


if __name__ == '__main__':
    main()
