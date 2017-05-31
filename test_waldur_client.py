import unittest
import responses
from waldur_client import WaldurClient, WaldurClientException


class TestWaldurClient(unittest.TestCase):

    def setUp(self):
        self.params = {
            'name': 'instance',
            'api_url': 'http://example.com',
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

    def setUpResponses(self):
        post_url = '%s/api/openstacktenant-instances/' % self.params['api_url']
        mapping = {
            'provider': 'openstacktenant',
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

        service_project_link = self._get_object('service_project_link')
        responses.add(responses.GET, self._get_url('openstacktenant-service-project-link'),
                      json=[service_project_link])
        security_group = self._get_object('security_group')
        responses.add(responses.GET, self._get_url('openstacktenant-security-groups'),
                      json=[security_group])
        responses.add(responses.POST, post_url, json=self.instance, status=201)
        status_url = self._get_url('openstacktenant-instances')
        responses.add(responses.GET, status_url, json=[self.instance])

    def _get_url(self, endpoint):
        url = '%(url)s/api/%(endpoint)s/'
        return url % {
            'url': self.params['api_url'],
            'endpoint': endpoint,
        }

    def _get_object(self, name):
        return {
            'url': 'url_%s' % name,
            'uuid': 'uuid_%s' % name,
            'name': self.params[name] if name in self.params else name,
        }

    @responses.activate
    def test_waldur_client_sends_request_with_passed_parameters(self):
        self.setUpResponses()
        client = WaldurClient(self.params.pop('api_url'), self.params.pop('access_token'))
        instance = client.create_instance(**self.params)
        self.assertTrue(instance['name'], self.params['name'])

    @responses.activate
    def test_waldur_client_raises_error_if_networks_do_no_have_a_subnet(self):
        self.setUpResponses()
        client = WaldurClient(self.params.pop('api_url'), self.params.pop('access_token'))
        del self.params['networks'][0]['subnet']
        self.assertRaises(WaldurClientException, client.create_instance, **self.params)


if __name__ == '__main__':
    unittest.main()
