import json
import unittest
import uuid

import responses
from six.moves.urllib.parse import urlencode

from waldur_client import WaldurClient, WaldurClientException


class BaseWaldurClientTest(unittest.TestCase):

    def setUp(self):
        self.api_url = 'http://example.com:8000/api'
        self.access_token = 'token'
        self.client = WaldurClient(self.api_url, self.access_token)
        self.tenant = {
            'name': 'tenant',
            'uuid': str(uuid.uuid4())
        }

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


class InstanceCreateTest(BaseWaldurClientTest):

    def setUp(self):
        super(InstanceCreateTest, self).setUp()

        self.params = {
            'name': 'instance',
            'provider': 'provider',
            'project': 'project',
            'networks': [{
                'subnet': 'subnet',
                'floating_ip': 'auto',
            }],
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
            'disk': 10240
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

        provider = self._get_object('provider')
        provider['settings_uuid'] = 'settings_uuid'
        responses.add(responses.GET, self._get_url('openstacktenant'),
                      json=[provider])

        service_project_link = self._get_object('service_project_link')
        responses.add(responses.GET, self._get_url('openstacktenant-service-project-link'),
                      json=[service_project_link])
        security_group = self._get_object('security_group')
        responses.add(responses.GET, self._get_url('openstacktenant-security-groups'),
                      json=[security_group])
        responses.add(responses.POST, post_url, json=self.instance, status=201)
        status_url = self._get_url('openstacktenant-instances')
        responses.add(responses.GET, status_url, json=[self.instance])

        instance_url = '%s/openstacktenant-instances/%s/' % (self.api_url, self.instance['uuid'])
        responses.add(responses.GET, instance_url, json=self.instance)

        url = self._get_url('openstacktenant-flavors', {'ram__gte': 2000, 'cores__gte': 2, 'o': 'cores,ram,disk'})
        responses.add(
            method='GET',
            url=url,
            json=[self.flavor, self.flavor, self.flavor],
            match_querystring=True
        )

        url = self._get_url('openstacktenant-flavors', {'settings_uuid': u'settings_uuid', 'name_exact': 'flavor'})
        responses.add(
            method='GET',
            url=url,
            json=[self.flavor],
            match_querystring=True
        )

    @responses.activate
    def test_waldur_client_sends_request_with_passed_parameters(self):

        instance = self.client.create_instance(**self.params)

        self.assertTrue(instance['name'], self.params['name'])
        self.assertEqual('token %s' % self.access_token,
                         responses.calls[0].request.headers['Authorization'])

    @responses.activate
    def test_waldur_client_sends_request_with_flavor_min_cpu_and_flavor_min_ram(self):
        self.params.pop('flavor')
        self.params['flavor_min_cpu'] = 2
        self.params['flavor_min_ram'] = 2000

        instance = self.client.create_instance(**self.params)

        self.assertTrue(instance['name'], self.params['name'])
        self.assertEqual('token %s' % self.access_token,
                         responses.calls[0].request.headers['Authorization'])

    @responses.activate
    def test_waldur_client_raises_error_if_networks_do_no_have_a_subnet(self):
        del self.params['networks'][0]['subnet']

        self.assertRaises(WaldurClientException, self.client.create_instance, **self.params)


class InstanceDeleteTest(BaseWaldurClientTest):
    def setUp(self):
        super(InstanceDeleteTest, self).setUp()
        self.expected_url = 'http://example.com:8000/api/openstacktenant-instances/' \
                            '6b6e60870ad64085aadcdcbc1fd84a7e/?' \
                            'delete_volumes=True&release_floating_ips=True'

    @responses.activate
    def test_deletion_parameters_are_passed_as_query_parameters(self):
        responses.add(responses.DELETE,
                      self.expected_url,
                      status=204,
                      json={'details': 'Instance has been deleted.'})
        self.client.delete_instance('6b6e60870ad64085aadcdcbc1fd84a7e')
        self.assertEqual(self.expected_url, responses.calls[0].request.url)

    @responses.activate
    def test_error_is_raised_if_invalid_status_code_is_returned(self):
        responses.add(responses.DELETE,
                      self.expected_url,
                      status=400,
                      json={'details': 'Instance has invalid state.'})
        self.assertRaises(WaldurClientException, self.client.delete_instance, '6b6e60870ad64085aadcdcbc1fd84a7e')


class InstanceStopTest(BaseWaldurClientTest):
    def setUp(self):
        super(InstanceStopTest, self).setUp()
        self.expected_url = 'http://example.com:8000/api/openstacktenant-instances/' \
                            '6b6e60870ad64085aadcdcbc1fd84a7e/stop/'

    @responses.activate
    def test_valid_url_is_rendered_for_action(self):
        responses.add(responses.POST,
                      self.expected_url,
                      status=202,
                      json={'details': 'Instance stop has been scheduled.'})
        self.client.stop_instance('6b6e60870ad64085aadcdcbc1fd84a7e', wait=False)


class SecurityGroupTest(BaseWaldurClientTest):
    @responses.activate
    def test_waldur_client_returns_security_group_by_tenant_name_and_security_group_name(self):
        security_group = dict(name='security_group')
        params = dict(name=security_group['name'], tenant_uuid=self.tenant['uuid'])
        get_url = self._get_url('openstack-security-groups', params)
        responses.add(responses.GET, get_url, json=[security_group], match_querystring=True)
        responses.add(responses.GET, self._get_url('openstack-tenants'), json=[self.tenant])

        response = self.client.get_security_group(self.tenant['name'], security_group['name'])

        self.assertEqual(response['name'], security_group['name'])

    @responses.activate
    def test_waldur_client_creates_security_group_with_passed_parameters(self):
        action_name = 'create_security_group'
        security_group = {
            'name': 'secure group',
            'uuid': '59e46d029a79473779915a22',
            'state': 'OK',
            'rules': [{
                'to_port': 10,
                'from_port': 20,
                'cidr': '0.0.0.0/24',
                'protocol': 'tcp',
            }]
        }
        responses.add(responses.GET, self._get_url('openstack-tenants'), json=[self.tenant])
        post_url = self._get_subresource_url('openstack-tenants', self.tenant['uuid'], action_name)
        responses.add(responses.POST, post_url, json=security_group, status=201)

        instance_url = '%s/openstack-security-groups/%s/' % (self.api_url, security_group['uuid'])
        responses.add(responses.GET, instance_url, json=security_group, status=200)

        client = WaldurClient(self.api_url, self.access_token)
        response = client.create_security_group(
            tenant=self.tenant['name'],
            name=security_group['name'],
            rules=security_group['rules'])

        self.assertEqual(security_group['name'], response['name'])


class VolumeDetachTest(BaseWaldurClientTest):
    def setUp(self):
        super(VolumeDetachTest, self).setUp()
        self.expected_url = 'http://example.com:8000/api/openstacktenant-volumes/' \
                            '6b6e60870ad64085aadcdcbc1fd84a7e/detach/'

    @responses.activate
    def test_valid_url_is_rendered_for_action(self):
        responses.add(responses.POST,
                      self.expected_url,
                      status=202,
                      json={'details': 'detach was scheduled.'})
        self.client.detach_volume('6b6e60870ad64085aadcdcbc1fd84a7e', wait=False)


class VolumeAttachTest(BaseWaldurClientTest):
    def setUp(self):
        super(VolumeAttachTest, self).setUp()
        self.expected_url = 'http://example.com:8000/api/openstacktenant-volumes/' \
                            'volume_uuid/attach/'

    @responses.activate
    def test_valid_url_is_rendered_for_action(self):
        # Arrange
        responses.add(responses.POST,
                      self.expected_url,
                      status=202,
                      json={'details': 'attach was scheduled.'})

        # Act
        self.client.attach_volume('volume_uuid', 'instance_uuid', '/dev/vdb', wait=False)

        # Assert
        actual = json.loads(responses.calls[0].request.body)
        expected = {
            'instance': 'http://example.com:8000/api/openstacktenant-instances/instance_uuid/',
            'device': '/dev/vdb'
        }
        self.assertEqual(expected, actual)
