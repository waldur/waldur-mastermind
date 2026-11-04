from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.tests.helpers import create_pat
from waldur_core.permissions.enums import PermissionEnum

from . import factories, fixtures

CONSOLE_METHODS = {
    "console": "get_console_url",
    "web_console": "get_web_console_url",
}


@ddt
class VirtualMachineConsolePermissionTest(test.APITestCase):
    """The web console serves those who may change the VM; the remote console
    is staff only."""

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
    def test_administrators_open_the_web_console(self, user):
        response = self._get(user, "web_console")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["url"], "url")

    def test_staff_open_the_remote_console(self):
        response = self._get("staff", "console")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["url"], "url")

    @data("owner", "manager", "admin", "global_support")
    def test_only_staff_open_the_remote_console(self, user):
        response = self._get(user, "console")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.mocks["console"].assert_not_called()

    @data("member", "global_support", "customer_support")
    def test_viewers_who_may_not_change_the_vm_are_refused(self, user):
        for action in CONSOLE_METHODS:
            with self.subTest(action=action):
                response = self._get(user, action)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.mocks[action].assert_not_called()


@override_config(PAT_ENABLED=True)
class VirtualMachineConsoleTokenScopeTest(test.APITestCase):
    """A staff user's personal access token opens the remote console only
    with the staff scope."""

    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.vm = self.fixture.virtual_machine
        patcher = mock.patch(
            "waldur_vmware.backend.VMwareBackend.get_console_url", return_value="url"
        )
        self.get_console_url = patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self, scopes):
        staff = self.fixture.staff
        staff.can_use_personal_access_tokens = True
        staff.save(update_fields=["can_use_personal_access_tokens"])
        _, token = create_pat(staff, scopes=scopes)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return self.client.get(
            factories.VirtualMachineFactory.get_url(self.vm, "console")
        )

    def test_staff_token_without_staff_scope_is_refused(self):
        response = self._get([PermissionEnum.LIST_PROJECTS.value])
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.get_console_url.assert_not_called()

    def test_staff_token_with_staff_scope_opens_the_remote_console(self):
        response = self._get([PermissionEnum.STAFF_ACCESS.value])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["url"], "url")
