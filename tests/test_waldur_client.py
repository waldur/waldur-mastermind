import json
import unittest
import uuid

import responses
from six.moves.urllib.parse import parse_qs, urlencode, urlparse

from waldur_client import WaldurClient, WaldurClientException


class BaseWaldurClientTest(unittest.TestCase):
    def setUp(self):
        self.api_url = 'http://example.com:8000/api'
        self.access_token = 'token'
        self.client = WaldurClient(self.api_url, self.access_token)
        self.tenant = {'name': 'tenant', 'uuid': str(uuid.uuid4())}

    def _get_url(self, endpoint, params=None):
        url = '%(url)s/%(endpoint)s/'
        url = url % {
            'url': self.api_url,
            'endpoint': endpoint,
        }
        return '%s?%s' % (url, urlencode(params)) if params else url

    def _get_resource_url(self, endpoint, uuid):
        return '%s%s' % (self._get_url(endpoint), uuid)

    def _get_subresource_url(self, endpoint, uuid, action=None):
        url = self._get_resource_url(endpoint, uuid)
        return '%s/%s/' % (url, action) if action else url

    def _get_object(self, name):
        return {
            'url': 'url_%s' % name,
            'uuid': 'uuid_%s' % name,
            'name': self.params[name] if name in self.params else name,
        }


class InstanceCreateBaseTest(BaseWaldurClientTest):
    def setUp(self):
        super(InstanceCreateBaseTest, self).setUp()

        self.params = {
            'name': 'instance',
            'project': 'project',
            'networks': [
                {
                    'subnet': 'subnet',
                    'floating_ip': 'auto',
                }
            ],
            'security_groups': ['web'],
            'flavor': 'flavor',
            'image': 'image',
            'ssh_key': 'ssh_key',
            'wait': True,
            'timeout': 600,
            'interval': 0.1,
            'user_data': 'user_data',
            'system_volume_size': 10,
            'data_volume_size': 5,
        }

        self.flavor = {
            'url': 'url_flavor',
            'uuid': 'uuid',
            'name': 'g1.small1',
            'settings': 'url_settings',
            'cores': 1,
            'ram': 512,
            'disk': 10240,
        }

        self.instance = {
            'uuid': 'uuid',
            'name': self.params['name'],
            'url': 'url_instance',
            'state': 'OK',
            'external_ips': ['142.124.1.50'],
        }

        post_url = '%s/openstacktenant-instances/' % self.api_url
        mapping = {
            'project': 'projects',
            'image': 'openstacktenant-images',
            'subnet': 'openstacktenant-subnets',
            'security_groups': 'openstacktenant-security-groups',
            'ssh_key': 'keys',
        }
        for name in mapping:
            obj = self._get_object(name)
            responses.add(responses.GET, self._get_url(mapping[name]), json=[obj])

        service_project_link = self._get_object('service_project_link')
        responses.add(
            responses.GET,
            self._get_url('openstacktenant-service-project-link'),
            json=[service_project_link],
        )
        security_group = self._get_object('security_group')
        responses.add(
            responses.GET,
            self._get_url('openstacktenant-security-groups'),
            json=[security_group],
        )
        responses.add(responses.POST, post_url, json=self.instance, status=201)
        status_url = self._get_url('openstacktenant-instances')
        responses.add(responses.GET, status_url, json=[self.instance])

        self.instance_url = '%s/openstacktenant-instances/%s/' % (
            self.api_url,
            self.instance['uuid'],
        )
        responses.add(responses.GET, self.instance_url, json=self.instance)

        url = self._get_url(
            'openstacktenant-flavors',
            {'settings_uuid': u'settings_uuid', 'name_exact': 'flavor'},
        )
        responses.add(method='GET', url=url, json=[self.flavor], match_querystring=True)


