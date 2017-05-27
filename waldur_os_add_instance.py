#!/usr/bin/python
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import fetch_url

DOCUMENTATION = '''
    ---
    module: waldur_os_add_instance
    short_description: Create OpenStack instance
    version_added: "0.1"
    description:
        - Create OpenStack instance
    options:
        waldur_url:
            description:
                - fully qualified url to the Waldur.
            required: True
        access_token:
            description:
                - access_token which has permissions to create OpenStack instances.
            required: True
        name:
            description:
                - name of the new OpenStack instance.
            required: True
        provider:
            description:
                - name or uuid of the instance provider.
            required: True
        project:
            description:
                - name or uuid of the project to add instance to.
            required: True
        flavor:
            description:
                - name or uuid of the flavor to use.
            required: True
        image:
            description:
                - name or uuid of the image to use.
            required: True
        system_volume_size:
            description:
                - size of the system volume in GBs.
            required: True
        security_groups:
            description:
                - list of uuids or names of security groups to apply to newly created instance.
            required: False
            default: default
        networks:
            description:
                - list of networks an instance has to be attached to.
                    consists of 2 parameters:
                        subnet:
                            description:
                                - uuid or name of the subnet to use.
                            required: True
                        floating_ip:
                            description:
                                uuid or address of the existing floating ip to use.
                                Not assigned if not specified.
                                Use `auto` to allocate new floating ip or reuse available one.
                            required: True
            required: only if `subnet` and `floating_ip` are not provided
        subnet:
            description:
                - uuid or name of the subnet to use.
            required:
                - If a `networks` parameter is not provided.
        floating_ip:
            description:
                - uuid or address of the existing floating ip to use.
                  Not assigned if not specified.
                  Use `auto` to allocate new floating ip or reuse available one.
            required:
                - If a `networks` parameter is not provided.
        data_volume_size:
            description:
                - size of the data volume in GB.
            required: False
            default: volume is not created.
        ssh_key:
            description:
                - name or uuid of the ssh key to attach to newly created instance.
            required: False
        user_data:
            description:
                - Additional data that will be added to instance on provisioning.
            required: False
        wait:
            description:
                - Boolean value that defines whether client has to wait until instance provisioning
                 is finished.
            required: False
            default: True
        timeout:
            description:
                - A maximum amount of seconds to wait until instance provisioning is finished.
            required: False
            default: 60 * 10
        interval:
            description:
                - An interval of instance state polling.
            required: False
            default: 20
    requirements:
        - "python = 2.7"
        - "requests"
    '''

