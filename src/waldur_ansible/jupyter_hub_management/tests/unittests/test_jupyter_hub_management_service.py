from django.test import TestCase
from mock import patch
from rest_framework.exceptions import APIException
from waldur_ansible.jupyter_hub_management import jupyter_hub_management_service
from waldur_ansible.jupyter_hub_management.tests import fixtures
from waldur_ansible.python_management.tests import factories as python_management_factories


class JupyterHubManagementServiceTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.JupyterHubManagementOAuthFixture()

    def test_issue_globalize_request(self):
        jupyter_hub_management = self.fixture.jupyter_hub_management
        updated_virtual_environments = [dict(jupyter_hub_global=True, name='first-virt-env')]
        validated_data = dict(updated_virtual_environments=updated_virtual_environments)

        virtual_env = python_management_factories.VirtualEnvironmentFactory(name='first-virt-env', python_management=jupyter_hub_management.python_management)

        module_under_test = 'waldur_ansible.jupyter_hub_management.jupyter_hub_management_service.'
        with patch(module_under_test + 'python_management_models.VirtualEnvironment.objects.get') as find_virtual_env_mock, \
                patch(module_under_test + 'executors.JupyterHubManagementRequestExecutor.execute') as executor_mock:
            find_virtual_env_mock.return_value = virtual_env

            jupyter_hub_management_service.JupyterHubManagementService().issue_localize_globalize_requests(jupyter_hub_management, validated_data)

            executor_mock.assert_called_once()

    def test_issue_localize_request(self):
        jupyter_hub_management = self.fixture.jupyter_hub_management
        updated_virtual_environments = [dict(jupyter_hub_global=False, name='first-virt-env')]
        validated_data = dict(updated_virtual_environments=updated_virtual_environments)

        virtual_env = python_management_factories.VirtualEnvironmentFactory(name='first-virt-env', jupyter_hub_global=True,
                                                                            python_management=jupyter_hub_management.python_management)

        module_under_test = 'waldur_ansible.jupyter_hub_management.jupyter_hub_management_service.'
        with patch(module_under_test + 'python_management_models.VirtualEnvironment.objects.get') as find_virtual_env_mock, \
                patch(module_under_test + 'executors.JupyterHubManagementRequestExecutor.execute') as executor_mock:
            find_virtual_env_mock.return_value = virtual_env

            jupyter_hub_management_service.JupyterHubManagementService().issue_localize_globalize_requests(jupyter_hub_management, validated_data)

            executor_mock.assert_called_once()

    def test_schedule_jupyter_hub_management_removal_not_locked(self):
        jupyter_hub_management = self.fixture.jupyter_hub_management

        module_under_test = 'waldur_ansible.jupyter_hub_management.jupyter_hub_management_service.'
        with patch(module_under_test + 'locking_service.JupyterHubManagementBackendLockingService.is_processing_allowed') as is_processing_allowed_mock, \
                patch(module_under_test + 'executors.JupyterHubManagementRequestExecutor.execute') as executor_mock:
            is_processing_allowed_mock.return_value = True

            jupyter_hub_management_service.JupyterHubManagementService().schedule_jupyter_hub_management_removal(jupyter_hub_management)

            executor_mock.assert_called_once()

    def test_schedule_jupyter_hub_management_removal_locked_exception_thrown(self):
        jupyter_hub_management = self.fixture.jupyter_hub_management

        module_under_test = 'waldur_ansible.jupyter_hub_management.jupyter_hub_management_service.'
        with patch(module_under_test + 'locking_service.JupyterHubManagementBackendLockingService.is_processing_allowed') as is_processing_allowed_mock, \
                patch(module_under_test + 'executors.JupyterHubManagementRequestExecutor.execute'):
            is_processing_allowed_mock.return_value = False

            self.assertRaises(APIException, jupyter_hub_management_service.JupyterHubManagementService().schedule_jupyter_hub_management_removal, jupyter_hub_management)