class InstanceCreateTest(InstanceCreateBaseTest):
    def setUp(self):
        super(InstanceCreateTest, self).setUp()
        self.params['provider'] = 'provider'
        provider = self._get_object('provider')
        provider['settings'] = 'settings_url'
        provider['settings_uuid'] = 'settings_uuid'
        responses.add(responses.GET, self._get_url('openstacktenant'), json=[provider])

    @responses.activate
    def test_valid_body_is_sent(self):
        actual = self.create_instance()
        self.assertEqual(
            actual,
            {
                'data_volume_size': 5120,
                'flavor': 'url_flavor',
                'floating_ips': [{'subnet': 'url_subnet'}],
                'image': 'url_image',
                'internal_ips_set': [{'subnet': 'url_subnet'}],
                'name': 'instance',
                'security_groups': [{'url': 'url_security_groups'}],
                'service_settings': 'settings_url',
                'project': 'url_project',
                'ssh_public_key': 'url_ssh_key',
                'system_volume_size': 10240,
                'user_data': 'user_data',
            },
        )

    @responses.activate
    def test_flavors_are_filtered_by_ram_and_cpu(self):
        url = self._get_url(
            'openstacktenant-flavors',
            {'ram__gte': 2000, 'cores__gte': 2, 'o': 'cores,ram,disk'},
        )
        responses.add(
            method='GET',
            url=url,
            json=[self.flavor, self.flavor, self.flavor],
            match_querystring=True,
        )

        self.params.pop('flavor')
        self.params['flavor_min_cpu'] = 2
        self.params['flavor_min_ram'] = 2000

        actual = self.create_instance()
        self.assertEqual(actual['flavor'], self.flavor['url'])

    @responses.activate
    def test_if_networks_do_no_have_a_subnet_error_is_raised(self):
        del self.params['networks'][0]['subnet']

        self.assertRaises(WaldurClientException, self.create_instance)

    @responses.activate
    def test_wait_for_floating_ip(self):
        self.create_instance()
        self.assertEqual(
            2,
            len(
                [
                    call
                    for call in responses.calls
                    if call.request.url == self.instance_url
                ]
            ),
        )

    @responses.activate
    def test_skip_floating_ip(self):
        del self.instance['external_ips']
        del self.params['networks'][0]['floating_ip']

        self.create_instance()
        self.assertEqual(
            1,
            len(
                [
                    call
                    for call in responses.calls
                    if call.request.url == self.instance_url
                ]
            ),
        )

    def create_instance(self):
        self.client.create_instance(**self.params)
        post_request = [
            call.request for call in responses.calls if call.request.method == 'POST'
        ][0]
        return json.loads(post_request.body.decode('utf-8'))


class InstanceCreateViaMarketplaceTest(InstanceCreateBaseTest):
    def setUp(self):
        super(InstanceCreateViaMarketplaceTest, self).setUp()

        self.params['offering'] = 'offering'

        offering = self._get_object('offering')
        offering['scope_uuid'] = 'settings_uuid'
        offering['type'] = 'OpenStackTenant.Instance'
        responses.add(
            responses.GET, self._get_url('marketplace-offerings'), json=[offering]
        )

        self.order = {
            'uuid': '9ae5e13294884628aaf984a82214f7c4',
            'items': [{'state': 'executing'}],
        }

        url = self._get_url('marketplace-orders')
        responses.add(responses.POST, url, json=self.order, status=201)

        url = self._get_url('marketplace-orders/%s' % self.order['uuid'])
        responses.add(responses.GET, url, json=self.order, status=200)

        url = self._get_url('marketplace-orders/order_uuid/approve')
        responses.add(responses.POST, url, json=self.order, status=200)

        url = self._get_url('marketplace-resources')
        scope_url = self._get_url('scope_url')
        responses.add(responses.GET, url, json=[{'scope': scope_url}], status=200)

        responses.add(
            responses.GET,
            scope_url,
            json={'name': 'instance', 'uuid': 'uuid'},
            status=200,
        )

    @responses.activate
    def test_valid_body_is_sent(self):
        actual = self.create_instance()
        self.assertEqual(
            actual,
            {
                'project': 'url_project',
                'items': [
                    {
                        'accepting_terms_of_service': True,
                        'attributes': {
                            'name': 'instance',
                            'image': 'url_image',
                            'data_volume_size': 5120,
                            'user_data': 'user_data',
                            'floating_ips': [{'subnet': 'url_subnet'}],
                            'internal_ips_set': [{'subnet': 'url_subnet'}],
                            'ssh_public_key': 'url_ssh_key',
                            'system_volume_size': 10240,
                            'flavor': 'url_flavor',
                            'security_groups': [{'url': 'url_security_groups'}],
                        },
                        'offering': 'url_offering',
                        'limits': {},
                    }
                ],
            },
        )

    @responses.activate
    def test_flavors_are_filtered_by_ram_and_cpu(self):
        url = self._get_url(
            'openstacktenant-flavors',
            {'ram__gte': 2000, 'cores__gte': 2, 'o': 'cores,ram,disk'},
        )
        responses.add(
            method='GET',
            url=url,
            json=[self.flavor, self.flavor, self.flavor],
            match_querystring=True,
        )

        self.params.pop('flavor')
        self.params['flavor_min_cpu'] = 2
        self.params['flavor_min_ram'] = 2000

        actual = self.create_instance()
        self.assertEqual(actual['items'][0]['attributes']['flavor'], self.flavor['url'])

    @responses.activate
    def test_if_networks_do_no_have_a_subnet_error_is_raised(self):
        del self.params['networks'][0]['subnet']

        self.assertRaises(WaldurClientException, self.create_instance)

    @responses.activate
    def test_wait_for_floating_ip(self):
        self.create_instance()
        self.assertEqual(
            2,
            len(
                [
                    call
                    for call in responses.calls
                    if call.request.url == self.instance_url
                ]
            ),
        )

    @responses.activate
    def test_skip_floating_ip(self):
        del self.instance['external_ips']
        del self.params['networks'][0]['floating_ip']

        self.create_instance()
        self.assertEqual(
            1,
            len(
                [
                    call
                    for call in responses.calls
                    if call.request.url == self.instance_url
                ]
            ),
        )

    @responses.activate
    def test_raise_exception_if_order_item_state_is_erred(self):
        self.order['items'][0]['state'] = 'erred'
        self.order['items'][0]['error_message'] = 'error message'
        url = self._get_url('marketplace-orders/%s' % self.order['uuid'])
        responses.replace(responses.GET, url, json=self.order, status=200)
        self.assertRaises(WaldurClientException, self.create_instance)

    def create_instance(self):
        self.client.create_instance_via_marketplace(**self.params)
        post_request = [
            call.request for call in responses.calls if call.request.method == 'POST'
        ][0]
        return json.loads(post_request.body.decode('utf-8'))


