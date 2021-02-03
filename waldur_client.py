import json
import time
from uuid import UUID

import requests
import six
from six.moves.urllib.parse import urlencode, urljoin


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


class InvalidStateError(WaldurClientException):
    """Thrown when a resource transitions to the error state."""

    pass


class WaldurClient(object):
    class Endpoints(object):
        Provider = 'openstacktenant'
        Project = 'projects'
        Flavor = 'openstacktenant-flavors'
        Subnet = 'openstacktenant-subnets'
        FloatingIP = 'openstacktenant-floating-ips'
        SecurityGroup = 'openstacktenant-security-groups'
        Image = 'openstacktenant-images'
        Instance = 'openstacktenant-instances'
        Snapshot = 'openstacktenant-snapshots'
        SshKey = 'keys'
        Tenant = 'openstack-tenants'
        TenantSecurityGroup = 'openstack-security-groups'
        Volume = 'openstacktenant-volumes'
        VolumeType = 'openstacktenant-volume-types'
        OpenStackPackage = 'openstack-packages'
        MarketplaceOffering = 'marketplace-offerings'
        MarketplacePlan = 'marketplace-plans'
        MarketplaceOrder = 'marketplace-orders'
        MarketplaceResources = 'marketplace-resources'
        MarketplaceCategories = 'marketplace-categories'
        Customers = 'customers'

    marketplaceScopeEndpoints = {
        'OpenStackTenant.Instance': Endpoints.Instance,
        'OpenStackTenant.Volume': Endpoints.Volume,
    }

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
        return urljoin(self.api_url, self._ensure_trailing_slash(endpoint))

    def _build_resource_url(self, endpoint, uuid, action=None):
        parts = [endpoint, uuid]
        if action:
            parts.append(action)
        return self._build_url('/'.join(parts))

    def _parse_error(self, response):
        try:
            reason = response.json()
        except ValueError:
            reason = 'Unable to parse JSON'
        details = 'Status: %s. Reason: %s.' % (response.status_code, reason)
        return 'Server refuses to communicate. %s' % details

    def _make_request(self, method, url, valid_states, retry_count=3, **kwargs):
        if retry_count == 0:
            raise WaldurClientException('Reached a limit of retries for the operation: %s %s' % (method, url))

        params = dict(headers=self.headers)
        params.update(kwargs)

        try:
            response = getattr(requests, method)(url, **params)
        except requests.exceptions.RequestException as error:
            raise WaldurClientException(six.text_type(error))

        if response.status_code not in valid_states:
            # a special treatment for 409 response, which can be due to async operations
            if response.status_code == 409:
                time.sleep(2)  # wait for things to calm down
                return self._make_request(method, url, valid_states, retry_count - 1, **kwargs)
            error = self._parse_error(response)
            raise WaldurClientException(error)

        return response.json()

    def _get_paginated_data(self, url, **kwargs):
        params = dict(headers=self.headers)
        params.update(kwargs)
        params['page_size'] = 200

        try:
            response = requests.get(url, **params)
        except requests.exceptions.RequestException as error:
            raise WaldurClientException(six.text_type(error))

        if response.status_code != 200:
            error = self._parse_error(response)
            raise WaldurClientException(error)
        result = response.json()
        while 'next' in response.headers['Link']:
            if 'prev' in response.headers['Link']:
                next_url = response.headers['Link'].split(', ')[2].split('; ')[0][1:-1]
            else:  # First page case
                next_url = response.headers['Link'].split(', ')[1].split('; ')[0][1:-1]
            try:
                response = requests.get(next_url, **params)
            except requests.exceptions.RequestException as error:
                raise WaldurClientException(six.text_type(error))

            if response.status_code != 200:
                error = self._parse_error(response)
                raise WaldurClientException(error)

            result += response.json()

        return result

    def _get(self, url, valid_states, **kwargs):
        return self._make_request('get', url, valid_states, 1, **kwargs)

    def _post(self, url, valid_states, **kwargs):
        return self._make_request('post', url, valid_states, 3, **kwargs)

    def _put(self, url, valid_states, **kwargs):
        return self._make_request('put', url, valid_states, 3, **kwargs)

    def _delete(self, url, valid_states, **kwargs):
        return self._make_request('delete', url, valid_states, 3, **kwargs)

    def _make_get_query(self, url, query_params, get_first=False, get_few=False):
        """
        Get object via Waldur API.

        :param url: URL.
        :param query_params: dict with query params.
        :param get_first: If True then will return the first result.
        :param get_few: If True then will return all results.

        Note:
        If get_first or get_few have been set, then multiple results are correct.
        In the first case, we get the first result, in the second case we get all results.
        If get_first or get_few have not been set, then multiple results are not correct."""

        result = self._get(url, valid_states=[200], params=query_params)
        if not result:
            message = 'Result is empty. Endpoint: %s. Query: %s' % (url, query_params)
            raise ObjectDoesNotExist(message)

        if isinstance(result, dict):
            return result

        if len(result) > 1:
            if not get_first and not get_few:
                message = 'Ambiguous result. Endpoint: %s. Query: %s' % (
                    url,
                    query_params,
                )
                raise MultipleObjectsReturned(message)
            elif get_few:
                return result

        return result if get_few else result[0]

    def _query_resource(self, endpoint, query_params, get_first=False):
        url = self._build_url(endpoint)
        if 'uuid' in query_params:
            url += query_params.pop('uuid') + '/'

        return self._make_get_query(url, query_params, get_first)

    def _query_resource_by_uuid(self, endpoint, value, extra=None):
        payload = {'uuid': value}
        if extra:
            payload.update(extra)
        return self._query_resource(endpoint, payload)

    def _query_resource_by_name(self, endpoint, value, extra=None):
        payload = {'name_exact': value}
        if extra:
            payload.update(extra)
        return self._query_resource(endpoint, payload)

    def _query_resource_list(self, endpoint, query_params):
        url = self._build_url(endpoint)
        return self._get_paginated_data(url, params=query_params)

    def _get_resource(self, endpoint, value, extra=None):
        """
        Get resource by UUID, name or query parameters.

        :param endpoint: WaldurClient.Endpoint attribute.
        :param value: name or uuid of the resource
        :return: a resource as a dictionary.
        :raises: WaldurClientException if resource could not be received or response failed.
        """
        if is_uuid(value):
            return self._query_resource_by_uuid(endpoint, value, extra)
        else:
            return self._query_resource_by_name(endpoint, value, extra)

    def _create_resource(self, endpoint, payload=None, valid_state=201):
        url = self._build_url(endpoint)
        return self._post(url, [valid_state], json=payload)

    def _update_resource(self, endpoint, uuid, payload):
        url = self._build_resource_url(endpoint, uuid)
        return self._put(url, [200], data=json.dumps(payload))

    def _delete_resource_by_url(self, url):
        return self._delete(url, [202, 204])

    def _delete_resource(self, endpoint, uuid):
        url = self._build_resource_url(endpoint, uuid)
        return self._delete_resource_by_url(url)

    def _execute_resource_action(self, endpoint, uuid, action, **kwargs):
        url = self._build_resource_url(endpoint, uuid, action)
        return self._post(url, [202], **kwargs)

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

    def _get_flavor_from_params(self, cpu, ram):
        query_params = {'o': 'cores,ram,disk'}
        if cpu:
            query_params['cores__gte'] = cpu
        if ram:
            query_params['ram__gte'] = ram

        return self._query_resource(self.Endpoints.Flavor, query_params, get_first=True)

    def _get_image(self, identifier, settings_uuid):
        return self._get_property(self.Endpoints.Image, identifier, settings_uuid)

    def _get_security_group(self, identifier, settings_uuid):
        return self._get_property(
            self.Endpoints.SecurityGroup, identifier, settings_uuid
        )

    def _get_floating_ip(self, address):
        return self._query_resource(self.Endpoints.FloatingIP, {'address': address})

    def _get_subnet(self, identifier):
        return self._get_resource(self.Endpoints.Subnet, identifier)

    def _get_volume_type(self, identifier, settings_uuid):
        return self._get_property(self.Endpoints.VolumeType, identifier, settings_uuid)

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
            'name_exact': name,
            'tenant_uuid': tenant_uuid,
        }
        return self._query_resource(self.Endpoints.TenantSecurityGroup, query)

    def _get_tenant_security_groups(self, tenant_uuid):
        query = {
            'tenant_uuid': tenant_uuid
        }
        return self._query_resource_list(self.Endpoints.TenantSecurityGroup, query)

    def _is_resource_ready(self, endpoint, uuid):
        resource = self._query_resource_by_uuid(endpoint, uuid)
        if resource['state'] == 'Erred':
            raise InvalidStateError('Resource is in erred state.')
        return resource['state'] == 'OK'

    def _create_instance(self, payload):
        return self._create_resource(self.Endpoints.Instance, payload)

    def _get_tenant(self, name, project=None):
        """
        Find OpenStack tenant resource in Waldur database.
        :param name: OpenStack name or UUID.
        :param project: Waldur project name or UUID.
        :return: OpenStack tenant as Waldur resource.
        """
        extra = None
        if project:
            project = self._get_project(project)
            extra = {'project_uuid': project['uuid']}
        return self._get_resource(self.Endpoints.Tenant, name, extra)

    def _wait_for_resource(self, endpoint, uuid, interval, timeout):
        ready = self._is_resource_ready(endpoint, uuid)
        waited = 0
        while not ready:
            time.sleep(interval)
            ready = self._is_resource_ready(endpoint, uuid)
            waited += interval
            if waited >= timeout:
                error = (
                    'Resource "%s" with id "%s" has not changed state to stable.'
                    % (endpoint, uuid)
                )
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

    def create_security_group(
        self,
        tenant,
        name,
        rules,
        project=None,
        description=None,
        tags=None,
        wait=True,
        interval=10,
        timeout=600,
    ):
        """
        Creates OpenStack security group via Waldur API from passed parameters.

        :param tenant: uuid or name of the tenant to use.
        :param name: name of the security group.
        :param rules: list of rules to add the security group.
        :param project: name of the Waldur project where OpenStack tenant is located.
        :param description: arbitrary text.
        :param tags: list of tags to add to the security group.
        :param wait: defines whether the client has to wait for security group provisioning.
        :param interval: interval of security group state polling in seconds.
        :param timeout: a maximum amount of time to wait for security group provisioning.
        :return: security group as a dictionary.
        """
        tenant = self._get_tenant(tenant, project)
        payload = {'name': name, 'rules': rules}
        if description:
            payload.update({'description': description})
        if tags:
            payload.update({'tags': tags})

        action_url = '%s/%s/create_security_group' % (
            self.Endpoints.Tenant,
            tenant['uuid'],
        )
        resource = self._create_resource(action_url, payload)

        if wait:
            self._wait_for_resource(
                self.Endpoints.TenantSecurityGroup, resource['uuid'], interval, timeout
            )

        return resource

    def update_security_group_description(self, security_group, description):
        payload = {
            'name': security_group['name'],
            'description': description,
        }
        uuid = security_group['uuid']
        return self._update_resource(self.Endpoints.TenantSecurityGroup, uuid, payload)

    def update_security_group_rules(self, security_group, rules):
        return self._execute_resource_action(
            endpoint=self.Endpoints.TenantSecurityGroup,
            uuid=security_group['uuid'],
            action='set_rules',
            json=rules,
        )

    def get_security_group(self, tenant, name):
        tenant = self._get_tenant(tenant)
        security_group = None
        try:
            security_group = self._get_tenant_security_group(
                tenant_uuid=tenant['uuid'], name=name
            )
        except ObjectDoesNotExist:
            pass

        return security_group

    def list_security_group(self, tenant):
        tenant = self._get_tenant(tenant)
        return self._get_tenant_security_groups(tenant['uuid'])

    def delete_security_group(self, uuid):
        return self._delete_resource(self.Endpoints.TenantSecurityGroup, uuid)

    def _get_instance(self, instance):
        return self._get_resource(self.Endpoints.Instance, instance)

    def assign_floating_ips(
        self, instance, floating_ips, wait=True, interval=20, timeout=600
    ):
        instance = self._get_instance(instance)
        payload = {
            'floating_ips': [],
        }
        for ip in floating_ips:
            payload['floating_ips'].append(
                {
                    'url': self._get_floating_ip(ip['address'])['url'],
                    'subnet': self._get_subnet(ip['subnet'])['url'],
                }
            )

        endpoint = '%s/%s/update_floating_ips' % (
            self.Endpoints.Instance,
            instance['uuid'],
        )
        response = self._create_resource(endpoint, payload, valid_state=202)

        if wait:
            self._wait_for_resource(
                self.Endpoints.Instance, instance['uuid'], interval, timeout
            )

        return response

    def create_instance(
        self,
        name,
        provider,
        project,
        networks,
        image,
        system_volume_size,
        description=None,
        flavor=None,
        flavor_min_cpu=None,
        flavor_min_ram=None,
        interval=10,
        timeout=600,
        wait=True,
        ssh_key=None,
        data_volume_size=None,
        security_groups=None,
        tags=None,
        user_data=None,
        check_mode=False,
    ):
        """
        Creates OpenStack instance from passed parameters.

        :param name: name of the instance.
        :param description: description of the instance.
        :param provider: uuid or name of the provider to use.
        :param project: uuid or name of the project to add the instance.
        :param networks: a list of networks to attach instance to.
        :param flavor: uuid or name of the flavor to use.
        :param flavor_min_cpu: min cpu count.
        :param flavor_min_ram: min ram size (MB).
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
        if flavor:
            flavor = self._get_flavor(flavor, settings_uuid)
        else:
            flavor = self._get_flavor_from_params(flavor_min_cpu, flavor_min_ram)

        image = self._get_image(image, settings_uuid)
        subnets, floating_ips = self._networks_to_payload(networks)

        payload = {
            'name': name,
            'flavor': flavor['url'],
            'image': image['url'],
            'service_settings': provider['settings'],
            'project': project['url'],
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
        if description:
            payload['description'] = description

        if check_mode:
            return payload

        instance = self._create_instance(payload)

        if wait:
            self._wait_for_resource(
                self.Endpoints.Instance, instance['uuid'], interval, timeout
            )
            if floating_ips:
                self._wait_for_external_ip(instance['uuid'], interval, timeout)

        return instance

    def _get_project_resource(self, endpoint, name, project=None):
        if is_uuid(name):
            return self._query_resource_by_uuid(endpoint, name)
        else:
            if project is None:
                raise ValidationError(
                    "You should specify project name if name is not UUID"
                )
            query = {'project_name': project, 'name_exact': name}
            return self._query_resource(endpoint, query)

    def get_instance(self, name, project=None):
        """
        Deprecated. Use get_instance_via_marketplace marketplace method"""
        return self._get_project_resource(self.Endpoints.Instance, name, project)

    def get_marketplace_resource_scope(self, name, offering_type, project=None):
        """Get marketplace resource scope. Depending on the offering type scope type can be different.

            :param name: name of the scope.
            :param offering_type: marketplace offering type.
            :param project: project UUID or name.
        """

        if not is_uuid(name) and not project:
            raise ValidationError("You should specify project name if name is not UUID")

        endpoint = self.Endpoints.MarketplaceResources
        url = self._build_url(endpoint)
        params = {
            'offering_type': offering_type,
        }

        if is_uuid(name):
            params['scope'] = self._build_url(
                self.marketplaceScopeEndpoints[offering_type] + '/' + name
            )
        else:
            params['state'] = ['Creating', 'OK', 'Erred', 'Updating', 'Terminating']
            params['name_exact'] = name

        if project:
            if is_uuid(project):
                params['project_uuid'] = project
            else:
                params['project_name'] = project

        result = self._get(url, valid_states=[200], params=params)

        if not result:
            message = 'Result is empty. Endpoint: %s. Query: %s' % (endpoint, params)
            raise ObjectDoesNotExist(message)

        if len(result) > 1:
            message = 'Ambiguous result. Endpoint: %s. Query: %s' % (url, params)
            raise MultipleObjectsReturned(message)

        scope = self._get(result[0]['scope'], valid_states=[200])
        if not scope:
            message = 'Result is empty. Endpoint: %s. Query: %s' % (endpoint, params)
            raise ObjectDoesNotExist(message)

        return result[0], scope

    def get_instance_via_marketplace(self, name, project=None):
        """Get an openstack instance via marketplace.

            :param name: name of the instance.
            :param project: project UUID or name.
        """
        resource, instance = self.get_marketplace_resource_scope(
            name, 'OpenStackTenant.Instance', project
        )
        return instance

    def get_volume_via_marketplace(self, name, project=None):
        """Get an openstack volume via marketplace.

            :param name: name of the volume.
            :param project: project UUID or name.
        """
        resource, instance = self.get_marketplace_resource_scope(
            name, 'OpenStackTenant.Volume', project
        )
        return instance

    def stop_instance(self, uuid, wait=True, interval=10, timeout=600):
        """
        Stop OpenStack instance and wait until operation is completed.

        :param uuid: unique identifier of the instance
        :param wait: defines whether the client has to wait for operation completion.
        :param interval: interval of volume state polling in seconds.
        :param timeout: a maximum amount of time to wait for operation completion.
        """
        self._execute_resource_action(self.Endpoints.Instance, uuid, 'stop')
        if wait:
            self._wait_for_resource(self.Endpoints.Instance, uuid, interval, timeout)

    def delete_instance(self, uuid, delete_volumes=True, release_floating_ips=True):
        base_url = self._build_resource_url(self.Endpoints.Instance, uuid)
        params = dict(
            delete_volumes=delete_volumes, release_floating_ips=release_floating_ips
        )
        url = base_url + '?' + urlencode(params)
        return self._delete_resource_by_url(url)

    def update_instance_security_groups(
        self,
        instance_uuid,
        settings_uuid,
        security_groups,
        wait=True,
        interval=10,
        timeout=600,
    ):
        """
        Update security groups for OpenStack instance and wait until operation is completed.

        :param instance_uuid: unique identifier of the instance
        :param settings_uuid: unique identifier of the service settings
        :param security_groups: list of security group names
        :param wait: defines whether the client has to wait for operation completion.
        :param interval: interval of volume state polling in seconds.
        :param timeout: a maximum amount of time to wait for operation completion.
        """
        payload = []
        for group in security_groups:
            security_group = self._get_security_group(group, settings_uuid)
            payload.append({'url': security_group['url']})

        self._execute_resource_action(
            endpoint=self.Endpoints.Instance,
            uuid=instance_uuid,
            action='update_security_groups',
            json=dict(security_groups=payload),
        )
        if wait:
            self._wait_for_resource(
                self.Endpoints.Instance, instance_uuid, interval, timeout
            )

    def get_volume(self, name, project=None):
        return self._get_project_resource(self.Endpoints.Volume, name, project)

    def _get_volume(self, name):
        return self._get_resource(self.Endpoints.Volume, name)

    def update_volume(self, volume, description):
        payload = {
            'name': volume['name'],
            'description': description,
        }
        uuid = volume['uuid']
        return self._update_resource(self.Endpoints.Volume, uuid, payload)

    def delete_volume(self, uuid):
        return self._delete_resource(self.Endpoints.Volume, uuid)

    def create_volume(
        self,
        name,
        project,
        provider,
        size,
        description=None,
        tags=None,
        wait=True,
        interval=10,
        timeout=600,
    ):
        """
        Creates OpenStack volume via Waldur API from passed parameters.

        :param name: name of the volume.
        :param project: uuid or name of the project to add the volume to.
        :param provider: uuid or name of the provider to use.
        :param size: size of the volume in GBs.
        :param description: arbitrary text.
        :param tags: list of tags to add to the volume.
        :param wait: defines whether the client has to wait for volume provisioning.
        :param interval: interval of volume state polling in seconds.
        :param timeout: a maximum amount of time to wait for volume provisioning.
        :return: volume as a dictionary.
        """
        provider = self._get_provider(provider)
        project = self._get_project(project)

        payload = {
            'name': name,
            'service_settings': provider['settings'],
            'project': project['url'],
            'size': size * 1024,
        }
        if description:
            payload.update({'description': description})
        if tags:
            payload.update({'tags': tags})

        resource = self._create_resource(self.Endpoints.Volume, payload)

        if wait:
            self._wait_for_resource(
                self.Endpoints.Volume, resource['uuid'], interval, timeout
            )

        return resource

    def detach_volume(self, uuid, wait=True, interval=10, timeout=600):
        """
        Detach OpenStack volume from instance and wait until operation is completed.

        :param uuid: unique identifier of the volume
        :param wait: defines whether the client has to wait for operation completion.
        :param interval: interval of volume state polling in seconds.
        :param timeout: a maximum amount of time to wait for operation completion.
        """
        self._execute_resource_action(self.Endpoints.Volume, uuid, 'detach')
        if wait:
            self._wait_for_resource(self.Endpoints.Volume, uuid, interval, timeout)

    def attach_volume(
        self, volume, instance, device, wait=True, interval=10, timeout=600
    ):
        """
        Detach OpenStack volume from instance and wait until operation is completed.

        :param volume: unique identifier of the volume
        :param instance: unique identifier of the instance
        :param device: name of volume as instance device e.g. /dev/vdb
        :param wait: defines whether the client has to wait for operation completion.
        :param interval: interval of volume state polling in seconds.
        :param timeout: a maximum amount of time to wait for operation completion.
        """
        payload = dict(
            instance=self._build_resource_url(self.Endpoints.Instance, instance),
            device=device,
        )
        self._execute_resource_action(
            self.Endpoints.Volume, volume, 'attach', json=payload
        )
        if wait:
            self._wait_for_resource(self.Endpoints.Volume, volume, interval, timeout)

    def get_snapshot(self, name):
        return self._get_resource(self.Endpoints.Snapshot, name)

    def delete_snapshot(self, uuid):
        return self._delete_resource(self.Endpoints.Snapshot, uuid)

    def create_snapshot(
        self,
        name,
        volume,
        kept_until=None,
        description=None,
        tags=None,
        wait=True,
        interval=10,
        timeout=600,
    ):
        """
        Creates OpenStack snapshot via Waldur API from passed parameters.

        :param name: name of the snapshot.
        :param volume: name or ID of the volume.
        :param kept_until: Guaranteed time of snapshot retention. If null - keep forever.
        :param description: arbitrary text.
        :param tags: list of tags to add to the snapshot.
        :param wait: defines whether the client has to wait for snapshot provisioning.
        :param interval: interval of snapshot state polling in seconds.
        :param timeout: a maximum amount of time to wait for snapshot provisioning.
        :return: snapshot as a dictionary.
        """
        volume = self._get_volume(volume)
        payload = {
            'name': name,
        }
        if description:
            payload.update({'description': description})
        if tags:
            payload.update({'tags': tags})
        if kept_until:
            payload.update({'kept_until': kept_until})

        action_url = '%s/%s/snapshot' % (self.Endpoints.Volume, volume['uuid'])
        resource = self._create_resource(action_url, payload)

        if wait:
            self._wait_for_resource(
                self.Endpoints.Snapshot, resource['uuid'], interval, timeout
            )

        return resource

    def update_instance_internal_ips_set(
        self, instance_uuid, subnet_set, wait=True, interval=10, timeout=600
    ):
        """
        Update internal ip for OpenStack instance and wait until operation is completed.

        :param instance_uuid: unique identifier of the instance
        :param subnet_set: list of subnet names
        :param wait: defines whether the client has to wait for operation completion.
        :param interval: interval of volume state polling in seconds.
        :param timeout: a maximum amount of time to wait for operation completion.
        """

        payload = {'internal_ips_set': []}
        for subnet in subnet_set:
            subnet = self._get_subnet(subnet)
            payload['internal_ips_set'].append({'subnet': subnet['url']})

        self._execute_resource_action(
            endpoint=self.Endpoints.Instance,
            uuid=instance_uuid,
            action='update_internal_ips_set',
            json=payload,
        )
        if wait:
            self._wait_for_resource(
                self.Endpoints.Instance, instance_uuid, interval, timeout
            )

    def _get_offering(self, offering, project=None):
        """
        Get marketplace offering.

        :param offering: the name or UUID of the offering.
        :param project: the name or UUID of the project. It is required if offering is not UUID.
        :return: marketplace offering.
        """
        if is_uuid(offering):
            return self._get_resource(self.Endpoints.MarketplaceOffering, offering)
        elif project:
            if is_uuid(project):
                project_uuid = project
            else:
                project = self._get_resource(self.Endpoints.Project, project)
                project_uuid = project['uuid']

            return self._get_resource(
                self.Endpoints.MarketplaceOffering,
                offering,
                {'project_uuid': project_uuid},
            )
        else:
            return

    def _get_plan(self, identifier):
        return self._get_resource(self.Endpoints.MarketplacePlan, identifier)

    def create_marketplace_order(
        self, project, offering, plan=None, attributes=None, limits=None
    ):
        """
        Create order with one item in Waldur Marketplace.

        :param project: the name or UUID of the project
        :param offering: the name or UUID of the offering
        :param plan: the name or UUID of the plan.
        :param attributes: order item attributes.
        :param limits: order item limits.
        """
        project_url = self._get_project(project)['url']
        offering_url = self._get_offering(offering, project)['url']

        attributes = attributes or {}
        limits = limits or {}
        order_item = {
            'offering': offering_url,
            'attributes': attributes,
            'limits': limits,
        }

        if plan:
            plan_url = self._get_plan(plan)['url']
            order_item['plan'] = plan_url

        # TODO: replace with checkbox data from frontend
        order_item['accepting_terms_of_service'] = True

        payload = {
            'project': project_url,
            'items': [order_item],
        }
        return self._create_resource(self.Endpoints.MarketplaceOrder, payload=payload)

    def _create_scope_via_marketplace(
        self,
        name,
        offering,
        project,
        attributes,
        scope_endpoint,
        interval=10,
        timeout=600,
        wait=True,
        check_mode=False,
    ):
        """
        Create marketplace resource scope via marketplace.

        :param name: the name of scope.
        :param offering: the name or UUID of marketplace offering.
        :param project: the name or UUID of the project.
        :param attributes: order item attributes.
        :param scope_endpoint: scope endpoint.
        :param interval: interval of instance state polling in seconds.
        :param timeout: a maximum amount of time to wait for instance provisioning.
        :param wait: defines whether the client has to wait for instance provisioning.
        :param check_mode: True for check mode.
        :return: scope.
        """
        offering = self._get_offering(offering, project)
        offering_type = offering['type']

        if check_mode:
            return {
                'attributes': attributes,
                'project': project,
                'offering': offering['uuid'],
            }

        order = self.create_marketplace_order(
            project, offering['uuid'], attributes=attributes
        )
        order_uuid = order['uuid']
        scope = None
        waited = 0
        while not scope:
            time.sleep(interval)
            order = self._get_resource(
                WaldurClient.Endpoints.MarketplaceOrder, order_uuid
            )
            if order['items'][0]['state'] == 'erred':
                raise InvalidStateError(order['items'][0]['error_message'])

            try:
                resource, scope = self.get_marketplace_resource_scope(
                    name, offering_type, project
                )
            except ObjectDoesNotExist:
                pass
            waited += interval
            if waited >= timeout:
                error = (
                    'Marketplace resource of scope with name "%s" is not found.' % name
                )
                message = '%s. Seconds passed: %s' % (error, timeout)
                raise TimeoutError(message)

        if wait:
            self._wait_for_resource(scope_endpoint, scope['uuid'], interval, timeout)

        return scope

    def create_instance_via_marketplace(
        self,
        name,
        offering,
        project,
        networks,
        image,
        system_volume_size,
        description=None,
        flavor=None,
        flavor_min_cpu=None,
        flavor_min_ram=None,
        interval=10,
        timeout=600,
        wait=True,
        ssh_key=None,
        data_volume_size=None,
        security_groups=None,
        tags=None,
        user_data=None,
        check_mode=False,
        system_volume_type=None,
        data_volume_type=None,
    ):
        """
        Create OpenStack instance from passed parameters via marketplace.

        :param name: name of the instance.
        :param description: description of the instance.
        :param provider: uuid or name of the provider to use.
        :param project: uuid or name of the project to add the instance.
        :param networks: a list of networks to attach instance to.
        :param flavor: uuid or name of the flavor to use.
        :param flavor_min_cpu: min cpu count.
        :param flavor_min_ram: min ram size (MB).
        :param image: uuid or name of the image to use.
        :param system_volume_size: size of the system volume in GB.
        :param system_volume_type: UUID or name of system volume type.
        :param interval: interval of instance state polling in seconds.
        :param timeout: a maximum amount of time to wait for instance provisioning.
        :param wait: defines whether the client has to wait for instance provisioning.
        :param ssh_key: uuid or name of the ssh key to add to the instance.
        :param data_volume_size: size of the data volume in GB.
            No data volume is going to be created if empty.
        :param data_volume_type: UUID or name of data volume type.
        :param security_groups: list of security groups to add to the instance.
        :param tags: list of tags to add to the instance.
        :param user_data: additional data that will be added to the instance.
        :return: an instance as a dictionary.
        """
        offering = self._get_offering(offering, project)
        settings_uuid = offering['scope_uuid']

        # Collect attributes
        if flavor:
            flavor = self._get_flavor(flavor, settings_uuid)
        else:
            flavor = self._get_flavor_from_params(flavor_min_cpu, flavor_min_ram)

        image = self._get_image(image, settings_uuid)
        subnets, floating_ips = self._networks_to_payload(networks)

        attributes = {
            'name': name,
            'flavor': flavor['url'],
            'image': image['url'],
            'system_volume_size': system_volume_size * 1024,
            'internal_ips_set': subnets,
            'floating_ips': floating_ips,
        }

        if security_groups:
            attributes['security_groups'] = []
            for group in security_groups:
                security_group = self._get_security_group(group, settings_uuid)
                attributes['security_groups'].append({'url': security_group['url']})

        if data_volume_size:
            attributes.update({'data_volume_size': data_volume_size * 1024})
        if user_data:
            attributes.update({'user_data': user_data})
        if ssh_key:
            ssh_key = self._get_resource(self.Endpoints.SshKey, ssh_key)
            attributes.update({'ssh_public_key': ssh_key['url']})
        if description:
            attributes['description'] = description
        if tags:
            attributes.update({'tags': tags})
        if system_volume_type:
            volume_type = self._get_volume_type(system_volume_type, settings_uuid)
            attributes.update({'system_volume_type': volume_type['url']})
        if data_volume_type:
            volume_type = self._get_volume_type(data_volume_type, settings_uuid)
            attributes.update({'data_volume_type': volume_type['url']})

        instance = self._create_scope_via_marketplace(
            name,
            offering['uuid'],
            project,
            attributes,
            scope_endpoint=self.Endpoints.Instance,
            interval=interval,
            timeout=timeout,
            wait=wait,
            check_mode=check_mode,
        )

        if wait and floating_ips:
            self._wait_for_external_ip(instance['uuid'], interval, timeout)

        return instance

    def _delete_scope_via_marketplace(self, scope_uuid, offering_type, options=None):
        if options:
            options = {'attributes': options}
        resource, scope = self.get_marketplace_resource_scope(scope_uuid, offering_type)
        url = self._build_resource_url(
            self.Endpoints.MarketplaceResources, resource['uuid'], action='terminate'
        )
        order_uuid = self._post(url, valid_states=[200], json=options)['order_uuid']
        return order_uuid

    def delete_instance_via_marketplace(self, instance_uuid, **kwargs):
        """
        Delete OpenStack instance via marketplace.

        :param instance_uuid: instance UUID.
        """
        return self._delete_scope_via_marketplace(
            instance_uuid, 'OpenStackTenant.Instance', options=kwargs
        )

    def create_volume_via_marketplace(
        self,
        name,
        project,
        offering,
        size,
        volume_type=None,
        description=None,
        tags=None,
        wait=True,
        interval=10,
        timeout=600,
    ):
        """
        Create OpenStack volume from passed parameters via marketplace.

        :param name: name of the volume.
        :param project: uuid or name of the project to add the volume to.
        :param provider: uuid or name of the provider to use.
        :param size: size of the volume in GBs.
        :param type: uuid or name of volume type.
        :param description: arbitrary text.
        :param tags: list of tags to add to the volume.
        :param wait: defines whether the client has to wait for volume provisioning.
        :param interval: interval of volume state polling in seconds.
        :param timeout: a maximum amount of time to wait for volume provisioning.
        :return: volume as a dictionary.
        """

        offering = self._get_offering(offering, project)
        settings_uuid = offering['scope_uuid']

        # Collect attributes
        attributes = {
            'name': name,
            'size': size * 1024,
        }
        if description:
            attributes.update({'description': description})
        if tags:
            attributes.update({'tags': tags})
        if volume_type:
            volume_type = self._get_volume_type(volume_type, settings_uuid)
            attributes.update({'type': volume_type['url']})

        return self._create_scope_via_marketplace(
            name,
            offering['uuid'],
            project,
            attributes,
            scope_endpoint=self.Endpoints.Volume,
            interval=interval,
            timeout=timeout,
            wait=wait,
        )

    def delete_volume_via_marketplace(self, volume_uuid):
        """
        Delete OpenStack volume via marketplace.

        :param volume_uuid: volume UUID.
        """
        return self._delete_scope_via_marketplace(volume_uuid, 'OpenStackTenant.Volume')

    def create_offering(self, params, check_mode=False):
        """
        Create an offering with specified parameters

        :param params: dict with parameters
        :param check_mode: True for check mode.
        :return: new offering information
        """
        category_url = self._get_resource(
            self.Endpoints.MarketplaceCategories, params['category']
        )['url']
        params['category'] = category_url
        if params['customer']:
            customer_url = self._get_resource(
                self.Endpoints.Customers, params['customer']
            )['url']
            params['customer'] = customer_url

        if check_mode:
            return params, False

        else:
            resource = self._create_resource(
                self.Endpoints.MarketplaceOffering, payload=params
            )

            return resource, True


def waldur_full_argument_spec(**kwargs):
    spec = dict(
        api_url=dict(required=True, type='str'),
        access_token=dict(required=True, type='str', no_log=True),
        wait=dict(default=True, type='bool'),
        timeout=dict(default=600, type='int'),
        interval=dict(default=20, type='int'),
    )
    spec.update(kwargs)
    return spec


def waldur_resource_argument_spec(**kwargs):
    spec = dict(
        name=dict(required=True, type='str'),
        description=dict(type='str', default=''),
        state=dict(default='present', choices=['absent', 'present']),
        tags=dict(type='list', default=None),
    )
    spec.update(waldur_full_argument_spec(**kwargs))
    return spec


def waldur_client_from_module(module):
    return WaldurClient(module.params['api_url'], module.params['access_token'])
