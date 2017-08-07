import unittest
import uuid

import responses

try:
    from urllib import urlencode
except ImportError:
    from urllib.parse import urlencode

from waldur_client import WaldurClient, WaldurClientException


class TestWaldurClient(unittest.TestCase):

    def setUp(self):
        self.params = {
            'name': 'instance',
            'api_url': 'http://example.com:8000/api',
            'access_token': 'token',
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

        self.instance = {
            'uuid': 'uuid',
            'name': self.params['name'],
            'url': 'url_instance',
            'state': 'OK',
        }

        self.tenant = {
            'name': 'tenant',
            'uuid': str(uuid.uuid4())
        }

    def setUpInstanceCreationResponses(self):
        post_url = '%s/openstacktenant-instances/' % self.params['api_url']
        mapping = {
            'project': 'projects',
            'flavor': 'openstacktenant-flavors',
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

    def _get_url(self, endpoint, params=None):
        url = '%(url)s/%(endpoint)s/'
        url = url % {
            'url': self.params['api_url'],
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

    @responses.activate
    def test_waldur_client_sends_request_with_passed_parameters(self):
        self.setUpInstanceCreationResponses()

        access_token = self.params.pop('access_token')
        client = WaldurClient(self.params.pop('api_url'), access_token)
        instance = client.create_instance(**self.params)

        self.assertTrue(instance['name'], self.params['name'])
        self.assertEqual('token %s' % access_token,
                         responses.calls[0].request.headers['Authorization'])

    @responses.activate
    def test_waldur_client_raises_error_if_networks_do_no_have_a_subnet(self):
        self.setUpInstanceCreationResponses()

        client = WaldurClient(self.params.pop('api_url'), self.params.pop('access_token'))
        del self.params['networks'][0]['subnet']

        self.assertRaises(WaldurClientException, client.create_instance, **self.params)

    @responses.activate
    def test_waldur_client_returns_security_group_by_tenant_name_and_security_group_name(self):
        security_group = dict(name='security_group')
        params = dict(name=security_group['name'], tenant_uuid=self.tenant['uuid'])
        get_url = self._get_url('openstack-security-groups', params)
        responses.add(responses.GET, get_url, json=[security_group], match_querystring=True)
        responses.add(responses.GET, self._get_url('openstack-tenants'), json=[self.tenant])

        client = WaldurClient(self.params['api_url'], self.params['access_token'])
        response = client.get_security_group(self.tenant['name'], security_group['name'])

        self.assertEqual(response['name'], security_group['name'])

    @responses.activate
    def test_waldur_client_creates_security_group_with_passed_parameters(self):
        action_name = 'create_security_group'
        security_group = {
            'name': 'secure group',
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

        client = WaldurClient(self.params['api_url'], self.params['access_token'])
        response = client.create_security_group(
            tenant=self.tenant['name'],
            name=security_group['name'],
            rules=security_group['rules'])

        self.assertEqual(security_group['name'], response['name'])


if __name__ == '__main__':
    unittest.main()
