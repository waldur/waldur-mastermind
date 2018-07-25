from django.test import TestCase
from mock import patch

from waldur_ansible.common import exceptions
from waldur_ansible.jupyter_hub_management.backend import backend
from waldur_ansible.jupyter_hub_management.tests import factories, fixtures


class JupyterHubManagementBackendTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.JupyterHubManagementOAuthFixture()
        self.module_path = 'waldur_ansible.jupyter_hub_management.backend.backend.'

    def test_do_not_process_when_locked(self):
        jupyter_hub_management_backend = backend.JupyterHubManagementBackend()
        with patch(self.module_path + 'locking_service.JupyterHubManagementBackendLockingService') as locking_service:
            locking_service.is_processing_allowed.return_value = False
            sync_request = factories.JupyterHubManagementSyncConfigurationRequestFactory(
                jupyter_hub_management=self.fixture.jupyter_hub_management)

            self.assertRaises(exceptions.LockedForProcessingError, jupyter_hub_management_backend.process_jupyter_hub_management_request, sync_request)

            self.assertEqual(sync_request.output, backend.JupyterHubManagementBackend.LOCKED_FOR_PROCESSING)

    def test_builds_command_properly(self):
        jupyter_hub_management_backend = backend.JupyterHubManagementBackend()
        jupyter_hub_management = self.fixture.jupyter_hub_management
        sync_request = factories.JupyterHubManagementSyncConfigurationRequestFactory(jupyter_hub_management=jupyter_hub_management)

        with patch(self.module_path + 'JupyterHubManagementBackend.ensure_playbook_exists_or_raise'):
            command = jupyter_hub_management_backend.build_command(sync_request)

        self.assertIn('ansible-playbook', command)
        self.assertIn('--extra-vars', command)
        self.assertIn('--ssh-common-args', command)

    def test_builds_extra_vars(self):
        jupyter_hub_management_backend = backend.JupyterHubManagementBackend()
        jupyter_hub_management = self.fixture.jupyter_hub_management
        sync_request = factories.JupyterHubManagementSyncConfigurationRequestFactory(jupyter_hub_management=jupyter_hub_management)

        extra_vars = jupyter_hub_management_backend.build_extra_vars(sync_request)

        self.assertIn('instance_public_ip', extra_vars)
        self.assertIn('virtual_envs_dir_path', extra_vars)
        self.assertIn('default_system_user', extra_vars)

    def test_releases_lock(self):
        jupyter_hub_management_backend = backend.JupyterHubManagementBackend()
        sync_request = factories.JupyterHubManagementSyncConfigurationRequestFactory(jupyter_hub_management=self.fixture.jupyter_hub_management)

        with patch(self.module_path + 'locking_service.JupyterHubManagementBackendLockingService') as locking_service:
            jupyter_hub_management_backend.handle_on_processing_finished(sync_request)

            locking_service.handle_on_processing_finished.assert_called_once()