EXAMPLES = '''
- hosts: localhost
  tasks:
    - name: Provision warehouse instance
      waldur_os_add_instance: 
        name: Warehouse instance #1
        provider: VPC #1
        project: OpenStack Project
        networks:
          -
            subnet: vpc-1-tm-sub-net
            floating_ip: auto
          -
            subnet: vpc-1-tm-sub-net-2
            floating_ip: 192.101.13.124
        security_groups:
            - web
        flavor: m1.micro
        image: Ubuntu14.04
        system_volume_size: 10
        data_volume_size: 100
        ssh_key: ssh1.pub
        waldur_url: https://waldur.com:8000
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        
    - name: Provision build instance
      waldur_os_add_instance: 
        name: Build instance #2
        provider: VPC #1
        project: OpenStack Project
        subnet: vpc-1-tm-sub-net-2
        floating_ip: auto
        flavor: m1.micro
        image: CentOs7
        system_volume_size: 40
        ssh_key: ssh1.pub
        waldur_url: https://waldur.com:8000
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        user_data: This instance does not need a data volume
        
    - name: Trigger master instance
      waldur_os_add_instance: 
        name: Build instance #3
        provider: VPC #1
        project: OpenStack Project
        subnet: vpc-1-tm-sub-net-2
        floating_ip: auto
        flavor: m1.micro
        image: CentOs7
        system_volume_size: 40
        ssh_key: ssh1.pub
        waldur_url: https://waldur.com:8000
        access_token: b83557fd8e2066e98f27dee8f3b3433cdc4183ce
        wait: False
        user_data: No need to wait until provisioning is done.
    '''

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
        SecurityGroup = 'openstacktenant-security-groups'
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

    def _get_resource(self, endpoint, value, query_params=None):
        """
        Returns resource received from sending a GET request to provided resource endpoint.
        Raises WaldurClientException 
            if response could not be received
            or it's status is bigger than 400.
        :param endpoint: WaldurClient.Endpoint attribute.
        :param value: name or uuid of the resource
        :param query_params: parameters to use to search for resource instead of value.
        :return: a resource as a dictionary.
        """
        try:
            uuid = UUID(value)
        except ValueError:
            if query_params:
                resource = self._query_resource(endpoint, query_params)
            else:
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

    def _networks_to_payload(self, networks):
        """
        Parse networks into waldur accepted format. Input should be 
        :param networks: networks info in the following format:
            {
                subnet: name or uuid
                floating_ip: auto or address or empty
            }
        :return: a tuple, 
                where first argument is subnets 
                and second is floating_ips 
        """
        subnets = []
        floating_ips = []

        for item in networks:
            if 'subnet' not in item:
                raise WaldurClientException('Wrong networks format. subnet key is required.')
            subnet_resource = self._get_resource(self.Endpoints.Subnet, item['subnet'])
            subnet = {'subnet': subnet_resource['url']}
            subnets.append(subnet)
            address = item.get('floating_ip')
            if address:
                ip = subnet.copy()
                if address != 'auto':
                    query = {'address': address}
                    floating_ip_resource = self._get_resource(self.Endpoints.FloatingIP, query)
                    ip.update({'url': floating_ip_resource['url']})
                floating_ips.append(ip)

        return subnets, floating_ips

    def _is_resource_ready(self, endpoint, uuid):
        resource = self._query_resource_by_uuid(endpoint, uuid)
        return resource in self.resource_stable_states

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
            security_groups=None,
            user_data=None):
        """
        Creates OpenStack instance from passed parameters.
        :param name: name of the instance
        :param provider: uuid or name of the provider to use 
        :param project: uuid or name of the project to add the instance
        :param networks: a list of networks to add instance to 
        :param flavor: uuid or name of the flavor to use
        :param image: uuid or name of the image to use
        :param system_volume_size: size of the system volume in GB
        :param interval: interval of instance state polling in seconds
        :param timeout: a maximum amount of time to wait for instance provisioning
        :param wait: defines whether the client has to wait for instance provisioning
        :param ssh_key: uuid or name of the ssh key to add to the instance
        :param data_volume_size: size of the data volume in GB. 
            No data volume is going to be created if empty.
        :param security_groups: list of security groups to add to the instance
        :param user_data: additional data that will be added to the instance
        :return: an instance as a dictionary.
        """
        provider = self._get_resource(self.Endpoints.Provider, provider)
        project = self._get_resource(self.Endpoints.Project, project)
        service_project_link = self._get_service_project_link(
            provider_uuid=provider['uuid'],
            project_uuid=project['uuid'])
        flavor = self._get_resource(self.Endpoints.Flavor, flavor)
        image = self._get_resource(self.Endpoints.Image, image)
        subnets, floating_ips = self._networks_to_payload(networks)

        payload = {
            'name': name,
            'flavor': flavor['url'],
            'image': image['url'],
            'service_project_link': service_project_link['url'],
            'system_volume_size': system_volume_size * 1024,
            'internal_ips_set': subnets,
            'floating_ips': floating_ips,
        }

        if security_groups:
            payload['security_groups'] = []
            for group in security_groups:
                security_group = self._get_resource(self.Endpoints.SecurityGroup, group)
                payload['security_groups'].append({'url': security_group['url']})

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


def main():
    fields = {
        'waldur_url': {'required': True, 'type': 'str'},
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
