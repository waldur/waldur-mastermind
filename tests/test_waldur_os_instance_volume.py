import unittest
from unittest import mock

import waldur_os_instance_volume


class BaseVolumeTest(unittest.TestCase):
    def setUp(self):
        self.module = mock.Mock()
        self.module.params = {
            'project': 'database management',
            'volume': 'postgresql-data',
            'instance': 'postgresql-server',
            'device': '/dev/vdb',
            'state': 'absent',
            'wait': False,
            'interval': 10,
            'timeout': 600,
        }


class VolumeDetachTest(BaseVolumeTest):
    def test_volume_is_already_detached(self):
        client = mock.Mock()
        client.get_volume.return_value = {
            'runtime_state': 'available',
        }
        has_changed = waldur_os_instance_volume.send_request_to_waldur(
            client, self.module
        )
        self.assertFalse(has_changed)

    def test_volume_is_attached(self):
        client = mock.Mock()
        client.get_volume.return_value = {
            'runtime_state': 'in-use',
            'uuid': 'volume_uuid',
        }
        has_changed = waldur_os_instance_volume.send_request_to_waldur(
            client, self.module
        )
        self.assertTrue(has_changed)
        client.detach_volume.assert_called_once()


class VolumeAttachTest(BaseVolumeTest):
    def setUp(self):
        super(VolumeAttachTest, self).setUp()
        self.module.params['state'] = 'present'

    def test_volume_is_already_attached(self):
        client = mock.Mock()
        client.get_volume.return_value = {
            'runtime_state': 'in-use',
            'instance': 'instance_url',
        }
        client.get_instance.return_value = {
            'url': 'instance_url',
        }
        has_changed = waldur_os_instance_volume.send_request_to_waldur(
            client, self.module
        )
        self.assertFalse(has_changed)

    def test_volume_is_attached_to_another_instance(self):
        client = mock.Mock()
        client.get_volume.return_value = {
            'runtime_state': 'in-use',
            'uuid': 'volume_uuid',
            'instance': 'instance_url',
        }
        client.get_instance.return_value = {
            'url': 'another_instance_url',
            'uuid': 'another_instance_uuid',
        }
        has_changed = waldur_os_instance_volume.send_request_to_waldur(
            client, self.module
        )
        self.assertTrue(has_changed)
        client.detach_volume.assert_called_once_with('volume_uuid')
        client.attach_volume.assert_called_once_with(
            'volume_uuid',
            'another_instance_uuid',
            '/dev/vdb',
            interval=10,
            timeout=600,
            wait=False,
        )
