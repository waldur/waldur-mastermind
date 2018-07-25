from django.test import TestCase, override_settings
from mock import patch, call

from waldur_ansible.common import exceptions
from waldur_ansible.python_management.backend import python_management_backend
from waldur_ansible.python_management.tests import factories, fixtures


class PythonManagementServiceTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.PythonManagementFixture()
        self.module_path = 'waldur_ansible.python_management.backend.python_management_backend.'

    def test_backend_starts_processing(self):
        backend_under_test = python_management_backend.PythonManagementInitializationBackend()
        init_request = factories.PythonManagementInitializeRequestFactory(python_management=self.fixture.python_management)

        with patch(self.module_path + 'PythonManagementBackend.process_request') as mocked_process_request:
            backend_under_test.process_python_management_request(init_request)

            mocked_process_request.assert_called_once_with(init_request)

    def test_init_backend_issues_additional_requests(self):
        backend_under_test = python_management_backend.PythonManagementInitializationBackend()
        init_request = factories.PythonManagementInitializeRequestFactory(python_management=self.fixture.python_management)
        init_request.sychronization_requests = [factories.PythonManagementSynchronizeRequestFactory(
            python_management=self.fixture.python_management, virtual_env_name='virtual-env')]

        with patch(self.module_path + 'executors.PythonManagementRequestExecutor.execute') as mocked_execute, \
                patch(self.module_path + 'PythonManagementBackend.process_request'):
            backend_under_test.process_python_management_request(init_request)
            mocked_execute.assert_called_once()

    @override_settings(WALDUR_ANSIBLE_COMMON={'ANSIBLE_LIBRARY': '/ansible_playbooks/path', 'REMOTE_VM_SSH_PORT': '22'})
    def test_process_request(self):
        backend = python_management_backend.PythonManagementBackend()
        with patch(self.module_path + 'PythonManagementBackend.build_command') as build_command, \
                patch(self.module_path + 'PythonManagementBackend.instantiate_extracted_information_handler_class') as intantiate_extracted_information_handler_class, \
                patch(self.module_path + 'PythonManagementBackend.instantiate_line_post_processor_class') as instantiate_line_post_processor_class, \
                patch('waldur_ansible.common.backend.utils.subprocess_output_iterator') as process_output_iterator, \
                patch(self.module_path + 'extracted_information_handlers.NullExtractedInformationHandler') as mock_extracted_information_handler, \
                patch(self.module_path + 'output_lines_post_processors.NullOutputLinesPostProcessor') as lines_post_processor_instance, \
                patch(self.module_path + 'locking_service.PythonManagementBackendLockingService') as locking_service:
            locking_service.is_processing_allowed.return_value = True
            build_command.return_value = ['command']
            intantiate_extracted_information_handler_class.return_value = mock_extracted_information_handler
            instantiate_line_post_processor_class.return_value = lines_post_processor_instance
            first_line = 'output1'
            second_line = 'output2'
            process_output_iterator.return_value = iter([first_line, second_line])

            python_management = self.fixture.python_management
            sync_request = factories.PythonManagementSynchronizeRequestFactory(
                python_management=python_management, virtual_env_name='virtual-env', output='')

            backend.process_python_management_request(sync_request)

            lines_post_processor_instance.post_process_line.assert_has_calls([call(first_line), call(second_line)])
            mock_extracted_information_handler.handle_extracted_information.assert_called_once()
            locking_service.handle_on_processing_finished.assert_called_once()

    def test_do_not_process_when_locked(self):
        backend = python_management_backend.PythonManagementBackend()
        with patch(self.module_path + 'locking_service.PythonManagementBackendLockingService') as locking_service:
            locking_service.is_processing_allowed.return_value = False
            python_management = self.fixture.python_management
            sync_request = factories.PythonManagementSynchronizeRequestFactory(
                python_management=python_management, virtual_env_name='virtual-env')

            self.assertRaises(exceptions.LockedForProcessingError, backend.process_python_management_request, sync_request)

            self.assertEqual(sync_request.output, python_management_backend.PythonManagementBackend.LOCKED_FOR_PROCESSING)

    def test_builds_command_properly(self):
        python_management = self.fixture.python_management
        python_management.instance.image_name = 'debian'
        sync_request = factories.PythonManagementSynchronizeRequestFactory(python_management=python_management, virtual_env_name='virtual-env')

        with patch(self.module_path + 'PythonManagementBackend.ensure_playbook_exists_or_raise'):
            command = python_management_backend.PythonManagementBackend().build_command(sync_request)

        self.assertIn('ansible-playbook', command)
        self.assertIn('--extra-vars', command)
        self.assertIn('--ssh-common-args', command)

    def test_builds_sync_extra_vars_properly(self):
        python_management = self.fixture.python_management
        python_management.instance.image_name = 'debian'
        sync_request = factories.PythonManagementSynchronizeRequestFactory(python_management=python_management, virtual_env_name='virtual-env')

        extra_vars = python_management_backend.PythonManagementBackend().build_additional_extra_vars(sync_request)

        self.assertTrue('libraries_to_install' in extra_vars)
        self.assertTrue('libraries_to_remove' in extra_vars)
        self.assertTrue('virtual_env_name' in extra_vars)


def transient_lib(self, name, version):
    return dict(
        name=name,
        version=version,
    )


def transient_virtual_env(self, name, libs):
    return dict(
        name=name,
        installed_libraries=libs
    )
