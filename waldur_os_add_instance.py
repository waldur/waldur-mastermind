#!/usr/bin/python

DOCUMENTATION = '''
    ---
    module: waldur_os_add_instance
    short_description: Create OpenStack instance
    '''

EXAMPLES = '''
# TODO [TM:5/26/17] update examples
- hosts: localhost
  tasks:
    - name: Test that my module works
      waldur_os_add_instance: 
        name: test instance 2
        provider: VPC #1 [TM]
        project: OpenStack Project
        networks:
          -
            subnet: vpc-1-tm-sub-net
            floating_ip: auto
        flavor: m1.micro
        image: TestVM
        system_volume_size: 1
        ssh_key: macOs.pub
        waldur_url: http://localhost:8000
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
      register: result

    - debug: var=result
    '''

# extract client to a separate file
from collections import namedtuple
from uuid import UUID
import json
import requests
import time


class WaldurClientException(Exception):
    pass


class WaldurClient(object):
    resource_stable_states = ['OK', 'ERRED']

    class Endpoints(object):
        Provider = 'openstacktenant'
        Project = 'projects'
        Flavor = 'openstacktenant-flavors'
        Subnet = 'openstacktenant-subnets'
        FloatingIP = 'openstacktenant-floating-ips'
        Image = 'openstacktenant-images'
        ServiceProjectLink = 'openstacktenant-service-project-link'
        Instance = 'openstacktenant-instances'
        SshKey = 'keys'

    def __init__(self, host, token):
        self.host = host
        self.headers = {
            'Authorization': 'token %s' % token,
            'Content-Type': 'application/json',
        }

    def _build_url(self, endpoint):
        return requests.compat.urljoin(self.host, 'api/%s/' % endpoint)

    def _raise_request_failed(self, response):
        try:
            reason = response.json()
        except ValueError as error:
            reason = error.message
        details = 'Status: %s. Reason: %s.' % (response.status_code, reason)
        raise WaldurClientException('Server refuses to communicate. %s' % details)

    def _query_resource(self, endpoint, query_params):
        url = self._build_url(endpoint)
        try:
            response = requests.get(url, params=query_params, headers=self.headers)
        except requests.exceptions.RequestException as error:
            raise WaldurClientException(error.message)

        if response.status_code >= 400:
            self._raise_request_failed(response)

        result = response.json()
        if len(result) > 1:
            message = 'Ambiguous reference to resource. Query: %s' % query_params
            raise WaldurClientException(message)

        return result[0] if result else None

    def _get_resource(self, endpoint, value):
        try:
            uuid = UUID(value)
        except ValueError:
            resource = self._query_resource_by_name(endpoint, value)
        else:
            resource = self._query_resource_by_uuid(endpoint, uuid)

        return resource

    def _query_resource_by_uuid(self, endpoint, value):
        return self._query_resource(endpoint, {'uuid': value})

    def _query_resource_by_name(self, endpoint, value):
        return self._query_resource(endpoint, {'name': value})

    def _get_service_project_link(self, provider_uuid, project_uuid):
        query = {'project_uuid': project_uuid, 'service_uuid': provider_uuid}
        return self._query_resource(self.Endpoints.ServiceProjectLink, query)

    def _create_resource(self, endpoint, payload=None):
        url = self._build_url(endpoint)
        try:
            response = requests.post(url, data=json.dumps(payload), headers=self.headers)
        except requests.exceptions.RequestException as error:
            raise WaldurClientException(error.message)

        if response.status_code != 201:
            self._raise_request_failed(response)

        return response.json()

    def _allocate_floating_ip(self, provider_uuid):
        endpoint = requests.compat.urljoin(self.Endpoints.Provider, provider_uuid)
        return _create_resource(endpoint)

    def _networks_to_payload(self, networks, provider_uuid):
        subnets = []
        floating_ips = []

        for item in networks:
            subnet_resource = self._get_resource(self.Endpoints.Subnet, item['subnet'])
            subnet = {'subnet': subnet_resource['url']}
            subnets.append(subnet)
            ip_name = item.get('floating_ip')
            if ip_name:
                ip = subnet.copy()
                if ip_name != 'auto':
                    floating_ip_resource = self._get_resource(self.Endpoints.FloatingIP, ip_name)
                    ip.update({'url': floating_ip_resource['url']})
                floating_ips.append(ip)

        return subnets, floating_ips

    def create_instance(
            self,
            name,
            provider,
            project,
            networks,
            flavor,
            image,
            system_volume_size,
            interval=10,
            timeout=60,
            wait=None,
            ssh_key=None,
            data_volume_size=None,
            user_data=None):
        '''
        TODO: add documentation.
        '''
        provider = self._get_resource(self.Endpoints.Provider, provider)
        project = self._get_resource(self.Endpoints.Project, project)
        service_project_link = self._get_service_project_link(
            provider_uuid=provider['uuid'],
            project_uuid=project['uuid'])
        flavor = self._get_resource(self.Endpoints.Flavor, flavor)
        image = self._get_resource(self.Endpoints.Image, image)
        subnets, floating_ips = self._networks_to_payload(networks, provider['uuid'])

        payload = {
            'name': name,
            'flavor': flavor['url'],
            'image': image['url'],
            'service_project_link': service_project_link['url'],
            'system_volume_size': system_volume_size * 1024,
            'internal_ips_set': subnets,
            'floating_ips': floating_ips,
        }

        # TODO: security groups.
        if data_volume_size:
            payload.update({'data_volume_size': data_volume_size * 1024})
        if user_data:
            payload.update({'user_data': user_data})
        if ssh_key:
            ssh_key = self._get_resource(self.Endpoints.SshKey, ssh_key)
            payload.update({'ssh_public_key': ssh_key['url']})

        instance = self._create_resource(self.Endpoints.Instance, payload)

        if wait:
            ready = self._is_resource_ready(self.Endpoints.Instance, instance['uuid'])
            waited = 0
            while not ready:
                time.sleep(interval)
                ready = self._is_resource_ready(self.Endpoints.Instance, instance['uuid'])
                waited += interval
                if waited >= interval:
                    error = 'Instance "%s" has not changed state to stable' % instance['url']
                    message = '%s. Seconds passed: %s' % (error, timeout)
                    raise WaldurClientException(message)

        return instance

    def _is_resource_ready(self, endpoint, uuid):
        resource = self._query_resource_by_uuid(endpoint, uuid)
        return resource in self.resource_stable_states
