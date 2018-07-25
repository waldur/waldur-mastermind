from django.test import TestCase
from mock import patch
from waldur_ansible.jupyter_hub_management.backend import extracted_information_handlers
from waldur_ansible.jupyter_hub_management.tests import fixtures, factories
from waldur_ansible.python_management.backend import extracted_information_handlers as python_handlers, output_lines_post_processors as python_post_processors
from waldur_ansible.python_management.tests import factories as python_management_factories


class JupyterHubManagementServiceTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.JupyterHubManagementOAuthFixture()

    def test_delete_request_handler(self):
        extracted_information_handler = extracted_information_handlers.JupyterHubManagementDeleteExtractedInformationHandler()
        jupyter_hub_management = self.fixture.jupyter_hub_management
        virtual_env = python_management_factories.VirtualEnvironmentFactory(name='first-virt-env', jupyter_hub_global=True,
                                                                            python_management=jupyter_hub_management.python_management)
        jupyter_hub_management.python_management.virtual_environments = [virtual_env]
        delete_request = factories.JupyterHubManagementDeleteRequestFactory(jupyter_hub_management=jupyter_hub_management)

        extracted_information_handler.handle_extracted_information(delete_request, python_handlers.NullExtractedInformationHandler())

        self.assertTrue(all(not ve.jupyter_hub_global for ve in jupyter_hub_management.python_management.virtual_environments.all()))

    def test_make_virtual_env_local_handler(self):
        extracted_information_handler = extracted_information_handlers.JupyterHubVirtualEnvironmentLocalExtractedInformationHandler()

        jupyter_hub_management = self.fixture.jupyter_hub_management
        jupyter_hub_management.python_management.virtual_environments = [
            python_management_factories.VirtualEnvironmentFactory(name='first-virt-env', jupyter_hub_global=True,
                                                                  python_management=jupyter_hub_management.python_management)]
        make_ve_local_request = factories.JupyterHubManagementMakeVirtualEnvironmentLocalRequestFactory(
            jupyter_hub_management=jupyter_hub_management, virtual_env_name='first-virt-env')

        with patch('waldur_ansible.python_management.backend.extracted_information_handlers.InstalledLibrariesExtractedInformationHandler') as installed_libs_handler_mock:
            extracted_information_handler.handle_extracted_information(make_ve_local_request, python_post_processors.InstalledLibrariesOutputLinesPostProcessor())
            self.assertTrue(all(not ve.jupyter_hub_global for ve in jupyter_hub_management.python_management.virtual_environments.all()))
            installed_libs_handler_mock.assert_called_once()

    def test_make_virtual_env_global_handler(self):
        extracted_information_handler = extracted_information_handlers.JupyterHubVirtualEnvironmentGlobalExtractedInformationHandler()

        jupyter_hub_management = self.fixture.jupyter_hub_management
        jupyter_hub_management.python_management.virtual_environments = [
            python_management_factories.VirtualEnvironmentFactory(name='first-virt-env', jupyter_hub_global=False,
                                                                  python_management=jupyter_hub_management.python_management)]
        make_ve_local_request = factories.JupyterHubManagementMakeVirtualEnvironmentLocalRequestFactory(
            jupyter_hub_management=jupyter_hub_management, virtual_env_name='first-virt-env')

        with patch('waldur_ansible.python_management.backend.extracted_information_handlers.InstalledLibrariesExtractedInformationHandler') as installed_libs_handler_mock:
            extracted_information_handler.handle_extracted_information(make_ve_local_request, python_post_processors.InstalledLibrariesOutputLinesPostProcessor())
            self.assertTrue(all(ve.jupyter_hub_global for ve in jupyter_hub_management.python_management.virtual_environments.all()))
            installed_libs_handler_mock.assert_called_once()
