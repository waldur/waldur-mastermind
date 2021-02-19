import unittest
from unittest import mock

import waldur_batch_offering


@mock.patch('waldur_batch_offering.waldur_client_from_module')
@mock.patch('waldur_batch_offering.AnsibleModule')
class CreateOfferingTest(unittest.TestCase):
    def setUp(self):
        module = mock.Mock()
        module.params = {
            'api_url': 'http://example.com:8000/api',
            'access_token': 'token',
            'name': 'Test offering',
            'category': 'Category UUID',
            'plans': ['test-plan-info'],
            'batch_service': 'SLURM',
            'hostname': 'localhost',
            'username': 'user',
            'port': '8080',
            'gateway': 'localhost',
            'default_account': 'root',
        }
        module.check_mode = False
        self.module = module

    def test_create_offering(self, mock_ansible_module, mock_ansible_client):
        client = mock.Mock()
        client.create_offering.return_value = {
            'offering': [{'uuid': 'offering_uuid'}],
            'changed': True,
        }
        offering, has_changed = waldur_batch_offering.send_request_to_waldur(
            client, self.module
        )
        client.create_offering.assert_called_once()
        self.assertTrue(has_changed)
