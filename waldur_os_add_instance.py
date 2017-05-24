#!/usr/bin/python

DOCUMENTATION = '''
---
module: waldur_os_add_instance
short_description: Create OpenStack instance
'''

EXAMPLES = '''
- hosts: localhost
  tasks:
    - name: Create OpenStack instance
      waldur_os_add_instance:
        name: 'OS Instance'
        service_uuid: 'ca8217c97bf74089b1e1131c69f8ad40'
        project_uuid: '77c23dbcb94a4ad5bba54d82c17fa862'
        subnet: 'vpc-1-tm-sub-net'
        flavor: 'm1.micro'
        image: 'TestVM'
        system_volume_size: 1024
        url: 'http://localhost:8000'
        url_username: 'username'
        url_password: 'userpassword'
      register: result

- hosts: localhost
  tasks:
    - name: Create OpenStack instance with data_volume
      waldur_os_add_instance:
        name: 'OS Instance'
        service_uuid: 'ca8217c97bf74089b1e1131c69f8ad40'
        project_uuid: '77c23dbcb94a4ad5bba54d82c17fa862'
        subnet: 'vpc-1-tm-sub-net'
        flavor: 'm1.micro'
        image: 'TestVM'
        system_volume_size: 1024
        data_volume_size: 1024
        url: 'http://localhost:8000'
        url_username: 'username'
        url_password: 'userpassword'
      register: result

- hosts: localhost
  tasks:
    - name: Create OpenStack instance with user_data
      waldur_os_add_instance:
        name: 'OS Instance'
        service_uuid: 'ca8217c97bf74089b1e1131c69f8ad40'
        project_uuid: '77c23dbcb94a4ad5bba54d82c17fa862'
        subnet: 'vpc-1-tm-sub-net'
        flavor: 'm1.micro'
        image: 'TestVM'
        system_volume_size: 1024
        url: 'http://localhost:8000'
        url_username: 'username'
        url_password: 'userpassword'
        user_data: 'This is my user data'
      register: result
'''

import json

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import fetch_url

HEADERS = {'Content-Type': 'application/json'}


def response_to_json(response):
    return json.loads(response.read())


def get_auth_token(url, module):
    payload = {
        'username': module.params.get('url_username'),
        'password': module.params.get('url_password'),
    }
    url = '%s/%s' % (url, 'api-auth/password/')
    response, info = fetch_url(module, url=url, headers=HEADERS, data=json.dumps(payload), method='POST')
    if info['status'] >= 400:
        module.fail_json(msg=info['body'])
    return response_to_json(response)['token']


def get_service_project_link(host, token, module):
    service = module.params.get('service_uuid')
    project = module.params.get('project_uuid')
    params = 'service_uuid=%s&project_uuid=%s' % (service, project)
    url = '%s/%s?%s' % (host, 'api/openstacktenant-service-project-link/', params)
    response, info = fetch_url(module, url=url, headers=HEADERS, method='GET')
    if info['status'] >= 400:
        module.fail_json(msg=info['body'])
    return response_to_json(response)


def get_flavor(host, token, module):
    url = '%s/%s?name=%s' % (host, 'api/openstacktenant-flavors/', module.params.get('flavor'))
    response, info = fetch_url(module, url=url, headers=HEADERS, method='GET')
    if info['status'] >= 400:
        module.fail_json(msg=info['body'])
    return response_to_json(response)

def get_image(host, token, module):
    url = '%s/%s?name=%s' % (host, 'api/openstacktenant-images/', module.params.get('image'))
    response, info = fetch_url(module, url=url, headers=HEADERS, method='GET')
    if info['status'] >= 400:
        module.fail_json(msg=info['body'])

    return response_to_json(response)

def get_subnet(host, token, module):
    url = '%s/%s?name=%s' % (host, 'api/openstacktenant-subnets/', module.params.get('subnet'))
    response, info = fetch_url(module, url=url, headers=HEADERS, method='GET')
    if info['status'] >= 400:
        module.fail_json(msg=info['body'])

    return response_to_json(response)

def create_instance(host, token, module):
    spls = get_service_project_link(host, token, module)
    if len(spls) > 1:
        module.fail_json(msg='ambigious reference to the service project link')

    flavors = get_flavor(host, token, module)
    if len(flavors) > 1:
        module.fail_json(msg='ambigious reference to the flavor')

    images = get_image(host, token, module)
    if len(images) > 1:
        module.fail_json(msg='ambigious reference to the image')

    subnets = get_subnet(host, token, module)
    if len(subnets) > 1:
        module.fail_json(msg='ambigious reference to the subnet')

    service_project_link = spls[0]
    flavor = flavors[0]
    image = images[0]
    subnet = subnets[0]

    url = '%s/%s' % (host, 'api/openstacktenant-instances/')
    payload = {
        'name': module.params.get('name'),
        'flavor': flavor['url'],
        'image': image['url'],
        'service_project_link' : service_project_link['url'],
        'system_volume_size': module.params.get('system_volume_size'),
        'internal_ips_set':[
            {'subnet': subnet['url']},
        ]
    }

    data_volume_size = module.params.get('data_volume_size')
    if isinstance(data_volume_size, basestring):
        payload.update({'data_volume_size':data_volume_size})
    user_data = module.params.get('user_data')
    if isinstance(user_data, basestring):
        payload.update({'user_data':user_data})

    response, info = fetch_url(module, url=url, headers=HEADERS, data=json.dumps(payload), method='POST')
    if info['status'] >= 400:
        module.fail_json(msg=info['body'])

    has_changed = info['status'] == 201
    return (has_changed, response_to_json(response)['url'])


def main():
    fields = {
        'name': {'required': True, 'type': 'str'},
        'service_uuid': {'required': True, 'type': 'str'},
        'project_uuid': {'required': True, 'type': 'str'},
        'subnet': {'required': True, 'type': 'str'},
        'flavor': {'required': True, 'type': 'str'},
        'image': {'required': True, 'type': 'str'},
        'system_volume_size': {'required': True, 'type': 'int'},
        'data_volume_size': {'required': False, 'type': 'int'},
        'user_data': {'required': False, 'type': 'str'},
        'url': {'required': True, 'type': 'str'},
        'url_username': {'required': True, 'type': 'str'},
        'url_password': {'required': True, 'type': 'str'},
    }
    module = AnsibleModule(argument_spec=fields)
    url = module.params.get('url')
    token = get_auth_token(url, module)
    HEADERS.update({'Authorization': 'token %s' % token})
    has_changed, meta = create_instance(url, token, module)
    module.exit_json(changed=has_changed, meta=meta)


if __name__ == '__main__':
    main()
