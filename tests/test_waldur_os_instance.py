import unittest

import mock

import waldur_marketplace_os_instance


class InstanceSubNetUpdateTest(unittest.TestCase):
    def setUp(self):
        module = mock.Mock()
        self.subnets_set = ['subnet_1', 'subnet_2']
        module.params = {
            'name': 'Test instance',
            'project': 'Test project',
            'subnet': self.subnets_set,
            'state': 'present',
            'wait': True,
            'interval': 20,
            'timeout': 600,
        }
        module.check_mode = False
        self.module = module

    def test_connect_marketplace_instance_to_multiple_subnets_using_network_syntax(
        self,
    ):
        client = mock.Mock()
        instance_uuid = '59e46d029a79473779915a22'
        client.get_instance_via_marketplace.return_value = {
            'uuid': instance_uuid,
            'internal_ips_set': [
                {
                    'subnet_name': 'Test subnet',
                    'subnet_uuid': '77e46d029a79473779915a22',
                }
            ],
            'security_groups': [],
        }

        module = mock.Mock()
        module.params = {
            'name': 'Test instance',
            'project': 'Test project',
            'networks': [
                {'subnet': subnet_name, 'floating_ip': 'auto'}
                for subnet_name in self.subnets_set
            ],
            'state': 'present',
            'wait': True,
            'interval': 20,
            'timeout': 600,
        }
        module.check_mode = False

        _, has_changed = waldur_marketplace_os_instance.send_request_to_waldur(
            client, module
        )
        client.update_instance_internal_ips_set.assert_called_once_with(
            instance_uuid=instance_uuid,
            subnet_set=self.subnets_set,
            interval=20,
            timeout=600,
            wait=True,
        )
        self.assertTrue(has_changed)

    def test_update_ip_of_marketplace_instance(self):
        client = mock.Mock()
        instance_uuid = '59e46d029a79473779915a22'
        client.get_instance_via_marketplace.return_value = {
            'uuid': instance_uuid,
            'internal_ips_set': [],
            'security_groups': [],
        }

        module = mock.Mock()
        module.params = {
            'name': 'Test instance',
            'project': 'Test project',
            'subnet': self.subnets_set[0],
            'floating_ip': 'auto',
            'state': 'present',
            'wait': True,
            'interval': 20,
            'timeout': 600,
        }
        module.check_mode = False

        _, has_changed = waldur_marketplace_os_instance.send_request_to_waldur(
            client, module
        )
        client.update_instance_internal_ips_set.assert_called_once_with(
            instance_uuid=instance_uuid,
            subnet_set=[self.subnets_set[0]],
            interval=20,
            timeout=600,
            wait=True,
        )
        self.assertTrue(has_changed)
