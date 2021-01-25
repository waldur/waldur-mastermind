import unittest

import mock

import waldur_os_security_group


def group_side_effect(*args, **kwargs):
    if args[1] == 'web':
        return {'url': 'api/123'}


class SecurityGroupCreateTest(unittest.TestCase):
    def setUp(self) -> None:
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

    def test_group_creation_with_link_to_remote_group(self):

        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'remote_group': 'web',
                'protocol': 'tcp'
            }
        ]

        client = mock.Mock()
        client.get_security_group.side_effect = group_side_effect

        has_changed = waldur_os_security_group.send_request_to_waldur(client, self.module)

        client.create_security_group.assert_called_once_with(
            project=None,
            tenant='tenant',
            name='sec-group',
            description='descr',
            rules=[{
                'from_port': '80',
                'to_port': '80',
                'remote_group': 'api/123',
                'protocol': 'tcp'
            }],
            tags=None,
            wait=True,
            interval=20,
            timeout=600,
        )
        self.assertTrue(has_changed)

    def test_group_creation_erred_with_invalid_params(self):
        def fail_side_effect(*args, **kwargs):
            raise Exception(kwargs['msg'])

        self.module.params['rules'] = [
            {
                'from_port': '80',
                'to_port': '80',
                'protocol': 'tcp'
            }
        ]

        client = mock.Mock()
        client.get_security_group.side_effect = group_side_effect

        self.module.fail_json.side_effect = fail_side_effect

        self.assertRaisesRegex(Exception,
                               'One of cidr and remote_group parameters must be specified.',
                               waldur_os_security_group.send_request_to_waldur,
                               client, self.module,)
