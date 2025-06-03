import uuid
from unittest import mock

from django.core import mail
from django.core.exceptions import ObjectDoesNotExist
from django.test import override_settings, testcases
from django.utils import timezone

import respx
from waldur_auth_social.models import ProviderChoices
from waldur_core.core.enums import ReviewStates
from waldur_core.core.utils import format_text, serialize_instance
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests.factories import (
    NotificationFactory,
    ProjectFactory,
    UserFactory,
)
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import OfferingStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace_remote import PLUGIN_NAME, tasks, utils
from waldur_mastermind.marketplace_remote.models import ProjectUpdateRequest
from waldur_mastermind.marketplace_remote.tests.utils import get_request_data


@override_settings(WALDUR_AUTH_SOCIAL={"ENABLE_EDUTEAMS_SYNC": True})
class SyncRemoteProjectPermissionsTest(testcases.TransactionTestCase):
    def setUp(self):
        respx.start()
        self.remote_customer_uuid = uuid.uuid4().hex
        self.remote_project_uuid = uuid.uuid4().hex
        self.remote_user_uuid = uuid.uuid4().hex
        remote_api_token = uuid.uuid4().hex

        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.resource.offering.type = PLUGIN_NAME
        self.api_url = "https://example.com"
        self.resource.offering.secret_options = {
            "api_url": self.api_url,
            "token": remote_api_token,
            "customer_uuid": self.remote_customer_uuid,
        }
        self.resource.offering.save()

    def tearDown(self):
        respx.stop()
        super().tearDown()
        mock.patch.stopall()

    def mock_project_exists(self, exists=False):
        projects = [{"uuid": self.remote_project_uuid}] if exists else []
        respx.get(f"{self.api_url}/api/projects/").respond(200, json=projects)

    def mock_user_creation(self):
        respx.post(f"{self.api_url}/api/remote-eduteams/").respond(
            200, json={"uuid": self.remote_user_uuid}
        )

    def mock_permissions(self, permissions=None):
        if permissions is None:
            permissions = []
        respx.get(
            f"{self.api_url}/api/projects/{self.remote_project_uuid}/list_users/"
        ).respond(200, json=permissions)

    def mock_create_permission(self):
        return respx.post(
            f"{self.api_url}/api/projects/{self.remote_project_uuid}/add_user/"
        ).respond(201, json={"expiration_time": None})

    def mock_update_permission(self):
        return respx.post(
            f"{self.api_url}/api/projects/{self.remote_project_uuid}/update_user/"
        ).respond(200, json={"expiration_time": None})

    def mock_delete_permission(self):
        return respx.post(
            f"{self.api_url}/api/projects/{self.remote_project_uuid}/delete_user/"
        ).respond(200)

    def mock_project_creation(self):
        return respx.post(f"{self.api_url}/api/projects/").respond(
            201, json={"uuid": self.remote_project_uuid}
        )

    def test_project_is_not_created_if_there_are_no_users_in_project(self):
        self.mock_project_exists(True)
        self.mock_permissions()
        router = self.mock_project_creation()
        tasks.sync_remote_project_permissions()
        self.assertFalse(router.called)

    def test_project_is_not_created_if_there_are_no_valid_resources(self):
        self.fixture.manager
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()

        router = self.mock_project_creation()
        tasks.sync_remote_project_permissions()
        self.assertFalse(router.called)

    def test_project_is_not_created_if_there_are_no_eduteams_users(self):
        respx.get(f"{self.api_url}/api/projects/").respond(200, json=[])

        self.fixture.manager

        router = self.mock_project_creation()
        tasks.sync_remote_project_permissions()
        self.assertFalse(router.called)

    def test_project_is_created_if_it_does_not_exist_yet(self):
        # Arrange
        self.fixture.manager.registration_method = ProviderChoices.EDUTEAMS
        self.fixture.manager.save()

        self.mock_project_exists(exists=False)
        project_creation_mock = self.mock_project_creation()
        self.mock_user_creation()
        self.mock_permissions()
        self.mock_create_permission()

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertTrue(project_creation_mock.called)

    def test_project_is_not_created_if_it_already_exists(self):
        # Arrange
        self.fixture.manager.registration_method = ProviderChoices.EDUTEAMS
        self.fixture.manager.save()

        self.mock_project_exists(exists=True)
        self.mock_user_creation()
        self.mock_permissions()
        self.mock_create_permission()
        router = self.mock_project_creation()

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertFalse(router.called)

    def test_project_permission_is_created_if_it_does_not_exist_yet(self):
        # Arrange
        self.fixture.manager.registration_method = ProviderChoices.EDUTEAMS
        self.fixture.manager.save()

        self.mock_project_exists(exists=True)
        self.mock_user_creation()
        self.mock_permissions()
        create_permission_mock = self.mock_create_permission()

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertEqual(
            get_request_data(create_permission_mock),
            {
                "user": self.remote_user_uuid,
                "role": RoleEnum.PROJECT_MANAGER.value,
                "expiration_time": None,
            },
        )

    def test_project_permission_is_not_created_if_it_already_exists(self):
        # Arrange
        self.fixture.manager.registration_method = ProviderChoices.EDUTEAMS
        self.fixture.manager.save()

        self.mock_project_exists(exists=True)
        self.mock_user_creation()
        self.mock_permissions(
            permissions=[
                {
                    "expiration_time": None,
                    "role_name": RoleEnum.PROJECT_MANAGER,
                    "user_username": self.fixture.manager.username,
                    "user_uuid": self.remote_user_uuid,
                }
            ]
        )
        router = self.mock_create_permission()

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertFalse(router.called)

    def test_project_permission_is_updated_if_expiration_time_differs(self):
        # Arrange
        self.fixture.manager.registration_method = ProviderChoices.EDUTEAMS
        self.fixture.manager.save()

        self.mock_project_exists(exists=True)
        self.mock_user_creation()
        self.mock_permissions(
            permissions=[
                {
                    "expiration_time": timezone.now().isoformat(),
                    "role_name": RoleEnum.PROJECT_MANAGER,
                    "user_username": self.fixture.manager.username,
                    "user_uuid": self.remote_user_uuid,
                }
            ]
        )
        update_mock = self.mock_update_permission()

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertEqual(
            get_request_data(update_mock),
            {
                "user": self.remote_user_uuid,
                "role": RoleEnum.PROJECT_MANAGER.value,
                "expiration_time": None,
            },
        )

    def test_project_permission_is_updated_if_role_differs(self):
        # Arrange
        self.fixture.manager.registration_method = ProviderChoices.EDUTEAMS
        self.fixture.manager.save()

        self.mock_project_exists(exists=True)
        self.mock_user_creation()
        self.mock_permissions(
            permissions=[
                {
                    "expiration_time": timezone.now().isoformat(),
                    "role_name": RoleEnum.PROJECT_ADMIN,
                    "user_username": self.fixture.manager.username,
                    "user_uuid": self.remote_user_uuid,
                }
            ]
        )
        delete_mock = self.mock_delete_permission()
        create_mock = self.mock_create_permission()

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertEqual(
            get_request_data(delete_mock),
            {
                "user": self.remote_user_uuid,
                "role": RoleEnum.PROJECT_ADMIN.value,
            },
        )
        self.assertEqual(
            get_request_data(create_mock),
            {
                "user": self.remote_user_uuid,
                "role": RoleEnum.PROJECT_MANAGER.value,
                "expiration_time": None,
            },
        )

    def test_if_user_is_owner_and_admin_then_manager_role_is_created(self):
        # Arrange
        self.fixture.admin.registration_method = ProviderChoices.EDUTEAMS
        self.fixture.admin.save()
        self.fixture.customer.add_user(self.fixture.admin, CustomerRole.OWNER)

        self.mock_project_exists(exists=True)
        self.mock_user_creation()
        self.mock_permissions()
        create_mock = self.mock_create_permission()

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertEqual(
            get_request_data(create_mock),
            {
                "user": self.remote_user_uuid,
                "role": RoleEnum.PROJECT_MANAGER.value,
                "expiration_time": None,
            },
        )

    def test_skip_mapping_for_owners_if_offering_belongs_to_the_same_customer(self):
        # Arrange
        self.fixture.owner.registration_method = ProviderChoices.EDUTEAMS
        self.fixture.owner.save()

        self.resource.project.customer = self.fixture.resource.offering.customer
        self.resource.project.save()

        self.mock_project_exists(exists=True)
        self.mock_user_creation()
        self.mock_permissions()
        create_mock = self.mock_create_permission()

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertFalse(create_mock.called)

    def test_project_permission_is_deleted_if_it_is_absent_in_local_database(self):
        # Arrange
        self.fixture.manager.registration_method = ProviderChoices.EDUTEAMS
        self.fixture.manager.save()

        self.mock_create_permission()
        self.mock_project_exists(exists=True)
        self.mock_user_creation()
        self.mock_permissions(
            permissions=[
                {
                    "expiration_time": timezone.now().isoformat(),
                    "role_name": RoleEnum.PROJECT_ADMIN.value,
                    "user_username": self.fixture.manager.username,
                    "user_uuid": self.remote_user_uuid,
                }
            ]
        )
        delete_mock = self.mock_delete_permission()

        # Act
        tasks.sync_remote_project_permissions()

        # Assert
        self.assertTrue(delete_mock.called)
        self.assertEqual(
            get_request_data(delete_mock),
            {
                "user": self.remote_user_uuid,
                "role": RoleEnum.PROJECT_ADMIN.value,
            },
        )