class InstanceDeleteTest(BaseWaldurClientTest):
    def setUp(self):
        super(InstanceDeleteTest, self).setUp()
        self.expected_url = (
            'http://example.com:8000/api/openstacktenant-instances/'
            '6b6e60870ad64085aadcdcbc1fd84a7e/?'
            'delete_volumes=True&release_floating_ips=True'
        )

    @responses.activate
    def test_deletion_parameters_are_passed_as_query_parameters(self):
        responses.add(
            responses.DELETE,
            self.expected_url,
            status=204,
            json={'details': 'Instance has been deleted.'},
        )
        self.client.delete_instance('6b6e60870ad64085aadcdcbc1fd84a7e')
        expected = {
            'delete_volumes': ['True'],
            'release_floating_ips': ['True'],
        }
        request = urlparse(responses.calls[0].request.url)
        self.assertEqual(expected, parse_qs(request.query))

    @responses.activate
    def test_error_is_raised_if_invalid_status_code_is_returned(self):
        responses.add(
            responses.DELETE,
            self.expected_url,
            status=400,
            json={'details': 'Instance has invalid state.'},
        )
        self.assertRaises(
            WaldurClientException,
            self.client.delete_instance,
            '6b6e60870ad64085aadcdcbc1fd84a7e',
        )


class InstanceDeleteViaMarketplaceTest(BaseWaldurClientTest):
    def setUp(self):
        super(InstanceDeleteViaMarketplaceTest, self).setUp()
        url = self._get_url('marketplace-resources')
        scope_url = self._get_url('scope_url')
        responses.add(
            responses.GET,
            url,
            json=[{'scope': scope_url, 'uuid': 'resource_uuid'}],
            status=200,
        )
        responses.add(
            responses.GET,
            scope_url,
            json={'name': 'instance', 'uuid': '6b6e60870ad64085aadcdcbc1fd84a7e'},
            status=200,
        )
        url = self._get_url('marketplace-resources/resource_uuid/terminate')
        responses.add(
            responses.POST, url, json={'order_uuid': 'order_uuid'}, status=200
        )

    @responses.activate
    def test_deletion_parameters_are_passed_as_query_parameters(self):
        self.client.delete_instance_via_marketplace('6b6e60870ad64085aadcdcbc1fd84a7e')
        self.assertEqual(
            [c.request.url for c in responses.calls if c.request.method == 'POST'][0],
            'http://example.com:8000/api/marketplace-resources/resource_uuid/terminate/',
        )

    @responses.activate
    def test_pass_delete_options_to_api(self):
        self.client.delete_instance_via_marketplace(
            '6b6e60870ad64085aadcdcbc1fd84a7e', release_floating_ips=False
        )
        self.assertEqual(
            [
                json.loads(c.request.body)
                for c in responses.calls
                if c.request.method == 'POST'
            ][0],
            {"attributes": {"release_floating_ips": False}},
        )

    @responses.activate
    def test_error_is_raised_if_invalid_status_code_is_returned(self):
        self.assertRaises(
            WaldurClientException,
            self.client.delete_instance,
            '6b6e60870ad64085aadcdcbc1fd84a7e',
        )


