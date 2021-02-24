import unittest
from unittest import mock

import waldur_os_security_group

WEB = {
    'url': 'api/123',
    'description': 'descr',
    'rules': [
        {
            'from_port': '80',
            'to_port': '80',
            'cidr': '192.168.0.0/28',
            'protocol': 'tcp',
            'direction': 'ingress',
            'ethertype': 'IPv4',
            'remote_group': None,
        }
    ],
}

SSH = {
    'url': 'api/124',
    'description': 'descr',
    'rules': [
        {
            'from_port': '80',
            'to_port': '80',
            'remote_group': 'api/123',
            'protocol': 'tcp',
            'direction': 'ingress',
            'cidr': None,
            'ethertype': 'IPv4',
        }
    ],
}


def fail_side_effect(*args, **kwargs):
    raise Exception(kwargs['msg'])


class SecurityGroupCreateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.create_sg_call_kwargs = dict(
            project=None,
            tenant='tenant',
            name='sec-group',
            description='descr',
            rules=[],
            tags=None,
            wait=True,
            interval=20,
            timeout=600,
        )

        module = mock.Mock()
        module.params = {
            'access_token': 'token',
            'api_url': 'api',
            'tenant': 'tenant',
            'description': 'descr',
            'state': 'present',
            'name': 'sec-group',
            'wait': True,
            'interval': 20,
            'timeout': 600,
        }
        module.check_mode = False
        self.module = module

        self.client = mock.Mock()

    def check_successful_function_call(self):
        has_changed = waldur_os_security_group.send_request_to_waldur(
            self.client, self.module
        )

        self.client.create_security_group.assert_called_once_with(
            **self.create_sg_call_kwargs
        )
        self.assertTrue(has_changed)

    def check_unsuccessful_function_call(self, msg):
        self.module.fail_json.side_effect = fail_side_effect

        self.assertRaisesRegex(
            Exception,
            msg,
            waldur_os_security_group.send_request_to_waldur,
            self.client,
            self.module,
        )

    def test_group_creation_with_link_to_remote_group(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'remote_group': 'web',
                'protocol': 'tcp',
            }
        ]

        self.client.get_security_group.side_effect = [WEB, None]

        self.create_sg_call_kwargs['rules'].append(
            {
                'from_port': '80',
                'to_port': '80',
                'remote_group': 'api/123',
                'protocol': 'tcp',
                'direction': 'ingress',
            }
        )

        self.check_successful_function_call()

    def test_group_creation_erred_with_invalid_params(self):
        self.module.params['rules'] = [
            {'from_port': '80', 'to_port': '80', 'protocol': 'tcp'}
        ]

        self.module.fail_json.side_effect = fail_side_effect

        self.check_unsuccessful_function_call(
            'Either cidr or remote_group must be specified.'
        )

    def test_group_creation_with_valid_ipv4_cidr(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
            }
        ]

        self.client.get_security_group.return_value = None

        self.create_sg_call_kwargs['rules'].append(
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
                'ethertype': 'IPv4',
                'direction': 'ingress',
            }
        )

        self.check_successful_function_call()

    def test_group_creation_with_valid_ipv6_cidr(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '2002::/16',
                'protocol': 'tcp',
                'ethertype': 'IPv6',
            }
        ]

        self.client.get_security_group.return_value = None

        self.create_sg_call_kwargs['rules'].append(
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '2002::/16',
                'protocol': 'tcp',
                'ethertype': 'IPv6',
                'direction': 'ingress',
            }
        )

        self.check_successful_function_call()

    def test_group_creation_with_invalid_v6_address(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
                'ethertype': 'IPv6',
            }
        ]

        self.check_unsuccessful_function_call('Invalid IPv6 address.')

    def test_group_creation_with_invalid_v4_address(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '2002::/16',
                'protocol': 'tcp',
                'ethertype': 'IPv4',
            }
        ]

        self.check_unsuccessful_function_call('Invalid IPv4 address.')

    def test_group_creation_with_invalid_ethertype(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '2002::/16',
                'protocol': 'tcp',
                'ethertype': 'ABC',
            }
        ]

        self.check_unsuccessful_function_call('Invalid ethertype')

    def test_group_creation_with_ingress_direction(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
                'direction': 'ingress',
            }
        ]

        self.client.get_security_group.return_value = None

        self.create_sg_call_kwargs['rules'].append(
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
                'direction': 'ingress',
                'ethertype': 'IPv4',
            }
        )

        self.check_successful_function_call()

    def test_group_creation_with_egress_direction(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
                'direction': 'egress',
            }
        ]

        self.client.get_security_group.return_value = None

        self.create_sg_call_kwargs['rules'].append(
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
                'direction': 'egress',
                'ethertype': 'IPv4',
            }
        )

        self.check_successful_function_call()

    def test_group_creation_with_invalid_direction(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
                'direction': 'invalid',
            }
        ]

        self.check_unsuccessful_function_call('Invalid direction .')

    def test_group_creation_if_group_already_exists(self):
        self.module.params['name'] = 'ssh'
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'remote_group': 'web',
                'protocol': 'tcp',
                'direction': 'ingress',
            }
        ]

        self.client.get_security_group.side_effect = [WEB, SSH]

        has_changed = waldur_os_security_group.send_request_to_waldur(
            self.client, self.module
        )

        self.assertFalse(has_changed)

    def test_security_group_rules_comparison_with_cidr_positive(self):
        local_rules = [
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
                'direction': 'ingress',
                'ethertype': 'IPv4',
            }
        ]

        remote_rules = WEB['rules']

        self.assertTrue(
            waldur_os_security_group.compare_rules(local_rules, remote_rules)
        )

    def test_security_group_rules_with_remote_link_comparison_positive(self):
        local_rules = [
            {
                'from_port': '80',
                'to_port': '80',
                'remote_group': 'api/123',
                'protocol': 'tcp',
                'direction': 'ingress',
                'ethertype': 'IPv4',
            }
        ]

        remote_rules = SSH['rules']

        self.assertFalse(
            waldur_os_security_group.compare_rules(local_rules, remote_rules)
        )

    def test_security_group_rules_comparison_with_cidr_negative(self):
        local_rules = [
            {
                'from_port': '81',
                'to_port': '81',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
                'direction': 'ingress',
                'ethertype': 'IPv4',
            }
        ]

        remote_rules = WEB['rules']

        self.assertFalse(
            waldur_os_security_group.compare_rules(local_rules, remote_rules)
        )

    def test_security_group_rules_with_remote_link_comparison_negative(self):
        local_rules = [
            {
                'from_port': '80',
                'to_port': '80',
                'remote_group': 'api/124',
                'protocol': 'tcp',
                'direction': 'ingress',
                'ethertype': 'IPv4',
            }
        ]

        remote_rules = SSH['rules']

        self.assertFalse(
            waldur_os_security_group.compare_rules(local_rules, remote_rules)
        )