class DeleteRemoteProjectsTest(testcases.TransactionTestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.backend_id = f"{self.project.customer.uuid}_{self.project.uuid}"
        self.api_url = "http://example.com"
        self.offering = factories.OfferingFactory(
            type=PLUGIN_NAME,
            state=OfferingStates.ACTIVE,
            secret_options={"api_url": self.api_url, "token": "token"},
        )
        self.remote_project_uuid = uuid.uuid4().hex

    @respx.mock
    def test_clean_remote_projects(self):
        respx.get(f"{self.api_url}/api/projects/").respond(
            200,
            json=[{"backend_id": self.backend_id, "uuid": self.remote_project_uuid}],
        )

        delete_mock = respx.delete(
            f"{self.api_url}/api/projects/{self.remote_project_uuid}/"
        ).respond(204)

        self.project.delete()

        tasks.clean_remote_projects()

        self.assertTrue(delete_mock.called)

    @mock.patch("waldur_mastermind.marketplace_remote.tasks.delete_remote_project")
    def test_handler(self, mock_task):
        serialized_project = serialize_instance(self.project)
        self.project.delete()
        mock_task.delay.assert_called_once_with(serialized_project)

    @respx.mock
    def test_delete_remote_project(self):
        factories.ResourceFactory(offering=self.offering, project=self.project)
        serialized_project = serialize_instance(self.project)
        self.project.delete()

        respx.get(f"{self.api_url}/api/projects/").respond(
            200,
            json=[{"backend_id": self.backend_id, "uuid": self.remote_project_uuid}],
        )
        delete_mock = respx.delete(
            f"{self.api_url}/api/projects/{self.remote_project_uuid}/"
        ).respond(204)

        tasks.delete_remote_project(serialized_project)
        self.assertTrue(delete_mock.called)


class OfferingUserPullTest(testcases.TransactionTestCase):
    def setUp(self):
        self.api_url = "http://example.com"
        self.offering = factories.OfferingFactory(
            secret_options={"api_url": self.api_url, "token": "token"}
        )
        respx.start()

    def tearDown(self):
        respx.stop()
        super().tearDown()

    def mock_offering_users(self, users):
        respx.get(f"{self.api_url}/api/marketplace-offering-users/").respond(
            200, json=users
        )

    def test_offering_user_is_skipped_if_there_is_no_user_in_local_db(self):
        self.mock_offering_users(
            [{"user_username": "alice@myaccessid.org", "username": "alice"}]
        )
        tasks.OfferingUserPullTask().pull(self.offering)

    def test_missing_offering_user_is_created_if_there_is_user_in_local_db(self):
        user = UserFactory(username="alice@myaccessid.org")
        self.mock_offering_users(
            [{"user_username": "alice@myaccessid.org", "username": "alice"}]
        )
        tasks.OfferingUserPullTask().pull(self.offering)
        self.assertEqual(
            models.OfferingUser.objects.get(user=user, offering=self.offering).username,
            "alice",
        )

    def test_stale_offering_user_is_deleted(self):
        self.mock_offering_users([])
        user = UserFactory(username="alice@myaccessid.org")
        offering_user = models.OfferingUser.objects.create(
            user=user, offering=self.offering, username="alice"
        )
        tasks.OfferingUserPullTask().pull(self.offering)
        self.assertRaises(ObjectDoesNotExist, offering_user.refresh_from_db)

    def test_existing_offering_user_is_updated(self):
        user = UserFactory(username="alice@myaccessid.org")
        offering_user = models.OfferingUser.objects.create(
            user=user, offering=self.offering, username="bob"
        )
        self.mock_offering_users(
            [{"user_username": "alice@myaccessid.org", "username": "alice"}]
        )
        tasks.OfferingUserPullTask().pull(self.offering)
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "alice")


