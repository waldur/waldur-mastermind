from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import override_settings
from rest_framework import test

from waldur_core.structure.models import ProjectRole
from waldur_core.structure.tests.factories import ProjectPermissionFactory, UserFactory
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_remote import PLUGIN_NAME

REMOTE_USER_UUID = '111'
REMOTE_PROJECT_UUID = '112'
REMOTE_PERMISSION_ID = '1'


@override_settings(
    WALDUR_AUTH_SOCIAL={'ENABLE_EDUTEAMS_SYNC': True},
    task_always_eager=True,
)
class RemoteProjectPermissionsTestCase(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.mp_fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.mp_fixture.project
        self.new_user = UserFactory(registration_method='eduteams')

        resource = self.mp_fixture.resource
        resource.set_state_ok()
        resource.save()
        self.resource = resource

        offering = self.mp_fixture.offering
        offering.backend_id = 'ABC'
        offering.secret_options = {
            'api_url': 'http://offerings.example.com/api',
            'token': 'AAABBBCCC',
            'customer_uuid': '12345',
        }
        offering.type = PLUGIN_NAME
        offering.save()
        self.offering = offering

        self.customer = self.mp_fixture.customer

        self.patcher = mock.patch(
            "waldur_mastermind.marketplace_remote.utils.WaldurClient"
        )
        client_mock = self.patcher.start()
        client_mock().get_remote_eduteams_user.return_value = {'uuid': REMOTE_USER_UUID}
        client_mock().list_projects.return_value = [{'uuid': REMOTE_PROJECT_UUID}]
        client_mock().get_project_permissions.return_value = []
        self.client_mock = client_mock

    def tearDown(self):
        super(RemoteProjectPermissionsTestCase, self).tearDown()
        mock.patch.stopall()

    def create_permission(self, role, expiration_time=None):
        return self.project.add_user(
            user=self.new_user,
            role=role,
            expiration_time=expiration_time,
        )

    def delete_permission(self, role):
        return self.project.remove_user(
            user=self.new_user,
            role=role,
        )

    def test_create_remote_permission(self):
        self.create_permission(ProjectRole.ADMINISTRATOR)
        self.client_mock().get_remote_eduteams_user.assert_called_once_with(
            self.new_user.username
        )
        self.client_mock().list_projects.assert_called_once_with(
            {'backend_id': f'{self.customer.uuid}_{self.project.uuid}'}
        )
        self.client_mock().get_project_permissions.assert_called_once_with(
            REMOTE_PROJECT_UUID,
            REMOTE_USER_UUID,
            ProjectRole.ADMINISTRATOR,
        )
        self.client_mock().create_project_permission.assert_called_once_with(
            '111',
            '112',
            ProjectRole.ADMINISTRATOR,
            None,
        )

    def test_create_remote_permission_with_expiration_time(self):
        time = datetime.now() + timedelta(days=1)
        self.create_permission(ProjectRole.ADMINISTRATOR, time)
        self.client_mock().get_remote_eduteams_user.assert_called_once_with(
            self.new_user.username
        )
        self.client_mock().list_projects.assert_called_once_with(
            {'backend_id': f'{self.customer.uuid}_{self.project.uuid}'}
        )
        self.client_mock().get_project_permissions.assert_called_once_with(
            REMOTE_PROJECT_UUID,
            REMOTE_USER_UUID,
            ProjectRole.ADMINISTRATOR,
        )
        self.client_mock().create_project_permission.assert_called_once_with(
            '111',
            '112',
            ProjectRole.ADMINISTRATOR,
            time.isoformat(),
        )

    def test_update_remote_permission(self):
        old_time = datetime.now() + timedelta(days=1)
        new_time = (datetime.now() + timedelta(days=2)).replace(tzinfo=timezone.utc)
        permission, _ = self.create_permission(ProjectRole.ADMINISTRATOR, old_time)
        self.client.force_login(self.mp_fixture.owner)
        self.client_mock().get_project_permissions.return_value = [
            {'pk': REMOTE_PERMISSION_ID, 'expiration_time': old_time.isoformat()}
        ]
        self.client.patch(
            ProjectPermissionFactory.get_url(permission), {'expiration_time': new_time}
        )
        self.client_mock().update_project_permission.assert_called_once_with(
            REMOTE_PERMISSION_ID, new_time.isoformat()
        )

    def test_delete_remote_permission(self):
        self.create_permission(ProjectRole.ADMINISTRATOR)
        self.client_mock().get_project_permissions.return_value = [
            {'pk': REMOTE_PERMISSION_ID, 'expiration_time': None}
        ]
        self.delete_permission(ProjectRole.ADMINISTRATOR)
        self.client_mock().remove_project_permission.assert_called_once_with(
            REMOTE_PERMISSION_ID
        )
