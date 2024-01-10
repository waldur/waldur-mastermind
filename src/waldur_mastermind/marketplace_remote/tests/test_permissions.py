import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import override_settings
from rest_framework import test

from waldur_auth_social.models import ProviderChoices
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_remote import PLUGIN_NAME

REMOTE_USER_UUID = uuid.uuid4().hex
REMOTE_PROJECT_UUID = uuid.uuid4().hex
REMOTE_CUSTOMER_UUID = uuid.uuid4().hex


@override_settings(
    WALDUR_AUTH_SOCIAL={"ENABLE_EDUTEAMS_SYNC": True},
    task_always_eager=True,
    task_eager_propagates=True,
)
class RemoteProjectPermissionsTestCase(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.mp_fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.mp_fixture.project
        self.new_user = UserFactory(registration_method=ProviderChoices.EDUTEAMS)

        resource = self.mp_fixture.resource
        resource.set_state_ok()
        resource.save()
        self.resource = resource

        offering = self.mp_fixture.offering
        offering.backend_id = "ABC"
        offering.secret_options = {
            "api_url": "http://offerings.example.com/api",
            "token": "AAABBBCCC",
            "customer_uuid": REMOTE_CUSTOMER_UUID,
        }
        offering.type = PLUGIN_NAME
        offering.save()
        self.offering = offering

        self.customer = self.mp_fixture.customer

        self.patcher = mock.patch(
            "waldur_mastermind.marketplace_remote.utils.WaldurClient"
        )
        client_mock = self.patcher.start()
        client_mock().get_remote_eduteams_user.return_value = {"uuid": REMOTE_USER_UUID}
        client_mock().list_projects.return_value = [{"uuid": REMOTE_PROJECT_UUID}]
        client_mock().get_project_permissions.return_value = []
        self.client_mock = client_mock

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_create_remote_permission(self):
        self.project.add_user(
            user=self.new_user,
            role=ProjectRole.ADMIN,
        )
        self.client_mock().get_remote_eduteams_user.assert_called_once_with(
            self.new_user.username
        )
        self.client_mock().list_projects.assert_called_once_with(
            {"backend_id": f"{self.customer.uuid}_{self.project.uuid}"}
        )
        self.client_mock().get_project_permissions.assert_called_once_with(
            REMOTE_PROJECT_UUID,
            REMOTE_USER_UUID,
            RoleEnum.PROJECT_ADMIN,
        )
        self.client_mock().create_project_permission.assert_called_once_with(
            REMOTE_PROJECT_UUID,
            REMOTE_USER_UUID,
            RoleEnum.PROJECT_ADMIN,
            None,
        )

    def test_create_remote_permission_with_expiration_time(self):
        expiration_time = datetime.now() + timedelta(days=1)
        self.project.add_user(
            user=self.new_user,
            role=ProjectRole.ADMIN,
            expiration_time=expiration_time,
        )
        self.client_mock().get_remote_eduteams_user.assert_called_once_with(
            self.new_user.username
        )
        self.client_mock().list_projects.assert_called_once_with(
            {"backend_id": f"{self.customer.uuid}_{self.project.uuid}"}
        )
        self.client_mock().get_project_permissions.assert_called_once_with(
            REMOTE_PROJECT_UUID,
            REMOTE_USER_UUID,
            RoleEnum.PROJECT_ADMIN,
        )
        self.client_mock().create_project_permission.assert_called_once_with(
            REMOTE_PROJECT_UUID,
            REMOTE_USER_UUID,
            RoleEnum.PROJECT_ADMIN,
            expiration_time.isoformat(),
        )

    def test_update_remote_permission(self):
        old_expiration_time = datetime.now() + timedelta(days=1)
        new_expiration_time = (datetime.now() + timedelta(days=2)).replace(
            tzinfo=timezone.utc
        )
        permission = self.project.add_user(
            user=self.new_user,
            role=ProjectRole.ADMIN,
            expiration_time=old_expiration_time,
        )
        self.client_mock().get_project_permissions.return_value = [
            {"expiration_time": old_expiration_time.isoformat()}
        ]
        permission.set_expiration_time(new_expiration_time)
        self.client_mock().update_project_permission.assert_called_once_with(
            REMOTE_PROJECT_UUID,
            REMOTE_USER_UUID,
            RoleEnum.PROJECT_ADMIN,
            new_expiration_time.isoformat(),
        )

    def test_delete_remote_permission(self):
        self.project.add_user(
            user=self.new_user,
            role=ProjectRole.ADMIN,
        )
        self.client_mock().get_project_permissions.return_value = [
            {"expiration_time": None}
        ]
        self.project.remove_user(
            user=self.new_user,
            role=ProjectRole.ADMIN,
        )
        self.client_mock().remove_project_permission.assert_called_once_with(
            REMOTE_PROJECT_UUID,
            REMOTE_USER_UUID,
            RoleEnum.PROJECT_ADMIN,
        )
