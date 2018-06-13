import mock
import unittest

from waldur_client import ObjectDoesNotExist
import waldur_os_instance


class InstanceDeleteTest(unittest.TestCase):
    def setUp(self):
        self.module = mock.Mock()
        self.module.params = {
            'name': 'Test instance',
            'project': 'Test project',
            'state': 'absent',
        }

    def test_instance_is_deleted_if_exists(self):
        client = mock.Mock()
        client.get_instance.return_value = {
            'uuid': '59e46d029a79473779915a22',
        }

        _, has_changed = waldur_os_instance.send_request_to_waldur(client, self.module)
        client.get_instance.assert_called_once_with('Test instance', 'Test project')
        client.delete_instance.assert_called_once_with('59e46d029a79473779915a22')
        self.assertTrue(has_changed)

    def test_instance_is_not_deleted_if_it_does_not_exist(self):
        client = mock.Mock()
        client.get_instance.side_effect = ObjectDoesNotExist
        _, has_changed = waldur_os_instance.send_request_to_waldur(client, self.module)
        self.assertEqual(0, client.delete_instance.call_count)
        self.assertFalse(has_changed)


class InstanceCreateTest(unittest.TestCase):
    def setUp(self):
        module = mock.Mock()
        module.params = {
            'name': 'Test instance',
            'project': 'Test project',
            'provider': 'Test provider',
            'subnet': 'Test subnet',
            'image': 'Test image',
            'floating_ip': '1.1.1.1',
            'system_volume_size': 10,
            'state': 'present',
            'wait': True,
            'interval': 20,
            'timeout': 600,
        }
        module.check_mode = False
        self.module = module

    def test_instance_is_created_if_it_does_not_exist(self):
        client = mock.Mock()
        client.get_instance.side_effect = ObjectDoesNotExist
        _, has_changed = waldur_os_instance.send_request_to_waldur(client, self.module)
        client.create_instance.assert_called_once()
        self.assertTrue(has_changed)

    def test_instance_is_not_created_if_it_already_exists(self):
        client = mock.Mock()
        client.get_instance.return_value = {
            'uuid': '59e46d029a79473779915a22',
        }
        _, has_changed = waldur_os_instance.send_request_to_waldur(client, self.module)
        self.assertEqual(0, client.create_instance.call_count)
        self.assertFalse(has_changed)
