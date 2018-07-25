import requests
import urllib3

from unittest import skipUnless
from django.conf import settings
from django.test import TestCase, tag
from mock import patch

from waldur_ansible.common.tests.integration.ubuntu1604_container import Ubuntu1604Container, CONTAINER_SSH_PORT_ON_HOST
from waldur_ansible.common.tests.integration import integration_tests_config
from waldur_ansible.common.tests.integration.ubuntu1604_image import Ubuntu1604Image
from waldur_ansible.jupyter_hub_management.tests import factories as jupyter_hub_factories, fixtures as jupyter_hub_fixtures
from waldur_ansible.python_management.tests import factories as python_management_factories
from waldur_openstack.openstack_tenant import models as openstack_tenant_models


@tag(integration_tests_config.INTEGRATION_TEST)
@skipUnless(integration_tests_config.integration_test_flag_provided(), integration_tests_config.SKIP_INTEGRATION_REASON)
class JupyterHubManagementIntegrationTest(TestCase):

    UBUNTU_IMAGE = Ubuntu1604Image()

    @classmethod
    def setUpClass(cls):
        super(JupyterHubManagementIntegrationTest, cls).setUpClass()
        # For some reason setUpClass gets called by Django test logic even if test is skipped
        if integration_tests_config.integration_test_flag_provided():
            JupyterHubManagementIntegrationTest.UBUNTU_IMAGE.build_image()

    def setUp(self):
        self.fixture = jupyter_hub_fixtures.JupyterHubManagementLinuxPamFixture()
        self.module_path = "waldur_ansible.python_management.backend.python_management_backend."

    def test_jupyter_hub_initialization(self):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        waldur_ansible_common_settings = settings.WALDUR_ANSIBLE_COMMON.copy()
        waldur_ansible_common_settings["REMOTE_VM_SSH_PORT"] = CONTAINER_SSH_PORT_ON_HOST
        waldur_ansible_common_settings["PRIVATE_KEY_PATH"] = JupyterHubManagementIntegrationTest.UBUNTU_IMAGE.get_private_key_path()

        with self.settings(WALDUR_ANSIBLE_COMMON=waldur_ansible_common_settings):
            ubuntu_container = Ubuntu1604Container()
            container_https_port = "443/tcp"
            ubuntu_container.bind_port(container_https_port, "4444")

            with ubuntu_container:
                python_management = self.fixture.jupyter_hub_admin_user.jupyter_hub_management.python_management
                self.adapt_instance_external_ip(python_management, ubuntu_container)

                with patch(self.module_path + "locking_service.PythonManagementBackendLockingService") as locking_service:
                    locking_service.is_processing_allowed.return_value = True

                    python_management_init_request = python_management_factories.PythonManagementInitializeRequestFactory(python_management=python_management, output="")
                    python_management_init_request.get_backend().process_python_management_request(python_management_init_request)

                    jup_init_request = jupyter_hub_factories.JupyterHubManagementSyncConfigurationRequestFactory(
                        jupyter_hub_management=self.fixture.jupyter_hub_management_linux_pam, output="")
                    jup_init_request.get_backend().process_jupyter_hub_management_request(jup_init_request)

                jupyter_hub_request = requests.get("https://%s:%s" % (ubuntu_container.get_container_host_ip(), ubuntu_container.ports[container_https_port]), verify=False)
                self.assertEquals(jupyter_hub_request.status_code, 200)

    def adapt_instance_external_ip(self, python_management, ubuntu_container):
        container_host_ip = ubuntu_container.get_container_host_ip()
        openstack_tenant_models.FloatingIP.objects.filter(id=python_management.instance.floating_ips[0].id).update(address=container_host_ip, name=container_host_ip)
