import json
import time
from uuid import UUID
import requests


def is_uuid(value):
    try:
        UUID(value)
        return True
    except ValueError:
        return False


class WaldurClientException(Exception):
    pass


class ObjectDoesNotExist(WaldurClientException):
    """The requested object does not exist"""
    pass


class MultipleObjectsReturned(WaldurClientException):
    """The query returned multiple objects when only one was expected."""
    pass


class ValidationError(WaldurClientException):
    """An error while validating data."""
    pass


class TimeoutError(WaldurClientException):
    """Thrown when a command does not complete in enough time."""
    pass


class WaldurClient(object):
    resource_stable_states = ['OK', 'Erred']

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
        Tenant = 'openstack-tenants'
        TenantSecurityGroup = 'openstack-security-groups'

    def __init__(self, api_url, access_token):
        """
        Initializes a Waldur client
        :param api_url: a fully qualified URL to the Waldur API. Example: https://waldur.example.com:8000/api
        :param access_token: an access token to be used to communicate with the API.
        """

        self.api_url = self._ensure_trailing_slash(api_url)
        self.headers = {
            'Authorization': 'token %s' % access_token,
            'Content-Type': 'application/json',
        }

    def _ensure_trailing_slash(self, url):
        return url if url[-1] == '/' else '%s/' % url

    def _build_url(self, endpoint):
        return requests.compat.urljoin(self.api_url, self._ensure_trailing_slash(endpoint))

    def _build_resource_url(self, endpoint, uuid, action=None):
        resource_url = self._build_url('/'.join([endpoint, uuid]))
        return ''.join([resource_url, action]) if action else resource_url

    def _parse_error(self, response):
        try:
            reason = response.json()
        except ValueError as error:
            reason = error.message
        details = 'Status: %s. Reason: %s.' % (response.status_code, reason)
        return 'Server refuses to communicate. %s' % details

    def _query_resource(self, endpoint, query_params):
        url = self._build_url(endpoint)
        if 'uuid' in query_params:
            url += query_params.pop('uuid') + '/'
        try:
            response = requests.get(url, params=query_params, headers=self.headers)
        except requests.exceptions.RequestException as error:
            raise WaldurClientException(error.message)

        if response.status_code >= 400:
            error = self._parse_error(response)
            raise WaldurClientException(error)

        result = response.json()
        if not result:
            message = 'Result is empty. Endpoint: %s. Query: %s' % (endpoint, query_params)
            raise ObjectDoesNotExist(message)

        if isinstance(result, dict):
            return result

        if len(result) > 1:
            message = 'Ambiguous result. Endpoint: %s. Query: %s' % (url, query_params)
            raise MultipleObjectsReturned(message)

        return result[0]

    def _query_resource_by_uuid(self, endpoint, value):
        return self._query_resource(endpoint, {'uuid': value})

    def _query_resource_by_name(self, endpoint, value):
        return self._query_resource(endpoint, {'name': value})

    def _get_resource(self, endpoint, value):
        """
        Get resource by UUID, name or query parameters.

        :param endpoint: WaldurClient.Endpoint attribute.
        :param value: name or uuid of the resource
        :return: a resource as a dictionary.
        :raises: WaldurClientException if resource could not be received or response failed.
        """
        if is_uuid(value):
            return self._query_resource_by_uuid(endpoint, value)
        else:
            return self._query_resource_by_name(endpoint, value)

    def _create_resource(self, endpoint, payload=None, valid_state=201):
        url = self._build_url(endpoint)
        try:
            response = requests.post(url, json=payload, headers=self.headers)
        except requests.exceptions.RequestException as error:
            raise WaldurClientException(error.message)

        if response.status_code != valid_state:
            error = self._parse_error(response)
            raise WaldurClientException(error)

        return response.json()

    def _update_resource(self, endpoint, uuid, payload):
        url = self._build_resource_url(endpoint, uuid)
        try:
            response = requests.put(url, data=json.dumps(payload), headers=self.headers)
        except requests.exceptions.RequestException as error:
            raise WaldurClientException(error.message)

        if response.status_code != 200:
            error = self._parse_error(response)
            raise WaldurClientException(error)

        return response.json()

    def _delete_resource(self, endpoint, uuid):
        url = self._build_resource_url(endpoint, uuid)
        try:
            response = requests.delete(url, headers=self.headers)
        except requests.exceptions.RequestException as error:
            raise WaldurClientException(error.message)

        if response.status_code not in (204, 202):
            error = self._parse_error(response)
            raise WaldurClientException(error)

        return response.json()

    def _get_service_project_link(self, provider_uuid, project_uuid):
        query = {'project_uuid': project_uuid, 'service_uuid': provider_uuid}
        return self._query_resource(self.Endpoints.ServiceProjectLink, query)

    def _get_provider(self, identifier):
        return self._get_resource(self.Endpoints.Provider, identifier)

    def _get_project(self, identifier):
        return self._get_resource(self.Endpoints.Project, identifier)

    def _get_property(self, endpoint, identifier, settings_uuid):
        query = {'settings_uuid': settings_uuid}
        if is_uuid(identifier):
            query['uuid'] = identifier
        else:
            query['name_exact'] = identifier
        return self._query_resource(endpoint, query)

    def _get_flavor(self, identifier, settings_uuid):
        return self._get_property(self.Endpoints.Flavor, identifier, settings_uuid)

    def _get_image(self, identifier, settings_uuid):
        return self._get_property(self.Endpoints.Image, identifier, settings_uuid)

    def _get_security_group(self, identifier, settings_uuid):
        return self._get_property(self.Endpoints.SecurityGroup, identifier, settings_uuid)

    def _get_floating_ip(self, address):
        return self._query_resource(self.Endpoints.FloatingIP, {'address': address})

    def _get_subnet(self, identifier):
        return self._get_resource(self.Endpoints.Subnet, identifier)

    def _networks_to_payload(self, networks):
        """
        Serialize networks. Input should be in the following format:
            {
                subnet: name or uuid
                floating_ip: auto or address or empty
            }
        :return: a tuple, where first argument is subnets and second is floating_ips.
        """
        subnets = []
        floating_ips = []

        for item in networks:
            if 'subnet' not in item:
                raise ValidationError('Wrong networks format. subnet key is required.')
            subnet_resource = self._get_subnet(item['subnet'])
            subnet = {'subnet': subnet_resource['url']}
            subnets.append(subnet)
            address = item.get('floating_ip')
            if address:
                ip = subnet.copy()
                if address != 'auto':
                    floating_ip_resource = self._get_floating_ip(address)
                    ip.update({'url': floating_ip_resource['url']})
                floating_ips.append(ip)

        return subnets, floating_ips

    def _get_tenant_security_group(self, tenant_uuid, name):
        query = {
            'name': name,
            'tenant_uuid': tenant_uuid,
        }
        return self._query_resource(self.Endpoints.TenantSecurityGroup, query)

    def _is_resource_ready(self, endpoint, uuid):
        resource = self._query_resource_by_uuid(endpoint, uuid)
        return resource['state'] in self.resource_stable_states

    def _create_instance(self, payload):
        return self._create_resource(self.Endpoints.Instance, payload)

    def _get_tenant(self, name):
        return self._get_resource(self.Endpoints.Tenant, name)

    def _wait_for_resource(self, endpoint, uuid, interval, timeout):
        ready = self._is_resource_ready(endpoint, uuid)
        waited = 0
        while not ready:
            time.sleep(interval)
            ready = self._is_resource_ready(endpoint, uuid)
            waited += interval
            if waited >= timeout:
                error = 'Resource "%s" with id "%s" has not changed state to stable.' % (endpoint, uuid)
                message = '%s. Seconds passed: %s' % (error, timeout)
                raise TimeoutError(message)

    def _wait_for_external_ip(self, uuid, interval, timeout):
        ready = self._instance_has_external_ip(uuid)
        waited = 0
        while not ready:
            time.sleep(interval)
            ready = self._instance_has_external_ip(uuid)
            waited += interval
            if waited >= timeout:
                error = 'Resource "%s" with id "%s" has not got external IP.' % uuid
                message = '%s. Seconds passed: %s' % (error, timeout)
                raise TimeoutError(message)

    def _instance_has_external_ip(self, uuid):
        resource = self._query_resource_by_uuid(self.Endpoints.Instance, uuid)
        return len(resource['external_ips']) > 0

    def create_security_group(self,
                              tenant,
                              name,
                              rules,
                              description=None,
                              tags=None,
                              wait=True,
                              interval=10,
                              timeout=600):
        """
        Creates OpenStack security group via Waldur API from passed parameters.

        :param tenant: uuid or name of the tenant to use.
        :param name: name of the security group.
        :param rules: list of rules to add the security group.
        :param description: arbitrary text.
        :param tags: list of tags to add to the security group.
        :param wait: defines whether the client has to wait for security group provisioning.
        :param interval: interval of security group state polling in seconds.
        :param timeout: a maximum amount of time to wait for security group provisioning.
        :return: security group as a dictionary.
        """
        tenant = self._get_tenant(tenant)
        payload = {
            'name': name,
            'rules': rules
        }
        if description:
            payload.update({'description': description})
        if tags:
            payload.update({'tags': tags})

        action_url = '%s/%s/create_security_group' % (self.Endpoints.Tenant, tenant['uuid'])
        resource = self._create_resource(action_url, payload)

        if wait:
            self._wait_for_resource(self.Endpoints.TenantSecurityGroup, resource['uuid'], interval, timeout)

        return resource

    def update_security_group_description(self, security_group, description):
        payload = {
            'name': security_group['name'],
            'description': description,
        }
        uuid = security_group['uuid']
        return self._update_resource(self.Endpoints.TenantSecurityGroup, uuid, payload)

    def get_security_group(self, tenant, name):
        tenant = self._get_tenant(tenant)
        security_group = None
        try:
            security_group = self._get_tenant_security_group(tenant_uuid=tenant['uuid'], name=name)
        except ObjectDoesNotExist:
            pass

        return security_group

    def delete_security_group(self, uuid):
        return self._delete_resource(self.Endpoints.TenantSecurityGroup, uuid)

    def _get_instance(self, instance):
        return self._get_resource(self.Endpoints.Instance, instance)

    def assign_floating_ips(self, instance, floating_ips, wait=True, interval=20, timeout=600):
        instance = self._get_instance(instance)
        payload = {
            'floating_ips': [],
        }
        for ip in floating_ips:
            payload['floating_ips'].append({
                'url': self._get_floating_ip(ip['address'])['url'],
                'subnet': self._get_subnet(ip['subnet'])['url'],
            })

        endpoint = '%s/%s/update_floating_ips' % (self.Endpoints.Instance, instance['uuid'])
        response = self._create_resource(endpoint, payload, valid_state=202)

        if wait:
            self._wait_for_resource(self.Endpoints.Instance, instance['uuid'], interval, timeout)

        return response

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
            timeout=600,
            wait=True,
            ssh_key=None,
            data_volume_size=None,
            security_groups=None,
            tags=None,
            user_data=None,
            check_mode=False):
        """
        Creates OpenStack instance from passed parameters.

        :param name: name of the instance.
        :param provider: uuid or name of the provider to use.
        :param project: uuid or name of the project to add the instance.
        :param networks: a list of networks to attach instance to.
        :param flavor: uuid or name of the flavor to use.
        :param image: uuid or name of the image to use.
        :param system_volume_size: size of the system volume in GB.
        :param interval: interval of instance state polling in seconds.
        :param timeout: a maximum amount of time to wait for instance provisioning.
        :param wait: defines whether the client has to wait for instance provisioning.
        :param ssh_key: uuid or name of the ssh key to add to the instance.
        :param data_volume_size: size of the data volume in GB.
            No data volume is going to be created if empty.
        :param security_groups: list of security groups to add to the instance.
        :param tags: list of tags to add to the instance.
        :param user_data: additional data that will be added to the instance.
        :return: an instance as a dictionary.
        """
        provider = self._get_provider(provider)
        settings_uuid = provider['settings_uuid']
        project = self._get_project(project)
        service_project_link = self._get_service_project_link(
            provider_uuid=provider['uuid'],
            project_uuid=project['uuid'])
        flavor = self._get_flavor(flavor, settings_uuid)
        image = self._get_image(image, settings_uuid)
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
                security_group = self._get_security_group(group, settings_uuid)
                payload['security_groups'].append({'url': security_group['url']})

        if data_volume_size:
            payload.update({'data_volume_size': data_volume_size * 1024})
        if user_data:
            payload.update({'user_data': user_data})
        if ssh_key:
            ssh_key = self._get_resource(self.Endpoints.SshKey, ssh_key)
            payload.update({'ssh_public_key': ssh_key['url']})
        if tags:
            payload.update({'tags': tags})

        if check_mode:
            payload['WALDUR_CHECK_MODE'] = True
            return payload

        instance = self._create_instance(payload)

        if wait:
            self._wait_for_resource(self.Endpoints.Instance, instance['uuid'], interval, timeout)
            self._wait_for_external_ip(instance['uuid'], interval, timeout)

        return instance

    def get_instance(self, name, project):
        if is_uuid(name):
            return self._query_resource_by_uuid(self.Endpoints.Instance, name)
        else:
            query = {'project_name': project, 'name_exact': name}
            return self._query_resource(self.Endpoints.Instance, query)