class ResourceOrderImportTest(testcases.TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.backend_id = uuid.uuid4().hex
        self.resource.save()
        self.resource.offering.type = PLUGIN_NAME
        self.api_url = "https://example.com"
        self.resource.offering.secret_options = {
            "api_url": self.api_url,
            "token": uuid.uuid4().hex,
        }
        self.resource.offering.save()
        respx.start()

    def tearDown(self):
        respx.stop()
        super().tearDown()
        mock.patch.stopall()

    def mock_marketplace_orders(self, orders):
        respx.get(f"{self.api_url}/api/marketplace-orders/").respond(200, json=orders)

    def mock_marketplace_order(self, order_uuid, order_data):
        respx.get(f"{self.api_url}/api/marketplace-orders/{order_uuid}/").respond(
            200, json=order_data
        )

    def mock_marketplace_resource(self, resource_uuid, resource_data):
        respx.get(f"{self.api_url}/api/marketplace-resources/{resource_uuid}/").respond(
            200, json=resource_data
        )

    def mock_marketplace_resource_details(self, resource_uuid, resource_data):
        respx.get(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/details/"
        ).respond(200, json=resource_data)

    def test_when_there_are_no_orders(self):
        self.mock_marketplace_orders([])
        actual = utils.import_resource_orders(self.resource)
        self.assertEqual([], actual)

    def test_there_is_one_order(self):
        remote_order_uuid = uuid.uuid4().hex
        order_data = {
            "uuid": remote_order_uuid,
            "state": "done",
            "created": "2021-12-12T01:01:01",
            "created_by_username": "alice",
            "type": "Terminate",
        }
        self.mock_marketplace_orders(
            [{"uuid": remote_order_uuid, "order_uuid": remote_order_uuid}]
        )
        self.mock_marketplace_order(remote_order_uuid, order_data)

        actual = utils.import_resource_orders(self.resource)
        self.assertEqual(1, len(actual))
        self.assertEqual(actual[0].backend_id, remote_order_uuid)

    def test_existing_order_is_skipped(self):
        remote_order_uuid = uuid.uuid4().hex
        factories.OrderFactory(
            backend_id=remote_order_uuid, resource=self.fixture.resource
        )
        order_data = {
            "uuid": remote_order_uuid,
            "state": "done",
            "created": "2021-12-12T01:01:01",
            "created_by_username": "alice",
            "type": "Terminate",
        }
        self.mock_marketplace_orders(
            [{"uuid": remote_order_uuid, "order_uuid": remote_order_uuid}]
        )
        self.mock_marketplace_order(remote_order_uuid, order_data)
        actual = utils.import_resource_orders(self.resource)
        self.assertEqual(0, len(actual))

    @respx.mock
    def test_resource_state(self):
        resource_uuid = self.resource.backend_id
        self.mock_marketplace_resource(resource_uuid, {"state": "Erred"})
        utils.pull_resource_state(self.fixture.resource)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.fixture.resource.state, ResourceStates.ERRED)

    @respx.mock
    def test_remote_resource_backend_id_is_saved_as_local_resource_effective_id(self):
        # Arrange
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()
        resource_uuid = self.resource.backend_id

        respx.get(
            f"{self.api_url}/api/marketplace-orders/?field=uuid&resource_uuid={resource_uuid}"
        ).respond(200, json=[])

        respx.post(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/update_options/"
        ).respond(200, json={})

        self.mock_marketplace_resource(
            resource_uuid,
            {
                "report": "",
                "backend_id": "effective_id",
                "state": "OK",
                "attributes": {"sample_attr": 1},
                "options": {},
            },
        )

        # Act
        tasks.ResourcePullTask().pull(self.resource)

        # Assert
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.fixture.resource.effective_id, "effective_id")
        self.assertEqual(self.fixture.resource.attributes, {"sample_attr": 1})


