from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from . import factories, fixtures

CONSOLE_METHODS = {
    "console": "get_console_url",
    "web_console": "get_web_console_url",
}


@ddt
class VirtualMachineConsolePermissionTest(test.APITestCase):
    """A console controls the VM, so only those who may change it open one."""

    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.vm = self.fixture.virtual_machine
        self.mocks = {}
        for action, method in CONSOLE_METHODS.items():
            patcher = mock.patch(
                f"waldur_vmware.backend.VMwareBackend.{method}", return_value="url"
            )
            self.mocks[action] = patcher.start()
            self.addCleanup(patcher.stop)

    def _get(self, user, action):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        return self.client.get(factories.VirtualMachineFactory.get_url(self.vm, action))

    @data("staff", "owner", "manager", "admin")
    def test_administrators_open_the_console(self, user):
        for action in CONSOLE_METHODS:
            with self.subTest(action=action):
                response = self._get(user, action)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["url"], "url")

    @data("member", "global_support", "customer_support")
    def test_viewers_who_may_not_change_the_vm_are_refused(self, user):
        for action in CONSOLE_METHODS:
            with self.subTest(action=action):
                response = self._get(user, action)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.mocks[action].assert_not_called()
