import unittest

import mock

import waldur_batch_allocation


@mock.patch('waldur_batch_allocation.waldur_client_from_module')
@mock.patch('waldur_batch_allocation.AnsibleModule')
class BatchAllocationMakingTest(unittest.TestCase):
    def setUp(self):
        module = mock.Mock()
        module.params = {
            'api_url': 'http://example.com:8000/api',
            'access_token': 'token',
            'offering': 'Test offering',
            'plan': 'Test plan',
            'project': 'Test project',
            'cpu_hours': 100,
            'gpu_hours': 50,
            'ram_gb': 60,
            'name': 'Alloc',
            'description': 'Sample allocation'
        }
        module.check_mode = False
        self.module = module

    def test_create_batch_allocation(self, mock_ansible_module, mock_ansible_client):
        client = mock.Mock()
        client.create_marketplace_order.return_value = {
            'items': [{'uuid': 'order_item_uuid'}]
        }
        _, has_changed = waldur_batch_allocation.send_request_to_waldur(
            client, self.module
        )
        client.create_marketplace_order.assert_called_once()
        self.assertTrue(has_changed)
