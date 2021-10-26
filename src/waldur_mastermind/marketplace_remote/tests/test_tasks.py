import uuid
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework import test

from waldur_core.structure.models import CustomerRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.marketplace_remote import PLUGIN_NAME, tasks


@override_settings(WALDUR_AUTH_SOCIAL={'ENABLE_EDUTEAMS_SYNC': True})
class SyncRemoteProjectPermissionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.patcher = mock.patch(
            "waldur_mastermind.marketplace_remote.utils.WaldurClient"
        )
        self.client = self.patcher.start()()
        self.remote_customer_uuid = uuid.uuid4().hex
        self.remote_project_uuid = uuid.uuid4().hex
        self.remote_user_uuid = uuid.uuid4().hex
        remote_api_token = uuid.uuid4().hex

        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = models.Resource.States.OK
        self.resource.save()
        self.resource.offering.type = PLUGIN_NAME
        self.resource.offering.secret_options = {
            'api_url': 'https://example.com/',
            'token': remote_api_token,
            'customer_uuid': self.remote_customer_uuid,
        }
        self.resource.offering.save()

    def tearDown(self):
        super(SyncRemoteProjectPermissionsTest, self).tearDown()
        mock.patch.stopall()

    def test_project_is_not_created_if_there_are_no_users_in_project(self):
        tasks.sync_remote_project_permissions()

        self.assertEqual(self.client.create_project.call_count, 0)

    def test_project_is_not_created_if_there_are_no_valid_resources(self):
        self.fixture.manager
        self.resource.state = models.Resource.States.TERMINATED
        self.resource.save()

        tasks.sync_remote_project_permissions()

        self.assertEqual(self.client.create_project.call_count, 0)

    def test_project_is_not_created_if_there_are_no_eduteams_users(self):
        self.fixture.manager

        tasks.sync_remote_project_permissions()

        self.assertEqual(self.client.create_project.call_count, 0)

    def test_project_is_created_if_it_does_not_exist_yet(self):
        # Arrange
        self.fixture.manager.registration_method = 'eduteams'
        self.fixture.manager.save()

        self.client.list_projects.return_value = []
        self.client.create_project.return_value = {'uuid': self.remote_project_uuid}
        self.client.get_remote_eduteams_user.return_value = {
            'uuid': self.remote_user_uuid
        }
        self.client.get_project_permissions.return_value = []

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.client.create_project.assert_called_once()

    def test_project_is_not_created_if_it_already_exists(self):
        # Arrange
        self.fixture.manager.registration_method = 'eduteams'
        self.fixture.manager.save()

        self.client.list_projects.return_value = [{'uuid': self.remote_project_uuid}]
        self.client.get_remote_eduteams_user.return_value = {
            'uuid': self.remote_user_uuid
        }
        self.client.get_project_permissions.return_value = []

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertEqual(self.client.create_project.call_count, 0)

    def test_project_permission_is_created_if_it_does_not_exist_yet(self):
        # Arrange
        self.fixture.manager.registration_method = 'eduteams'
        self.fixture.manager.save()

        self.client.list_projects.return_value = [{'uuid': self.remote_project_uuid}]
        self.client.get_remote_eduteams_user.return_value = {
            'uuid': self.remote_user_uuid
        }
        self.client.get_project_permissions.return_value = []

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.client.create_project_permission.assert_called_once_with(
            self.remote_user_uuid, self.remote_project_uuid, 'manager', None
        )

    def test_project_permission_is_not_created_if_it_already_exists(self):
        # Arrange
        self.fixture.manager.registration_method = 'eduteams'
        self.fixture.manager.save()

        self.client.list_projects.return_value = [{'uuid': self.remote_project_uuid}]
        self.client.get_remote_eduteams_user.return_value = {
            'uuid': self.remote_user_uuid
        }
        self.client.get_project_permissions.return_value = [
            {
                'expiration_time': None,
                'role': 'manager',
                'user_username': self.fixture.manager.username,
                'pk': 1,
            }
        ]

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertEqual(self.client.create_project_permission.call_count, 0)

    def test_project_permission_is_updated_if_expiration_time_differs(self):
        # Arrange
        self.fixture.manager.registration_method = 'eduteams'
        self.fixture.manager.save()

        self.client.list_projects.return_value = [{'uuid': self.remote_project_uuid}]
        self.client.get_remote_eduteams_user.return_value = {
            'uuid': self.remote_user_uuid
        }
        self.client.get_project_permissions.return_value = [
            {
                'expiration_time': timezone.now().isoformat(),
                'role': 'manager',
                'user_username': self.fixture.manager.username,
                'pk': 1,
            }
        ]

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.client.update_project_permission.assert_called_once_with('1', None)

    def test_project_permission_is_updated_if_role_differs(self):
        # Arrange
        self.fixture.manager.registration_method = 'eduteams'
        self.fixture.manager.save()

        self.client.list_projects.return_value = [{'uuid': self.remote_project_uuid}]
        self.client.get_remote_eduteams_user.return_value = {
            'uuid': self.remote_user_uuid
        }
        self.client.get_project_permissions.return_value = [
            {
                'expiration_time': timezone.now().isoformat(),
                'role': 'admin',
                'user_username': self.fixture.manager.username,
                'pk': 1,
            }
        ]

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.client.remove_project_permission.assert_called_once_with('1')
        self.client.create_project_permission.assert_called_once_with(
            self.remote_user_uuid, self.remote_project_uuid, 'manager', None
        )

    def test_if_user_is_owner_and_admin_then_manager_role_is_created(self):
        # Arrange
        self.fixture.admin.registration_method = 'eduteams'
        self.fixture.admin.save()
        self.fixture.customer.add_user(self.fixture.admin, CustomerRole.OWNER)

        self.client.list_projects.return_value = [{'uuid': self.remote_project_uuid}]
        self.client.get_remote_eduteams_user.return_value = {
            'uuid': self.remote_user_uuid
        }
        self.client.get_project_permissions.return_value = []

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.client.create_project_permission.assert_called_once_with(
            self.remote_user_uuid, self.remote_project_uuid, 'manager', None
        )

    def test_skip_mapping_for_owners_if_offering_belongs_to_the_same_customer(self):
        # Arrange
        self.fixture.owner.registration_method = 'eduteams'
        self.fixture.owner.save()

        self.resource.project.customer = self.fixture.resource.offering.customer
        self.resource.project.save()

        self.client.list_projects.return_value = [{'uuid': self.remote_project_uuid}]
        self.client.get_remote_eduteams_user.return_value = {
            'uuid': self.remote_user_uuid
        }
        self.client.get_project_permissions.return_value = []

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertEqual(self.client.create_project_permission.call_count, 0)
