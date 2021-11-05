import dataclasses
import json
import time
from enum import Enum
from typing import List
from urllib.parse import urlencode, urljoin
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


class InvalidStateError(WaldurClientException):
    """Thrown when a resource transitions to the error state."""

    pass


@dataclasses.dataclass
class ComponentUsage:
    # TODO: rename to 'component_type' after https://opennode.atlassian.net/browse/WAL-4259 is done
    type: str
    amount: int
    description: str = ''


@dataclasses.dataclass
class ResourceReportRecord:
    header: str
    body: str


class ResourceState(Enum):
    OK = 'ok'
    ERRED = 'erred'
    TERMINATED = 'terminated'


class ProjectRole(Enum):
    ADMINISTRATOR = 'admin'
    MANAGER = 'manager'
    MEMBER = 'member'


class PaymentProfileType(Enum):
    FIXED_PRICE = 'fixed_price'
    MONTHLY_INVOICES = 'invoices'
    PAYMENT_GW_MONTHLY = 'payment_gw_monthly'


class WaldurClient(object):
    class Endpoints(object):
        ComponentUsage = 'marketplace-component-usages'
        CustomerPermissions = 'customer-permissions'
        Customers = 'customers'
        Flavor = 'openstacktenant-flavors'
        FloatingIP = 'openstacktenant-floating-ips'
        Image = 'openstacktenant-images'
        Instance = 'openstacktenant-instances'
        Invoice = 'invoices'
        MarketplaceCategories = 'marketplace-categories'
        MarketplaceOffering = 'marketplace-offerings'
        MarketplaceOrder = 'marketplace-orders'
        MarketplaceOrderItem = 'marketplace-order-items'
        MarketplacePlan = 'marketplace-plans'
        MarketplaceResources = 'marketplace-resources'
        OfferingPermissions = 'marketplace-offering-permissions'
        OfferingUsers = 'marketplace-offering-users'
        PaymentProfiles = 'payment-profiles'
        Project = 'projects'
        ProjectPermissions = 'project-permissions'
        ProjectTypes = 'project-types'
        Provider = 'service-settings'
        RemoteEduteams = 'remote-eduteams'
        SecurityGroup = 'openstacktenant-security-groups'
        ServiceProviders = 'marketplace-service-providers'
        Snapshot = 'openstacktenant-snapshots'
        SshKey = 'keys'
        Subnet = 'openstacktenant-subnets'
        Tenant = 'openstack-tenants'
        TenantSecurityGroup = 'openstack-security-groups'
        UserInvitations = 'user-invitations'
        Users = 'users'
        Volume = 'openstacktenant-volumes'
        VolumeType = 'openstacktenant-volume-types'

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

    def _build_resource_url(self, endpoint, uid, action=None):
        parts = [endpoint, str(uid)]
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
            raise WaldurClientException(
                'Reached a limit of retries for the operation: %s %s' % (method, url)
            )

        params = dict(headers=self.headers)
        params.update(kwargs)

        try:
            response = getattr(requests, method)(url, **params)
        except requests.exceptions.RequestException as error:
            raise WaldurClientException(str(error))

        if response.status_code not in valid_states:
            # a special treatment for 409 response, which can be due to async operations
            if response.status_code == 409:
                time.sleep(2)  # wait for things to calm down
                return self._make_request(
                    method, url, valid_states, retry_count - 1, **kwargs
                )
            error = self._parse_error(response)
            raise WaldurClientException(error)

        if method == 'head':
            return response
        if response.text:
            return response.json()
        return ''

    def _get_all(self, url, **kwargs):
        params = dict(headers=self.headers)
        params.update(kwargs)

        try:
            response = requests.get(url, **params)
        except requests.exceptions.RequestException as error:
            raise WaldurClientException(str(error))

        if response.status_code != 200:
            error = self._parse_error(response)
            raise WaldurClientException(error)
        result = response.json()
        if 'Link' not in response.headers:
            return result
        while 'next' in response.headers['Link']:
            if 'prev' in response.headers['Link']:
                next_url = response.headers['Link'].split(', ')[2].split('; ')[0][1:-1]
            else:  # First page case
                next_url = response.headers['Link'].split(', ')[1].split('; ')[0][1:-1]
            try:
                response = requests.get(next_url, **params)
            except requests.exceptions.RequestException as error:
                raise WaldurClientException(str(error))

            if response.status_code != 200:
                error = self._parse_error(response)
                raise WaldurClientException(error)

            result += response.json()

        return result

    def _get_count(self, url, **kwargs):
        response = self._head(url, **kwargs)
        return int(response.headers['X-Result-Count'])

    def _get(self, url, valid_states, **kwargs):
        return self._make_request('get', url, valid_states, 1, **kwargs)

    def _head(self, url, **kwargs):
        return self._make_request('head', url, valid_states=[200], **kwargs)

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
        if query_params is None:
            query_params = {}
        query_params.setdefault('page_size', 200)
        return self._get_all(url, params=query_params)

    def _get_resource(self, endpoint, value, extra=None):
        """
        Get resource by UUID, name or query parameters.

        :param endpoint: WaldurClient.Endpoint attribute.
        :param value: name or uuid of the resource
        :return: a resource as a dictionary.
        :raises: WaldurClientException if resource could not be received or response failed.
        """
        if not value:
            raise WaldurClientException('Empty ID is not allowed.')
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

    def _get_service_settings(self, identifier):
        return self._get_resource(self.Endpoints.Provider, identifier)

    def _get_project(self, identifier):
        return self._get_resource(self.Endpoints.Project, identifier)

    def get_user(self, identifier):
        return self._get_resource(self.Endpoints.Users, identifier)

    def list_users(self):
        url = self._build_url(self.Endpoints.Users)
        return self._get_all(url)

    def count_users(self, **kwargs):
        url = self._build_url(self.Endpoints.Users)
        return self._get_count(url, **kwargs)

    def count_customer_permissions(self, **kwargs):
        url = self._build_url(self.Endpoints.CustomerPermissions)
        return self._get_count(url, **kwargs)

    def list_ssh_keys(self):
        url = self._build_url(self.Endpoints.SshKey)
        return self._get_all(url)

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
        query = {'tenant_uuid': tenant_uuid}
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

    def list_tenants(self, filters=None):
        endpoint = self._build_url(self.Endpoints.Tenant)
        return self._query_resource_list(endpoint, filters)

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

    def get_marketplace_resource(self, resource_uuid):
        return self._get_resource(
            WaldurClient.Endpoints.MarketplaceResources, resource_uuid
        )

    def list_marketplace_resources(
        self,
        provider_uuid: str = None,
        state: str = None,
        offering_uuid: str = None,
        fields: List[str] = None,
    ):
        params = {}
        if provider_uuid is not None:
            params['provider_uuid'] = provider_uuid
        if state is not None:
            params['state'] = state
        if offering_uuid is not None:
            params['offering_uuid'] = offering_uuid
        if fields is not None:
            if type(fields) is not list:
                fields = [fields]
            params['field'] = fields

        return self._query_resource_list(self.Endpoints.MarketplaceResources, params,)

    def count_marketplace_resources(self, **kwargs):
        url = self._build_url(self.Endpoints.MarketplaceResources)
        return self._get_count(url, **kwargs)

    def marketplace_resource_set_backend_id(self, resource_uuid: str, backend_id: str):
        url = self._build_resource_url(
            self.Endpoints.MarketplaceResources, resource_uuid, action='set_backend_id',
        )
        payload = {'backend_id': backend_id}
        return self._post(url, valid_states=[200], json=payload)

    def marketplace_resource_submit_report(
        self, resource_uuid: str, report: List[ResourceReportRecord]
    ):
        url = self._build_resource_url(
            self.Endpoints.MarketplaceResources, resource_uuid, action='submit_report'
        )
        payload = {'report': [dataclasses.asdict(record) for record in report]}
        return self._post(url, valid_states=[200], json=payload)

    def marketplace_resource_get_team(self, resource_uuid: str):
        url = self._build_resource_url(
            self.Endpoints.MarketplaceResources, resource_uuid, action='team'
        )
        return self._get(url, valid_states=[200])

    def marketplace_resource_get_plan_periods(self, resource_uuid: str):
        url = self._build_resource_url(
            self.Endpoints.MarketplaceResources, resource_uuid, action='plan_periods'
        )
        return self._get(url, valid_states=[200])

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
                {'project_uuid': project_uuid, 'state': ['Active', 'Paused']},
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
        project_uuid = self._get_project(project)['uuid']
        offering_uuid = self._get_offering(offering, project)['uuid']
        plan_uuid = plan and self._get_plan(plan)['uuid']
        return self.marketplace_resource_create_order(
            project_uuid, offering_uuid, plan_uuid, attributes, limits
        )

    def get_order(self, order_uuid):
        return self._get_resource(WaldurClient.Endpoints.MarketplaceOrder, order_uuid)

    def list_orders(self, filters=None):
        return self._query_resource_list(self.Endpoints.MarketplaceOrder, filters)

    def get_order_item(self, order_item_uuid):
        return self._get_resource(
            WaldurClient.Endpoints.MarketplaceOrderItem, order_item_uuid
        )

    def list_order_items(self, filters=None):
        return self._query_resource_list(self.Endpoints.MarketplaceOrderItem, filters)

    def marketplace_order_item_approve(self, order_item_uuid: str):
        url = self._build_resource_url(
            self.Endpoints.MarketplaceOrderItem, order_item_uuid, action='approve',
        )
        return self._post(url, valid_states=[200])

    def marketplace_order_item_reject(self, order_item_uuid: str):
        url = self._build_resource_url(
            self.Endpoints.MarketplaceOrderItem, order_item_uuid, action='reject',
        )
        return self._post(url, valid_states=[200])

    def _get_resource_from_creation_order(
        self, order_uuid, resource_field='resource_uuid', interval=10, timeout=600,
    ):
        waited = 0
        while True:
            order = self.get_order(order_uuid)
            if order['items'][0]['state'] == 'erred':
                raise InvalidStateError(order['items'][0]['error_message'])

            resource_uuid = order['items'][0].get(resource_field)
            if resource_uuid:
                return resource_uuid
            time.sleep(interval)

            waited += interval
            if waited >= timeout:
                error = (
                    'Resource reference has not been found from order item "%s" '
                    % order_uuid
                )
                message = '%s. Seconds passed: %s' % (error, timeout)
                raise TimeoutError(message)

    def _create_scope_via_marketplace(
        self,
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

        :param offering: the name or UUID of marketplace offering.
        :param project: the name or UUID of the project.
        :param attributes: order item attributes.
        :param scope_endpoint: scope endpoint.
        :param interval: interval of instance state polling in seconds.
        :param timeout: a maximum amount of time to wait for instance provisioning.
        :param wait: defines whether the client has to wait for instance provisioning.
        :param check_mode: True for check mode.
        :return: resource_uuid.
        """
        offering = self._get_offering(offering, project)

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

        resource_uuid = self._get_resource_from_creation_order(order_uuid)

        if wait:
            self._wait_for_resource(scope_endpoint, resource_uuid, interval, timeout)

        return resource_uuid

    def create_resource_via_marketplace(
        self, project_uuid, offering_uuid, plan_uuid, attributes, limits
    ):
        order = self.create_marketplace_order(
            project_uuid, offering_uuid, plan_uuid, attributes, limits
        )
        order_uuid = order['uuid']
        marketplace_resource_uuid = self._get_resource_from_creation_order(
            order_uuid, 'marketplace_resource_uuid'
        )
        return {
            'create_order_uuid': order_uuid,
            'marketplace_resource_uuid': marketplace_resource_uuid,
        }

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
        :param offering: the name or UUID of the offering
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

        resource_uuid = self._create_scope_via_marketplace(
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
            self._wait_for_external_ip(resource_uuid, interval, timeout)

        return resource_uuid

    def _delete_scope_via_marketplace(self, scope_uuid, offering_type, options=None):
        resource, scope = self.get_marketplace_resource_scope(scope_uuid, offering_type)
        return self.marketplace_resource_terminate_order(resource['uuid'], options)

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
        :param offering: the name or UUID of the offering
        :param size: size of the volume in GBs.
        :param volume_type: uuid or name of volume type.
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

    def list_customers(self, filters=None):
        return self._query_resource_list(self.Endpoints.Customers, filters)

    def count_customers(self):
        url = self._build_url(self.Endpoints.Customers)
        return self._get_count(url)

    def create_customer(
        self,
        name,
        email='',
        address='',
        registration_code='',
        backend_id='',
        abbreviation='',
        bank_account='',
        bank_name='',
        contact_details='',
        country='',
        display_name='',
        domain='',
        homepage='',
        native_name='',
        latitude=None,
        longitude=None,
        phone_number='',
        postal='',
        vat_code='',
    ):
        payload = {
            'abbreviation': abbreviation,
            'address': address,
            'bank_account': bank_account,
            'bank_name': bank_name,
            'contact_details': contact_details,
            'country': country,
            'display_name': display_name,
            'domain': domain,
            'email': email,
            'homepage': homepage,
            'name': name,
            'native_name': native_name,
            'registration_code': registration_code,
            'backend_id': backend_id,
            'latitude': latitude,
            'longitude': longitude,
            'phone_number': phone_number,
            'postal': postal,
            'vat_code': vat_code,
        }
        return self._create_resource(self.Endpoints.Customers, payload=payload)

    def delete_customer(self, customer):
        """
        Delete a customer by UUID or URL

        :param customer: customer's UUID or URL
        :return: deleted customer information
        """
        if is_uuid(customer):
            return self._delete_resource(self.Endpoints.Customers, customer)
        return self._delete_resource_by_url(customer)

    def list_projects(self, filters=None):
        return self._query_resource_list(self.Endpoints.Project, filters)

    def count_projects(self):
        url = self._build_url(self.Endpoints.Project)
        return self._get_count(url)

    def _serialize_project(
        self,
        name=None,
        backend_id=None,
        description='',
        end_date=None,
        oecd_fos_2007_code=None,
        type_uuid=None,
    ):
        type_url = type_uuid and self._build_resource_url(
            self.Endpoints.ProjectTypes, type_uuid
        )
        return {
            'name': name,
            'backend_id': backend_id,
            'description': description,
            'end_date': end_date,
            'oecd_fos_2007_code': oecd_fos_2007_code,
            'type': type_url,
        }

    def create_project(self, customer_uuid, name, **kwargs):
        payload = self._serialize_project(name=name, **kwargs)
        payload['customer'] = self._build_resource_url(
            self.Endpoints.Customers, customer_uuid
        )

        return self._create_resource(self.Endpoints.Project, payload=payload)

    def update_project(self, project_uuid, **kwargs):
        payload = self._serialize_project(**kwargs)
        return self._update_resource(
            self.Endpoints.Project, project_uuid, payload=payload
        )

    def delete_project(self, project):
        """
        Delete a project by UUID or URL

        :param project: project's UUID or URL
        :return: deleted project information
        """
        if is_uuid(project):
            return self._delete_resource(self.Endpoints.Project, project)
        return self._delete_resource_by_url(project)

    def list_marketplace_offerings(self, filters=None):
        return self._query_resource_list(self.Endpoints.MarketplaceOffering, filters)

    def get_marketplace_offering(self, offering_uuid):
        return self._query_resource_by_uuid(
            self.Endpoints.MarketplaceOffering, offering_uuid
        )

    def marketplace_resource_create_order(
        self, project_uuid, offering_uuid, plan_uuid=None, attributes=None, limits=None
    ):
        attributes = attributes or {}
        limits = limits or {}
        order_item = {
            'offering': self._build_resource_url(
                self.Endpoints.MarketplaceOffering, offering_uuid
            ),
            'attributes': attributes,
            'limits': limits,
        }

        if plan_uuid:
            order_item['plan'] = self._build_resource_url(
                self.Endpoints.MarketplacePlan, plan_uuid
            )

        # TODO: replace with checkbox data from frontend
        order_item['accepting_terms_of_service'] = True

        payload = {
            'project': self._build_resource_url(self.Endpoints.Project, project_uuid),
            'items': [order_item],
        }
        return self._create_resource(self.Endpoints.MarketplaceOrder, payload=payload)

    def marketplace_resource_update_limits_order(self, resource_uuid, limits):
        payload = {'limits': limits}
        url = self._build_resource_url(
            self.Endpoints.MarketplaceResources, resource_uuid, action='update_limits'
        )
        return self._post(url, valid_states=[200], json=payload)['order_uuid']

    def marketplace_resource_terminate_order(self, resource_uuid, options=None):
        if options:
            options = {'attributes': options}
        url = self._build_resource_url(
            self.Endpoints.MarketplaceResources, resource_uuid, action='terminate'
        )
        return self._post(url, valid_states=[200], json=options)['order_uuid']

    def get_invoice_for_customer(self, customer_uuid, year, month):
        return self._query_resource(
            self.Endpoints.Invoice,
            {'customer_uuid': customer_uuid, 'year': year, 'month': month},
        )

    def invoice_set_backend_id(self, invoice_uuid: str, backend_id: str):
        url = self._build_resource_url(
            self.Endpoints.Invoice, invoice_uuid, action='set_backend_id',
        )
        payload = {'backend_id': backend_id}
        return self._post(url, valid_states=[200], json=payload)

    def invoice_set_payment_url(self, invoice_uuid: str, payment_url: str):
        url = self._build_resource_url(
            self.Endpoints.Invoice, invoice_uuid, action='set_payment_url',
        )
        payload = {'payment_url': payment_url}
        return self._post(url, valid_states=[200], json=payload)

    def invoice_set_reference_number(self, invoice_uuid: str, reference_number: str):
        url = self._build_resource_url(
            self.Endpoints.Invoice, invoice_uuid, action='set_reference_number',
        )
        payload = {'reference_number': reference_number}
        return self._post(url, valid_states=[200], json=payload)

    def list_payment_profiles(self, filters=None):
        if 'payment_type' in filters:
            filters['payment_type'] = filters['payment_type'].value
        return self._query_resource_list(self.Endpoints.PaymentProfiles, filters)

    def list_component_usages(self, resource_uuid, date_after=None, date_before=None):
        return self._query_resource_list(
            self.Endpoints.ComponentUsage,
            {
                'resource_uuid': resource_uuid,
                'date_after': date_after,
                'date_before': date_before,
            },
        )

    def create_component_usages(
        self, plan_period_uuid: str, usages: List[ComponentUsage]
    ):
        url = self._build_url(f'{self.Endpoints.ComponentUsage}/set_usage/')
        payload = {
            'plan_period': plan_period_uuid,
            'usages': [dataclasses.asdict(usage) for usage in usages],
        }
        return self._post(url, valid_states=[201], json=payload)

    def get_remote_eduteams_user(self, cuid):
        return self._create_resource(
            self.Endpoints.RemoteEduteams, {'cuid': cuid,}, valid_state=200,
        )

    def create_project_permission(
        self, user_uuid, project_uuid, role, expiration_time=None
    ):
        return self._create_resource(
            self.Endpoints.ProjectPermissions,
            {
                'user': self._build_resource_url(self.Endpoints.Users, user_uuid),
                'project': self._build_resource_url(
                    self.Endpoints.Project, project_uuid
                ),
                'role': role,
                'expiration_time': expiration_time,
            },
        )

    def get_project_permissions(self, project_uuid, user_uuid=None, role=None):
        query_params = {
            'project': project_uuid,
        }
        if role:
            query_params['role'] = role
        if user_uuid:
            query_params['user'] = user_uuid

        return self._query_resource_list(
            self.Endpoints.ProjectPermissions, query_params
        )

    def list_project_permissions(self, filters=None):
        return self._query_resource_list(self.Endpoints.ProjectPermissions, filters)

    def update_project_permission(self, permission_id, new_expiration_time):
        return self._update_resource(
            self.Endpoints.ProjectPermissions,
            permission_id,
            {'expiration_time': new_expiration_time},
        )

    def remove_project_permission(self, permission_id):
        return self._delete_resource(self.Endpoints.ProjectPermissions, permission_id)

    def create_customer_permission(
        self, user_uuid, customer_uuid, role, expiration_time=None
    ):
        return self._create_resource(
            self.Endpoints.CustomerPermissions,
            {
                'user': self._build_resource_url(self.Endpoints.Users, user_uuid),
                'customer': self._build_resource_url(
                    self.Endpoints.Customers, customer_uuid
                ),
                'role': role,
                'expiration_time': expiration_time,
            },
        )

    def get_customer_permissions(self, customer_uuid, user_uuid=None, role=None):
        query_params = {
            'customer': customer_uuid,
        }
        if role:
            query_params['role'] = role
        if user_uuid:
            query_params['user'] = (user_uuid,)

        return self._query_resource_list(
            self.Endpoints.CustomerPermissions, query_params
        )

    def list_customer_permissions(self, filters=None):
        return self._query_resource_list(self.Endpoints.CustomerPermissions, filters)

    def update_customer_permission(self, permission_id, new_expiration_time):
        return self._update_resource(
            self.Endpoints.CustomerPermissions,
            permission_id,
            {'expiration_time': new_expiration_time},
        )

    def remove_customer_permission(self, permission_id):
        return self._delete_resource(self.Endpoints.CustomerPermissions, permission_id)

    def create_offering_permission(self, user_uuid, offering_uuid):
        return self._create_resource(
            self.Endpoints.OfferingPermissions,
            {
                'user': self._build_resource_url(self.Endpoints.Users, user_uuid),
                'offering': self._build_resource_url(
                    self.Endpoints.MarketplaceOffering, offering_uuid
                ),
            },
        )

    def get_offering_permissions(self, offering_uuid, user_uuid=None):
        query_params = {
            'offering_uuid': offering_uuid,
        }
        if user_uuid:
            query_params['user'] = user_uuid

        return self._query_resource_list(
            self.Endpoints.OfferingPermissions, query_params,
        )

    def remove_offering_permission(self, permission_id):
        return self._delete_resource(self.Endpoints.OfferingPermissions, permission_id)

    def create_project_invitation(
        self, email: str, project: str, project_role: ProjectRole
    ):
        if is_uuid(project):
            project = self._build_resource_url(self.Endpoints.Project, project)

        payload = {
            'email': email,
            'project': project,
            'project_role': project_role.value,
        }

        return self._create_resource(self.Endpoints.UserInvitations, payload)

    def create_remote_offering_user(
        self, offering: str, user: str, username: str = None
    ):
        if is_uuid(offering):
            offering = self._build_resource_url(
                self.Endpoints.MarketplaceOffering, offering
            )

        if is_uuid(user):
            user = self._build_resource_url(self.Endpoints.Users, user)

        payload = {
            'offering': offering,
            'user': user,
        }

        if username is not None:
            payload['username'] = username

        return self._create_resource(self.Endpoints.OfferingUsers, payload)

    def set_offerings_username(
        self, service_provider_uuid: str, user_uuid: str, username: str
    ):
        endpoint = self._build_resource_url(
            self.Endpoints.ServiceProviders,
            service_provider_uuid,
            'set_offerings_username',
        )
        payload = {
            'user_uuid': user_uuid,
            'username': username,
        }
        return self._post(endpoint, valid_states=[201], json=payload)

    def list_remote_offering_users(self, filters):
        return self._query_resource_list(self.Endpoints.OfferingUsers, filters)

    def list_service_providers(self, filters):
        endpoint = self._build_url(self.Endpoints.ServiceProviders)
        return self._query_resource_list(endpoint, filters)

    def list_service_provider_users(self, service_provider_uuid):
        endpoint = self._build_resource_url(
            self.Endpoints.ServiceProviders, service_provider_uuid, 'users'
        )
        return self._query_resource_list(endpoint, None)

    def list_service_provider_projects(self, service_provider_uuid):
        endpoint = self._build_resource_url(
            self.Endpoints.ServiceProviders, service_provider_uuid, 'projects'
        )
        return self._query_resource_list(endpoint, None)

    def list_service_provider_project_permissions(self, service_provider_uuid):
        endpoint = self._build_resource_url(
            self.Endpoints.ServiceProviders,
            service_provider_uuid,
            'project_permissions',
        )
        return self._query_resource_list(endpoint, None)

    def list_service_provider_ssh_keys(self, service_provider_uuid):
        endpoint = self._build_resource_url(
            self.Endpoints.ServiceProviders, service_provider_uuid, 'keys'
        )
        return self._query_resource_list(endpoint, None)


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
