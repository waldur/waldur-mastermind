from unittest import mock

from rest_framework import test

from waldur_vmware import backend, models
from waldur_vmware.tests.utils import override_plugin_settings

from . import factories


class TemplatePullTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.settings = factories.VMwareServiceSettingsFactory()
        self.backend = backend.VMwareBackend(self.settings)
        self.patcher = mock.patch("waldur_vmware.backend.VMwareClient")
        self.mock_client = self.patcher.start()
        self.ALL_TEMPLATES = [
            {
                "library_item": {
                    "creation_time": "2015-01-01T22:13:05.651Z",
                    "description": "string",
                    "id": "obj-103",
                    "last_modified_time": "2015-01-01T22:13:05.651Z",
                    "last_sync_time": "2015-01-01T22:13:05.651Z",
                    "name": "string",
                    "type": "vm-template",
                    "version": "string",
                },
                "template": {
                    "cpu": {"cores_per_socket": 1, "count": 1},
                    "disks": [
                        {
                            "key": "obj-103",
                            "value": {
                                "capacity": 1,
                                "disk_storage": {
                                    "datastore": "obj-103",
                                    "storage_policy": "obj-103",
                                },
                            },
                        }
                    ],
                    "guest_OS": "DOS",
                    "memory": {"size_MiB": 1},
                    "nics": [
                        {
                            "key": "obj-103",
                            "value": {
                                "backing_type": "STANDARD_PORTGROUP",
                                "mac_type": "MANUAL",
                                "network": "obj-103",
                            },
                        }
                    ],
                    "vm_home_storage": {
                        "datastore": "obj-103",
                        "storage_policy": "obj-103",
                    },
                    "vm_template": "string",
                },
            }
        ]

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_delete_old_templates(self):
        factories.TemplateFactory(settings=self.settings)
        factories.TemplateFactory(settings=self.settings)
        self.backend.pull_templates()
        self.assertEqual(models.Template.objects.count(), 0)

    def test_add_new_templates(self):
        client = mock.MagicMock()
        self.mock_client.return_value = client
        client.list_all_templates.return_value = self.ALL_TEMPLATES

        self.backend.pull_templates()
        self.assertEqual(models.Template.objects.count(), 1)

    @override_plugin_settings(BASIC_MODE=True)
    def test_template_with_multiple_nics_is_skipped(self):
        client = mock.MagicMock()
        self.mock_client.return_value = client
        self.ALL_TEMPLATES[0]["template"]["nics"].append(
            {
                "key": "obj-104",
                "value": {
                    "backing_type": "STANDARD_PORTGROUP",
                    "mac_type": "MANUAL",
                    "network": "obj-104",
                },
            }
        )
        client.list_all_templates.return_value = self.ALL_TEMPLATES

        self.backend.pull_templates()
        self.assertEqual(models.Template.objects.count(), 0)

    @override_plugin_settings(BASIC_MODE=True)
    def test_template_without_nics_is_skipped(self):
        client = mock.MagicMock()
        self.mock_client.return_value = client
        self.ALL_TEMPLATES[0]["template"]["nics"] = []
        client.list_all_templates.return_value = self.ALL_TEMPLATES

        self.backend.pull_templates()
        self.assertEqual(models.Template.objects.count(), 0)

    @override_plugin_settings(BASIC_MODE=True)
    def test_template_with_no_nics_key_is_skipped(self):
        """A template with no network adapters omits the key entirely.

        Indexing it directly makes the whole service-properties pull fail with
        KeyError, not just skip the one template.
        """
        client = mock.MagicMock()
        self.mock_client.return_value = client
        del self.ALL_TEMPLATES[0]["template"]["nics"]
        client.list_all_templates.return_value = self.ALL_TEMPLATES

        self.backend.pull_templates()
        self.assertEqual(models.Template.objects.count(), 0)

    def test_template_with_an_empty_hardware_spec_is_skipped(self):
        """vCenter answers for a library item it cannot describe with a skeleton.

        Every field it leaves out is non-null on the model, so indexing the
        skeleton fails the whole service-properties pull rather than skipping
        the one item. Basic mode is off here on purpose: with it on, the NIC
        filter happens to drop such an item before it is ever converted.
        """
        client = mock.MagicMock()
        self.mock_client.return_value = client
        self.ALL_TEMPLATES[0]["template"] = {
            "cpu": {},
            "memory": {},
            "vm_home_storage": {},
        }
        client.list_all_templates.return_value = self.ALL_TEMPLATES

        self.backend.pull_templates()

        self.assertEqual(models.Template.objects.count(), 0)

    def test_template_without_a_guest_os_is_skipped(self):
        client = mock.MagicMock()
        self.mock_client.return_value = client
        del self.ALL_TEMPLATES[0]["template"]["guest_OS"]
        client.list_all_templates.return_value = self.ALL_TEMPLATES

        self.backend.pull_templates()

        self.assertEqual(models.Template.objects.count(), 0)

    def test_template_without_disks_is_pulled_with_no_disk_size(self):
        """A diskless template is describable, unlike an empty skeleton."""
        client = mock.MagicMock()
        self.mock_client.return_value = client
        del self.ALL_TEMPLATES[0]["template"]["disks"]
        client.list_all_templates.return_value = self.ALL_TEMPLATES

        self.backend.pull_templates()

        self.assertEqual(models.Template.objects.get().disk, 0)
