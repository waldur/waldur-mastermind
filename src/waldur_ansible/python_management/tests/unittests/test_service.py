from django.test import TestCase
from mock import patch
from rest_framework.exceptions import APIException
from waldur_ansible.python_management import python_management_service
from waldur_ansible.python_management.tests import factories, fixtures


class PythonManagementServiceTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.PythonManagementFixture()

    def test_identifies_removed_virtual_envs(self):
        virtual_env = factories.VirtualEnvironmentFactory(name='first-virt-env', python_management=self.fixture.python_management)
        factories.InstalledLibraryFactory(name='lib1', version='11', virtual_environment=virtual_env)
        persisted_virtual_envs = [virtual_env]

        transient_virtual_envs = []
        transient_libs = [self.transient_lib('lib2', '22')]
        transient_virtual_envs.append(self.transient_virtual_env('second-virt-env', transient_libs))

        _, _, removed_virtual_envs = python_management_service.PythonManagementService().identify_changed_created_removed_envs(transient_virtual_envs, persisted_virtual_envs)

        self.assertIn(virtual_env, removed_virtual_envs)

    def test_identifies_changed_virtual_envs(self):
        virtual_env = factories.VirtualEnvironmentFactory(name='first-virt-env', python_management=self.fixture.python_management)
        library = factories.InstalledLibraryFactory(name='lib1', version='11', virtual_environment=virtual_env)
        persisted_virtual_envs = [virtual_env]

        transient_virtual_envs = []
        transient_libs = [self.transient_lib('lib2', '22')]
        transient_virtual_envs.append(self.transient_virtual_env(virtual_env.name, transient_libs))

        _, changed_virtual_envs, _ = python_management_service.PythonManagementService().identify_changed_created_removed_envs(transient_virtual_envs, persisted_virtual_envs)

        expected_change = dict(
            name=virtual_env.name,
            libraries_to_install=transient_libs,
            libraries_to_remove=[self.transient_lib(library.name, library.version)],
        )
        self.assertIn(expected_change, changed_virtual_envs)

    def test_identifies_created_virtual_envs(self):
        virtual_env = factories.VirtualEnvironmentFactory(name='first-virt-env', python_management=self.fixture.python_management)
        library = factories.InstalledLibraryFactory(name='lib1', version='11', virtual_environment=virtual_env)
        persisted_virtual_envs = [virtual_env]

        transient_virtual_envs = []
        existing_virtual_env = self.transient_virtual_env(virtual_env.name, [self.transient_lib(library.name, library.version)])
        transient_virtual_envs.append(existing_virtual_env)
        new_virtual_env = self.transient_virtual_env('second-virt-env', [self.transient_lib('lib2', '22')])
        transient_virtual_envs.append(new_virtual_env)

        created_virtual_envs, _, _ = python_management_service.PythonManagementService().identify_changed_created_removed_envs(transient_virtual_envs, persisted_virtual_envs)

        self.assertIn(new_virtual_env, created_virtual_envs)

    def test_identifies_blocked_requests(self):
        sync_request = factories.PythonManagementSynchronizeRequestFactory(
            python_management=self.fixture.python_management, virtual_env_name='virtual-env')

        with patch(
                'waldur_ansible.python_management.python_management_service.locking_service.PythonManagementBackendLockingService.is_processing_allowed') as mocked_locking_service, \
                patch('waldur_ansible.python_management.python_management_service.executors.PythonManagementRequestExecutor.execute') as executor:
            mocked_locking_service.return_value = False

            python_management_service.PythonManagementService().create_or_refuse_request(sync_request)

            executor.assert_not_called()

    def returns_true(self, lock_value):
        return True

    def returns_false(self, lock_value):
        return False

    def test_removal_not_possible_to_process_if_is_processing(self):
        python_management = self.fixture.python_management
        with patch('waldur_ansible.python_management.python_management_service.locking_service.PythonManagementBackendLockingService.is_processing_allowed',
                   side_effect=self.returns_false):
            self.assertRaises(APIException, lambda: python_management_service.PythonManagementService().schedule_python_management_removal(python_management))

    def test_removal_possible_when_not_processing(self):
        python_management = self.fixture.python_management
        with patch('waldur_ansible.python_management.python_management_service.executors.PythonManagementRequestExecutor.execute') as execute:
            python_management_service.PythonManagementService().schedule_python_management_removal(python_management)
            execute.assert_called_once()

    def test_virtual_env_search_not_possible_to_process_if_is_processing(self):
        python_management = self.fixture.python_management
        with patch('waldur_ansible.python_management.python_management_service.locking_service.PythonManagementBackendLockingService.is_processing_allowed',
                   side_effect=self.returns_false):
            self.assertRaises(APIException, lambda: python_management_service.PythonManagementService().schedule_virtual_environments_search(python_management))

    def test_virtual_env_search_possible_when_not_processing(self):
        python_management = self.fixture.python_management
        with patch('waldur_ansible.python_management.python_management_service.executors.PythonManagementRequestExecutor.execute') as execute:
            python_management_service.PythonManagementService().schedule_virtual_environments_search(python_management)
            execute.assert_called_once()

    def test_installed_libs_search_not_possible_to_process_if_is_processing(self):
        python_management = self.fixture.python_management
        virtual_env_name = 'oh-my-env'
        with patch('waldur_ansible.python_management.python_management_service.locking_service.PythonManagementBackendLockingService.is_processing_allowed',
                   side_effect=self.returns_false):
            self.assertRaises(APIException, lambda: python_management_service.PythonManagementService().schedule_installed_libraries_search(python_management, virtual_env_name))

    def test_installed_libs_search_possible_when_not_processing(self):
        python_management = self.fixture.python_management
        virtual_env_name = 'oh-my-env'
        with patch('waldur_ansible.python_management.python_management_service.executors.PythonManagementRequestExecutor.execute') as execute:
            python_management_service.PythonManagementService().schedule_installed_libraries_search(python_management, virtual_env_name)
            execute.assert_called_once()

    def test_update_not_possible_to_process_if_is_processing(self):
        python_management = self.fixture.python_management
        with patch('waldur_ansible.python_management.python_management_service.cache_utils.is_syncing', side_effect=self.returns_true):
            self.assertRaises(APIException, lambda: python_management_service.PythonManagementService().schedule_virtual_environments_update([], python_management))

    def test_update_possible_when_not_processing(self):
        python_management = self.fixture.python_management
        with patch('waldur_ansible.python_management.python_management_service.executors.PythonManagementRequestExecutor.execute'), \
                patch('waldur_ansible.python_management.python_management_service.PythonManagementService.create_or_refuse_requests') as create_or_refuse_requests:
            create_or_refuse_requests.return_value = []
            python_management_service.PythonManagementService().schedule_virtual_environments_update([], python_management)
            create_or_refuse_requests.assert_called_once()

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
