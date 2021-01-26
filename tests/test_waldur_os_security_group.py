import unittest

import mock

import waldur_os_security_group


def group_side_effect(*args, **kwargs):
    if args[1] == 'web':
        return {'url': 'api/123'}


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

        client = mock.Mock()
        client.get_security_group.side_effect = group_side_effect
        self.client = client

    def test_group_creation_with_link_to_remote_group(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'remote_group': 'web',
                'protocol': 'tcp'
            }
        ]

        self.create_sg_call_kwargs['rules'].append({
            'from_port': '80',
            'to_port': '80',
            'remote_group': 'api/123',
            'protocol': 'tcp'
        })

        has_changed = waldur_os_security_group.send_request_to_waldur(self.client,
                                                                      self.module)

        self.client.create_security_group.assert_called_once_with(
            **self.create_sg_call_kwargs
        )
        self.assertTrue(has_changed)

    def test_group_creation_erred_with_invalid_params(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'protocol': 'tcp'
            }
        ]

        self.module.fail_json.side_effect = fail_side_effect

        self.assertRaisesRegex(Exception,
                               'Either cidr or remote_group must be specified.',
                               waldur_os_security_group.send_request_to_waldur,
                               self.client, self.module, )

    def test_group_creation_with_valid_ipv4_cidr(self):
        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'cidr': '192.168.0.0/28',
                'protocol': 'tcp',
            }
        ]

        self.create_sg_call_kwargs['rules'].append({
            'from_port': '80',
            'to_port': '80',
            'cidr': '192.168.0.0/28',
            'protocol': 'tcp',
            'ethertype': 'IPv4',
        })

        has_changed = waldur_os_security_group.send_request_to_waldur(self.client,
                                                                      self.module)

        self.client.create_security_group.assert_called_once_with(
            **self.create_sg_call_kwargs
        )
        self.assertTrue(has_changed)

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

        self.create_sg_call_kwargs['rules'].append({
            'from_port': '80',
            'to_port': '80',
            'cidr': '2002::/16',
            'protocol': 'tcp',
            'ethertype': 'IPv6',
        })

        has_changed = waldur_os_security_group.send_request_to_waldur(self.client,
                                                                      self.module)

        self.client.create_security_group.assert_called_once_with(
            **self.create_sg_call_kwargs
        )
        self.assertTrue(has_changed)

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

        self.module.fail_json.side_effect = fail_side_effect

        self.assertRaisesRegex(Exception,
                               r'Invalid IPv6 address.',
                               waldur_os_security_group.send_request_to_waldur,
                               self.client, self.module)

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

        self.module.fail_json.side_effect = fail_side_effect

        self.assertRaisesRegex(Exception,
                               r'Invalid IPv4 address.',
                               waldur_os_security_group.send_request_to_waldur,
                               self.client, self.module)

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

        self.module.fail_json.side_effect = fail_side_effect

        self.assertRaisesRegex(Exception,
                               r'Invalid ethertype',
                               waldur_os_security_group.send_request_to_waldur,
                               self.client, self.module)