# end client


def main():
    fields = {
        'waldur_url': {'required': True, 'type': 'str'},
        'access_token': {'required': True, 'type': 'str'},
        'name': {'required': True, 'type': 'str'},
        'provider': {'required': True, 'type': 'str'},
        'project': {'required': True, 'type': 'str'},
        'networks': {'required': False, 'type': 'list'},
        'subnet': {'required': False, 'type': 'str'},
        'floating_ip': {'required': False, 'type': 'str'},
        'flavor': {'required': True, 'type': 'str'},
        'image': {'required': True, 'type': 'str'},
        'system_volume_size': {'required': True, 'type': 'int'},
        'data_volume_size': {'type': 'int'},
        'ssh_key': {'type': 'str'},
        'user_data': {'type': 'str'},
        'wait': {'default': True, 'type': 'bool'},
        'timeout': {'default': 60, 'type': 'int'},
        'interval': {'default': 10, 'type': 'int'}
    }
    required_together = [['wait','timeout'], ['subnet', 'floating_ip']]
    mutually_exclusive = [['subnet', 'networks'], ['floating_ip', 'networks']]
    required_one_of = [['subnet', 'networks'], ['floating_ip','networks']]
    module = AnsibleModule(
        argument_spec=fields,
        required_together=required_together,
        required_one_of=required_one_of,
        mutually_exclusive=mutually_exclusive)

    client = WaldurClient(module.params['waldur_url'], module.params['access_token'])
    networks = module.params.get('networks', {
        'subnet': module.params['subnet'],
        'floating_ip': module.params['floating_ip']
        })
    try:
        instance = client.create_instance(
            name=module.params['name'],
            provider=module.params['provider'],
            project=module.params['project'],
            networks=networks,
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

    module.exit_json(meta=instance)


from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import fetch_url
from ansible.module_utils.waldur_client import WaldurClient, WaldurClientException
if __name__ == '__main__':
    main()