class InstanceStopTest(BaseWaldurClientTest):
    def setUp(self):
        super(InstanceStopTest, self).setUp()
        self.expected_url = (
            'http://example.com:8000/api/openstacktenant-instances/'
            '6b6e60870ad64085aadcdcbc1fd84a7e/stop/'
        )

    @responses.activate
    def test_valid_url_is_rendered_for_action(self):
        responses.add(
            responses.POST,
            self.expected_url,
            status=202,
            json={'details': 'Instance stop has been scheduled.'},
        )
        self.client.stop_instance('6b6e60870ad64085aadcdcbc1fd84a7e', wait=False)


class SecurityGroupTest(BaseWaldurClientTest):
    security_group = {
        'name': 'secure group',
        'uuid': '59e46d029a79473779915a22',
        'state': 'OK',
        'rules': [
            {
                'to_port': 10,
                'from_port': 20,
                'cidr': '0.0.0.0/24',
                'protocol': 'tcp',
            }
        ],
    }

    @responses.activate
    def test_waldur_client_returns_security_group_by_tenant_name_and_security_group_name(
        self,
    ):
        security_group = dict(name='security_group')
        params = dict(
            name_exact=security_group['name'], tenant_uuid=self.tenant['uuid']
        )
        get_url = self._get_url('openstack-security-groups', params)
        responses.add(
            responses.GET, get_url, json=[security_group], match_querystring=True
        )
        responses.add(
            responses.GET, self._get_url('openstack-tenants'), json=[self.tenant]
        )

        response = self.client.get_security_group(
            self.tenant['name'], security_group['name']
        )

        self.assertEqual(response['name'], security_group['name'])

    def create_security_group(self, **kwargs):
        action_name = 'create_security_group'
        responses.add(
            responses.GET, self._get_url('openstack-tenants'), json=[self.tenant]
        )
        post_url = self._get_subresource_url(
            'openstack-tenants', self.tenant['uuid'], action_name
        )
        responses.add(responses.POST, post_url, json=self.security_group, status=201)

        instance_url = '%s/openstack-security-groups/%s/' % (
            self.api_url,
            self.security_group['uuid'],
        )
        responses.add(responses.GET, instance_url, json=self.security_group, status=200)

        client = WaldurClient(self.api_url, self.access_token)
        response = client.create_security_group(
            tenant=self.tenant['name'],
            name=self.security_group['name'],
            rules=self.security_group['rules'],
            **kwargs
        )
        return response

    @responses.activate
    def test_waldur_client_creates_security_group_with_passed_parameters(self):
        response = self.create_security_group()
        self.assertEqual(self.security_group['name'], response['name'])
        self.assertEqual(self.security_group['rules'], response['rules'])

    @responses.activate
    def test_search_tenant_by_project_name(self):
        project = {
            'uuid': str(uuid.uuid4()),
        }
        responses.add(responses.GET, self._get_url('projects'), json=[project])

        self.create_security_group(project='waldur')

        url = [
            call.request.url for call in responses.calls if call.request.method == 'GET'
        ][0]
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params['name_exact'][0], 'waldur')

        url = [
            call.request.url for call in responses.calls if call.request.method == 'GET'
        ][1]
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params['project_uuid'][0], project['uuid'])


class VolumeDetachTest(BaseWaldurClientTest):
    def setUp(self):
        super(VolumeDetachTest, self).setUp()
        self.expected_url = (
            'http://example.com:8000/api/openstacktenant-volumes/'
            '6b6e60870ad64085aadcdcbc1fd84a7e/detach/'
        )

    @responses.activate
    def test_valid_url_is_rendered_for_action(self):
        responses.add(
            responses.POST,
            self.expected_url,
            status=202,
            json={'details': 'detach was scheduled.'},
        )
        self.client.detach_volume('6b6e60870ad64085aadcdcbc1fd84a7e', wait=False)


class VolumeAttachTest(BaseWaldurClientTest):
    def setUp(self):
        super(VolumeAttachTest, self).setUp()
        self.expected_url = (
            'http://example.com:8000/api/openstacktenant-volumes/' 'volume_uuid/attach/'
        )

    @responses.activate
    def test_valid_url_is_rendered_for_action(self):
        # Arrange
        responses.add(
            responses.POST,
            self.expected_url,
            status=202,
            json={'details': 'attach was scheduled.'},
        )

        # Act
        self.client.attach_volume(
            'volume_uuid', 'instance_uuid', '/dev/vdb', wait=False
        )

        # Assert
        actual = json.loads(responses.calls[0].request.body.decode('utf-8'))
        expected = {
            'instance': 'http://example.com:8000/api/openstacktenant-instances/instance_uuid/',
            'device': '/dev/vdb',
        }
        self.assertEqual(expected, actual)
