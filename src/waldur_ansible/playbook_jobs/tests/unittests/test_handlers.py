from django.test import TestCase, override_settings
from mock import patch

from .. import factories


class PlaybookHandlersTest(TestCase):
    def setUp(self):
        self.playbook = factories.PlaybookFactory()

    @override_settings(WALDUR_PLAYBOOK_JOBS={'PRESERVE_PLAYBOOK_WORKSPACE_AFTER_DELETION': False})
    def test_playbook_file_is_deleted_with_valid_setting(self):
        with patch('waldur_ansible.playbook_jobs.tasks.delete_playbook_workspace.delay') as mocked_task:
            self.playbook.delete()
            mocked_task.assert_called()

    @override_settings(WALDUR_PLAYBOOK_JOBS={'PRESERVE_PLAYBOOK_WORKSPACE_AFTER_DELETION': True})
    def test_playbook_file_is_preserved_with_valid_setting(self):
        with patch('waldur_ansible.playbook_jobs.tasks.delete_playbook_workspace.delay') as mocked_task:
            self.playbook.delete()
            mocked_task.assert_not_called()