class NotificationAboutPendingProjectUpdatesTest(testcases.TransactionTestCase):
    def setUp(self):
        from datetime import datetime, timedelta

        project_fixture = ProjectFixture()
        fixture = fixtures.MarketplaceFixture()
        self.owner = project_fixture.owner
        self.project = project_fixture.project
        self.offering = fixture.offering

        self.week_ago = datetime.now() - timedelta(weeks=1)

    def test_send_notify_if_week_old_pending_project_update_exists(self):
        ProjectUpdateRequest.objects.create(
            project=self.project,
            offering=self.offering,
            old_name="old name",
            new_name="new name",
            state=ReviewStates.PENDING,
            created=self.week_ago,
        )

        event_type = "notification_about_pending_project_updates"
        NotificationFactory(key=f"marketplace_remote.{event_type}")
        tasks.notify_about_pending_project_update_requests()
        self.assertEqual(len(mail.outbox), 1)
        subject_template_name = "{}/{}_subject.txt".format(
            "marketplace_remote",
            "notification_about_pending_project_updates",
        )
        subject = format_text(subject_template_name, {})
        self.assertEqual(mail.outbox[0].subject, subject)
        self.assertEqual(mail.outbox[0].to[0], self.owner.email)
        self.assertTrue(self.project.name in mail.outbox[0].body)

    def test_do_not_send_notify_if_pending_project_update_is_recent(self):
        ProjectUpdateRequest.objects.create(
            project=self.project,
            offering=self.offering,
            old_name="old name",
            new_name="new name",
            state=ReviewStates.PENDING,
        )

        event_type = "notification_about_pending_project_updates"
        NotificationFactory(key=f"marketplace_remote.{event_type}")
        tasks.notify_about_pending_project_update_requests()
        self.assertEqual(len(mail.outbox), 0)


