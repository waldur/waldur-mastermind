import tempfile
import unittest
from unittest import mock

import waldur_marketplace


@mock.patch('waldur_marketplace.waldur_client_from_module')
@mock.patch('waldur_marketplace.AnsibleModule')
class OrderItemCreateTest(unittest.TestCase):
    def setUp(self):
        module = mock.Mock()
        module.params = {
            'api_url': 'http://example.com:8000/api',
            'access_token': 'token',
            'offering': 'Test offering',
            'plan': 'Test plan',
            'project': 'Test project',
        }
        module.check_mode = False
        self.module = module

    def test_create_marketplace_order_item(
        self, mock_ansible_module, mock_waldur_client
    ):
        client = mock.Mock()
        client.create_marketplace_order.return_value = {
            'items': [{'uuid': 'order_item_uuid'}]
        }
        _, has_changed = waldur_marketplace.send_request_to_waldur(client, self.module)
        client.create_marketplace_order.assert_called_once()
        self.assertTrue(has_changed)

    def test_fail_json_is_called_if_file_is_not_found(
        self, mock_ansible_module, mock_waldur_client
    ):
        self.module.params['attributes'] = '/file/not/found.json'
        mock_ansible_module.return_value = self.module
        waldur_marketplace.main()
        self.module.fail_json.assert_called_once_with(
            msg="Unable to open file: [Errno 2] No such file or directory: '/file/not/found.json'"
        )

    def test_exit_json_is_called_if_file_is_found(
        self, mock_ansible_module, mock_waldur_client
    ):
        tmp_file = tempfile.NamedTemporaryFile(delete=False, mode='w')
        tmp_file.write('{"name": "my name"}')
        tmp_file.close()
        self.module.params['attributes'] = tmp_file.name
        mock_ansible_module.return_value = self.module
        waldur_marketplace.main()
        self.module.fail_json.assert_not_called()
        self.module.exit_json.assert_called_once()
