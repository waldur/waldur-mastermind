import uuid
from unittest import mock

from django.core.exceptions import ObjectDoesNotExist
from django.test import override_settings
from django.utils import timezone
from rest_framework import test

from waldur_core.core.utils import serialize_instance
from waldur_core.structure.models import CustomerRole
from waldur_core.structure.tests.factories import ProjectFactory, UserFactory
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests.utils import create_system_robot
from waldur_mastermind.marketplace_remote import PLUGIN_NAME, tasks, utils


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


class DeleteRemoteProjectsTest(test.APITransactionTestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.backend_id = f'{self.project.customer.uuid}_{self.project.uuid}'
        self.offering = factories.OfferingFactory(
            type=PLUGIN_NAME, state=models.Offering.States.ACTIVE
        )
        self.offering.secret_options = {'api_url': 'api_url', 'token': 'token'}
        self.offering.save()

    @mock.patch('waldur_mastermind.marketplace_remote.tasks.WaldurClient')
    def test_clean_remote_projects(self, mock_client):
        self.project.delete()

        mock_client().list_projects.return_value = [
            {'backend_id': self.backend_id, 'uuid': '7f906264d0704af1b6589c65269e4357'}
        ]
        tasks.clean_remote_projects()
        mock_client().delete_project.assert_called_once_with(
            '7f906264d0704af1b6589c65269e4357'
        )

    @mock.patch('waldur_mastermind.marketplace_remote.tasks.delete_remote_project')
    def test_handler(self, mock_task):
        serialized_project = serialize_instance(self.project)
        self.project.delete()
        mock_task.delay.assert_called_once_with(serialized_project)

    @mock.patch('waldur_mastermind.marketplace_remote.tasks.WaldurClient')
    def test_delete_remote_project(self, mock_client):
        factories.ResourceFactory(offering=self.offering, project=self.project)
        mock_client().list_projects.return_value = [
            {'backend_id': self.backend_id, 'uuid': '7f906264d0704af1b6589c65269e4357'}
        ]
        serialized_project = serialize_instance(self.project)
        self.project.delete()
        tasks.delete_remote_project(serialized_project)
        mock_client().delete_project.assert_called_once_with(
            '7f906264d0704af1b6589c65269e4357'
        )


class OfferingUserPullTest(test.APITransactionTestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory(
            secret_options={'api_url': 'api_url', 'token': 'token'}
        )

    @mock.patch('waldur_mastermind.marketplace_remote.utils.WaldurClient')
    def test_offering_user_is_skipped_if_there_is_no_user_in_local_db(
        self, mock_client
    ):
        mock_client().list_remote_offering_users.return_value = [
            {'user_username': 'alice@myaccessid.org', 'username': 'alice'}
        ]
        tasks.OfferingUserPullTask().pull(self.offering)

    @mock.patch('waldur_mastermind.marketplace_remote.utils.WaldurClient')
    def test_missing_offering_user_is_created_if_there_is_user_in_local_db(
        self, mock_client
    ):
        user = UserFactory(username='alice@myaccessid.org')
        mock_client().list_remote_offering_users.return_value = [
            {'user_username': 'alice@myaccessid.org', 'username': 'alice'}
        ]
        tasks.OfferingUserPullTask().pull(self.offering)
        self.assertEqual(
            models.OfferingUser.objects.get(user=user, offering=self.offering).username,
            'alice',
        )

    @mock.patch('waldur_mastermind.marketplace_remote.utils.WaldurClient')
    def test_stale_offering_user_is_deleted(self, mock_client):
        user = UserFactory(username='alice@myaccessid.org')
        offering_user = models.OfferingUser.objects.create(
            user=user, offering=self.offering, username='alice'
        )
        mock_client().list_remote_offering_users.return_value = []
        tasks.OfferingUserPullTask().pull(self.offering)
        self.assertRaises(ObjectDoesNotExist, offering_user.refresh_from_db)

    @mock.patch('waldur_mastermind.marketplace_remote.utils.WaldurClient')
    def test_existing_offering_user_is_updated(self, mock_client):
        user = UserFactory(username='alice@myaccessid.org')
        offering_user = models.OfferingUser.objects.create(
            user=user, offering=self.offering, username='bob'
        )
        mock_client().list_remote_offering_users.return_value = [
            {'user_username': 'alice@myaccessid.org', 'username': 'alice'}
        ]
        tasks.OfferingUserPullTask().pull(self.offering)
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, 'alice')


class ResourceOrderItemImportTest(test.APITransactionTestCase):
    def setUp(self):
        self.patcher = mock.patch(
            "waldur_mastermind.marketplace_remote.utils.WaldurClient"
        )
        self.client = self.patcher.start()()

        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.backend_id = uuid.uuid4().hex
        self.resource.save()
        self.resource.offering.type = PLUGIN_NAME
        self.resource.offering.secret_options = {
            'api_url': 'https://example.com/',
            'token': uuid.uuid4().hex,
        }
        self.resource.offering.save()
        create_system_robot()

    def tearDown(self):
        super(ResourceOrderItemImportTest, self).tearDown()
        mock.patch.stopall()

    def test_when_there_are_no_order_items(self):
        self.client.list_order_items.return_value = []
        actual = utils.import_resource_order_items(self.resource)
        self.assertEqual([], actual)

    def test_there_is_one_order_item(self):
        remote_order_item_uuid = uuid.uuid4().hex
        remote_order_uuid = uuid.uuid4().hex
        self.client.list_order_items.return_value = [
            {'uuid': remote_order_item_uuid, 'order_uuid': remote_order_uuid}
        ]
        self.client.get_order.return_value = {
            'uuid': remote_order_item_uuid,
            'state': 'done',
            'created': '2021-12-12T01:01:01',
            'created_by_username': 'alice',
            'items': [
                {
                    'uuid': remote_order_item_uuid,
                    'type': 'Terminate',
                    'created': '2021-12-12T01:01:01',
                    'state': 'done',
                },
            ],
        }
        actual = utils.import_resource_order_items(self.resource)
        self.assertEqual(1, len(actual))
        self.assertEqual(actual[0].backend_id, remote_order_uuid)

    def test_existing_order_item_is_skipped(self):
        remote_order_item_uuid = uuid.uuid4().hex
        remote_order_uuid = uuid.uuid4().hex
        factories.OrderItemFactory(
            backend_id=remote_order_uuid, resource=self.fixture.resource
        )
        self.client.list_order_items.return_value = [
            {'uuid': remote_order_item_uuid, 'order_uuid': remote_order_uuid}
        ]
        self.client.get_order.return_value = {
            'uuid': remote_order_item_uuid,
            'state': 'done',
            'created': '2021-12-12T01:01:01',
            'created_by_username': 'alice',
            'items': [
                {
                    'uuid': remote_order_item_uuid,
                    'type': 'Terminate',
                    'created': '2021-12-12T01:01:01',
                    'state': 'done',
                },
            ],
        }
        actual = utils.import_resource_order_items(self.resource)
        self.assertEqual(0, len(actual))

    def test_resource_state(self):
        self.client.get_marketplace_resource.return_value = {'state': 'Erred'}
        utils.pull_resource_state(self.fixture.resource)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.fixture.resource.state, models.Resource.States.ERRED)

    def test_remote_resource_backend_id_is_saved_as_local_resource_effective_id(self):
        # Arrange
        self.fixture.resource.state = models.Resource.States.OK
        self.fixture.resource.save()
        self.client.get_marketplace_resource.return_value = {
            'report': '',
            'backend_id': 'effective_id',
            'state': 'OK',
        }

        # Act
        tasks.ResourcePullTask().pull(self.resource)

        # Assert
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.fixture.resource.effective_id, 'effective_id')