class NotificationAboutProjectUpdatesTest(testcases.TransactionTestCase):
    def setUp(self):
        project_fixture = ProjectFixture()
        fixture = fixtures.MarketplaceFixture()
        self.owner = project_fixture.owner
        self.project = project_fixture.project
        self.offering = fixture.offering

    def test_send_notify_if_project_details_updated(self):
        project_update = ProjectUpdateRequest.objects.create(
            project=self.project,
            offering=self.offering,
            old_name="old name",
            new_name="new_name",
            state=ReviewStates.PENDING,
            created_by=self.owner,
            reviewed_by=self.owner,
        )

        project_update.state = ReviewStates.APPROVED
        project_update.save()

        serialized_project = serialize_instance(project_update)

        event_type = "notification_about_project_details_update"
        NotificationFactory(key=f"marketplace_remote.{event_type}")
        tasks.notify_about_project_details_update(serialized_project)
        self.assertEqual(len(mail.outbox), 2)


class OfferingListPullTaskTest(testcases.TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = PLUGIN_NAME
        self.api_url = "https://example.com"
        self.offering.secret_options = {
            "api_url": self.api_url,
            "token": "token",
        }
        self.offering.backend_id = uuid.uuid4().hex
        self.offering.save()

    @respx.mock
    @mock.patch("waldur_mastermind.marketplace_remote.tasks.logger")
    def test_archived_offering_does_not_raise_exception(self, mock_logger):
        """
        Test that archived offerings do not raise an exception when pulled.
        """
        respx.get(
            f"{self.api_url}/api/marketplace-public-offerings/{self.offering.backend_id}/"
        ).respond(404)

        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()

        pulled_objects = tasks.OfferingListPullTask().get_pulled_objects()

        # Check that the offering was pulled
        self.assertEqual(
            list(pulled_objects),
            [self.offering],
            f"Offering {self.offering.id} should be pulled but got: {pulled_objects}",
        )

        # Check that the pull call was made
        task = tasks.OfferingPullTask()
        task.pull(self.offering)

        # Check that the offering is still archived
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.state, OfferingStates.ARCHIVED)

        # Check that the logger was called with the correct arguments
        mock_logger.debug.assert_called_once_with(
            "Offering %s is archived: ", self.offering
        )

    @respx.mock
    @mock.patch("waldur_mastermind.marketplace_remote.tasks.logger")
    def test_active_offering_that_does_not_exist_raises_warning(self, mock_logger):
        """
        Test that active offerings that do not exist raise an exception when pulled.
        """
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        respx.get(
            f"{self.api_url}/api/marketplace-public-offerings/{self.offering.backend_id}/"
        ).respond(404)

        pulled_objects = tasks.OfferingListPullTask().get_pulled_objects()

        # Check that the offering was pulled
        self.assertEqual(
            list(pulled_objects),
            [self.offering],
            f"Offering {self.offering.id} should be pulled but got: {pulled_objects}",
        )

        # Check that the pull call was made
        task = tasks.OfferingPullTask()
        task.pull(self.offering)

        # Check that the offering is set to archived
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.state, OfferingStates.ARCHIVED)

        # Check that the logger was called with the correct arguments
        mock_logger.warning.assert_called_once()

    def test_active_offering_is_pulled(self):
        """
        Test that active offerings are pulled by remote pull task.
        """
        # Set offering to active state
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        # Pull offerings
        pulled_objects = tasks.OfferingListPullTask().get_pulled_objects()

        # Check that offering is pulled
        self.assertEqual(
            list(pulled_objects),
            [self.offering],
            f"{self.offering.id} should be pulled but got: {pulled_objects}",
        )


@override_settings(
    WALDUR_AUTH_SOCIAL={"ENABLE_EDUTEAMS_SYNC": True},
    task_always_eager=True,
    task_eager_propagates=True,
)
class OfferingUserPullTaskTest(testcases.TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = PLUGIN_NAME
        self.offering.backend_id = "test-backend-id"
        self.offering.secret_options = {
            "api_url": "https://example.com/",
            "token": "token",
        }
        self.offering.save()

        self.deactivated_user = UserFactory(is_active=False)
        self.offering_user = models.OfferingUser.objects.create(
            user=self.deactivated_user,
            offering=self.offering,
            username=self.deactivated_user.username,
        )

    @respx.mock
    def test_deactivated_user_handling_in_offering_user_pull(self):
        """
        Test that deactivated users are handled correctly during offering user pull,
        specifically that no KeyError is raised when a deactivated user is not in user_map
        """

        # Simulate a case that the user does not exist in remote portal
        respx.get("https://example.com/api/marketplace-offering-users/").respond(
            200, json=[]
        )

        # Pull offering users
        task = tasks.OfferingUserPullTask()
        task.pull(self.offering)

        # Assert that the offering user still exists
        offering_user = models.OfferingUser.objects.get(
            user=self.deactivated_user, offering=self.offering
        )
        self.assertIsNotNone(
            offering_user,
            "Deactivated user's offering access should have been preserved but was not",
        )

        # Verify that we can handle deactivated users that are not in user_map
        usernames = {self.deactivated_user.username}
        user_map = {
            user.username: user
            for user in models.User.objects.filter(username__in=usernames)
        }
        # Verify user is not in regular user_map (because it's deactivated)
        self.assertNotIn(
            self.deactivated_user.username,
            user_map,
            "Deactivated user should not be in regular user map",
        )
        # Verify we can still access the user through all_objects
        user = models.User.all_objects.get(username=self.deactivated_user.username)
        self.assertFalse(user.is_active, "User should be deactivated")
