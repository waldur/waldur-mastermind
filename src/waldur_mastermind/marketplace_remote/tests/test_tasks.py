import datetime
import json
import uuid
from decimal import Decimal
from unittest import mock

import respx
from django.core import mail
from django.core.exceptions import ObjectDoesNotExist
from django.db import connection
from django.test import override_settings, testcases
from django.test import utils as django_test
from django.utils import timezone
from freezegun import freeze_time

from waldur_auth_social.const import ProviderChoices
from waldur_core.core import models as core_models
from waldur_core.core.enums import ReviewStates
from waldur_core.core.utils import format_text, serialize_instance
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests.factories import (
    NotificationFactory,
    ProjectFactory,
    UserFactory,
)
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace_remote import tasks, utils
from waldur_mastermind.marketplace_remote.models import ProjectUpdateRequest
from waldur_mastermind.marketplace_remote.tests.utils import get_request_data
from waldur_mastermind.marketplace_remote.utils import INVALID_RESOURCE_STATES


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
        self.resource.offering.type = REMOTE_OFFERING
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

    def test_if_user_is_owner_and_admin_then_project_role_is_kept(self):
        # A user who is both an organization owner and a project admin keeps
        # their project-level role on the remote; owner status is not synced.
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
                "role": RoleEnum.PROJECT_ADMIN.value,
                "expiration_time": None,
            },
        )

    def test_organization_owner_is_not_synced(self):
        # Organization owners without a project-level role are not propagated
        # to remote Waldur instances.
        self.fixture.owner.registration_method = ProviderChoices.EDUTEAMS
        self.fixture.owner.save()

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


class UpdateRemoteProjectPermissionsTest(testcases.TransactionTestCase):
    """Test cases for update_remote_project_permissions task behavior."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.user = self.fixture.manager

        self.user.identity_source = ProviderChoices.EDUTEAMS
        self.user.registration_method = ProviderChoices.EDUTEAMS
        self.user.save()

    @override_settings(WALDUR_AUTH_SOCIAL={"ENABLE_EDUTEAMS_SYNC": True})
    def test_task_should_not_be_triggered_for_project_without_remote_resources(self):
        """
        Test that update_remote_project_permissions task is not triggered
        for projects that have no remote offering resources.
        """
        self.fixture.resource.offering.type = "SomeOtherOffering"
        self.fixture.resource.offering.save()

        remote_resources = models.Resource.objects.filter(
            project=self.project, offering__type=REMOTE_OFFERING
        )
        self.assertEqual(
            remote_resources.count(), 0, "Project should have no remote resources"
        )
        with mock.patch(
            "waldur_mastermind.marketplace_remote.tasks.update_remote_project_permissions.apply_async"
        ) as mock_task:
            self.project.add_user(self.user, ProjectRole.MANAGER)
            mock_task.assert_not_called()

    @override_settings(WALDUR_AUTH_SOCIAL={"ENABLE_EDUTEAMS_SYNC": True})
    def test_task_should_not_be_triggered_for_project_with_terminated_remote_resources(
        self,
    ):
        """
        Test that update_remote_project_permissions task is not triggered
        for projects that only have terminated remote offering resources.
        """
        # Ensure project has remote resources but they are terminated
        self.fixture.resource.offering.type = REMOTE_OFFERING
        self.fixture.resource.state = ResourceStates.TERMINATED
        self.fixture.resource.offering.save()
        self.fixture.resource.save()

        # Verify only terminated remote resources exist
        active_remote_resources = models.Resource.objects.filter(
            project=self.project, offering__type=REMOTE_OFFERING
        ).exclude(state__in=INVALID_RESOURCE_STATES)
        self.assertEqual(
            active_remote_resources.count(),
            0,
            "Project should have no active remote resources",
        )

        with mock.patch(
            "waldur_mastermind.marketplace_remote.tasks.update_remote_project_permissions.apply_async"
        ) as mock_task:
            self.project.add_user(self.user, ProjectRole.MANAGER)
            mock_task.assert_not_called()

    @override_settings(WALDUR_AUTH_SOCIAL={"ENABLE_EDUTEAMS_SYNC": True})
    def test_task_should_be_triggered_for_project_with_remote_resources(self):
        """
        Test that update_remote_project_permissions task IS triggered
        for projects that have remote offering resources.
        """
        self.fixture.resource.offering.type = REMOTE_OFFERING
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()
        self.fixture.resource.offering.save()

        remote_resources = models.Resource.objects.filter(
            project=self.project, offering__type=REMOTE_OFFERING
        )
        self.assertEqual(
            remote_resources.count(), 1, "Project should have remote resources"
        )

        with mock.patch(
            "waldur_mastermind.marketplace_remote.tasks.update_remote_project_permissions.apply_async"
        ) as mock_task:
            self.project.add_user(self.user, ProjectRole.MANAGER)

            mock_task.assert_called_once()

    def test_task_handles_soft_deleted_project_gracefully(self):
        """
        Test that update_remote_project_permissions task handles soft-deleted projects
        without raising DoesNotExist exceptions.
        """
        self.project.delete(soft=True)
        self.project.refresh_from_db()
        self.assertTrue(self.project.is_removed, "Project should be soft-deleted")

        serialized_project = serialize_instance(self.project)
        serialized_user = serialize_instance(self.user)

        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.sync_project_permission"
        ) as mock_sync:
            tasks.update_remote_project_permissions(
                serialized_project=serialized_project,
                serialized_user=serialized_user,
                role_name=RoleEnum.PROJECT_MANAGER.value,
                grant=True,
                expiration_time=None,
            )
            mock_sync.assert_not_called()

    def test_task_handles_hard_deleted_project_gracefully(self):
        """
        Test that update_remote_project_permissions task handles hard-deleted projects
        without raising DoesNotExist exceptions.
        """
        serialized_project = serialize_instance(self.project)
        serialized_user = serialize_instance(self.user)

        self.project.delete(soft=False)

        with mock.patch(
            "waldur_mastermind.marketplace_remote.tasks.sync_project_permission"
        ) as mock_sync:
            tasks.update_remote_project_permissions(
                serialized_project=serialized_project,
                serialized_user=serialized_user,
                role_name=RoleEnum.PROJECT_MANAGER.value,
                grant=True,
                expiration_time=None,
            )
            mock_sync.assert_not_called()

    def test_task_calls_sync_project_permission_for_valid_project(self):
        """
        Test that update_remote_project_permissions task calls sync_project_permission
        for valid (non-deleted) projects.
        """
        serialized_project = serialize_instance(self.project)
        serialized_user = serialize_instance(self.user)

        with mock.patch(
            "waldur_mastermind.marketplace_remote.tasks.sync_project_permission"
        ) as mock_sync:
            tasks.update_remote_project_permissions(
                serialized_project=serialized_project,
                serialized_user=serialized_user,
                role_name=RoleEnum.PROJECT_MANAGER.value,
                grant=True,
                expiration_time=None,
            )
            mock_sync.assert_called_once_with(
                True, self.project, RoleEnum.PROJECT_MANAGER.value, self.user, None
            )

    def test_task_logs_soft_deleted_project_specifically(self):
        """
        Test that the task logs specifically when a project is soft-deleted
        vs when it's completely missing.
        """
        self.project.is_removed = True
        self.project.save()

        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.logger"
        ) as mock_logger:
            tasks.update_remote_project_permissions(
                serialized_project=serialize_instance(self.project),
                serialized_user=serialize_instance(self.user),
                role_name=ProjectRole.MANAGER,
                grant=True,
            )

            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            fmt = call_args[0][0]
            args = call_args[0][1:]
            message = fmt % args

            self.assertIn("soft-deleted project", message)
            self.assertIn(f"project_id={self.project.pk}", message)
            self.assertIn(f"user_id={self.user.pk}", message)

    def test_task_logs_soft_deleted_user_specifically(self):
        """
        Test that the task logs specifically when a user is soft-deleted
        vs when they're completely missing.
        """
        serialized_user = serialize_instance(self.user)

        self.user.is_active = False
        self.user.save()

        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.logger"
        ) as mock_logger:
            tasks.update_remote_project_permissions(
                serialized_project=serialize_instance(self.project),
                serialized_user=serialized_user,
                role_name=ProjectRole.MANAGER,
                grant=True,
            )

            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            fmt = call_args[0][0]
            args = call_args[0][1:]
            message = fmt % args
            self.assertIn("inactive user", message)
            self.assertIn(f"project_id={self.project.pk}", message)
            self.assertIn(f"user_id={self.user.pk}", message)


class DeleteRemoteProjectsTest(testcases.TransactionTestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.backend_id = f"{self.project.customer.uuid}_{self.project.uuid}"
        self.api_url = "http://example.com"
        self.offering = factories.OfferingFactory(
            type=REMOTE_OFFERING,
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
            secret_options={"api_url": self.api_url, "token": "token"},
            backend_id=uuid.uuid4().hex,
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
        self.resource.offering.type = REMOTE_OFFERING
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
    def test_resource_pull_imports_orders_when_state_is_unchanged(self):
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()
        resource_uuid = self.resource.backend_id

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

        respx.post(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/update_options/"
        ).respond(200, json={"status": "ok"})

        with mock.patch(
            "waldur_mastermind.marketplace_remote.tasks.utils.import_resource_orders"
        ) as mocked_import:
            tasks.ResourcePullTask().pull(self.resource)

        mocked_import.assert_called_once_with(self.resource)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.fixture.resource.effective_id, "effective_id")

    @respx.mock
    def test_resource_pull_imports_orders_once_when_state_changes(self):
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()
        resource_uuid = self.resource.backend_id

        resource_route = respx.get(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/"
        ).respond(
            200,
            json={
                "report": "",
                "backend_id": "effective_id",
                "state": "Erred",
                "attributes": {"sample_attr": 1},
                "options": {},
            },
        )

        respx.post(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/update_options/"
        ).respond(200, json={"status": "ok"})

        with mock.patch(
            "waldur_mastermind.marketplace_remote.tasks.utils.import_resource_orders"
        ) as mocked_import:
            tasks.ResourcePullTask().pull(self.resource)

        mocked_import.assert_called_once_with(self.resource)
        self.assertEqual(resource_route.calls.call_count, 1)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.fixture.resource.state, ResourceStates.ERRED)

    @respx.mock
    def test_remote_resource_backend_id_is_saved_as_local_resource_effective_id(self):
        # Arrange
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()
        resource_uuid = self.resource.backend_id

        respx.get(
            f"{self.api_url}/api/marketplace-orders/",
            params={"field": "uuid", "resource_uuid": resource_uuid, "page_size": 100},
        ).respond(200, json=[])

        respx.post(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/update_options/"
        ).respond(200, json={"status": "ok"})

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


class ResourceEndDatePushTest(testcases.TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.backend_id = uuid.uuid4().hex
        self.resource.save()
        self.resource.offering.type = REMOTE_OFFERING
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

    @respx.mock
    @freeze_time("2025-01-01")
    def test_resource_end_date_is_pushed_to_remote(self):
        end_date = datetime.date(2025, 1, 15)
        canonical_uuid = str(uuid.UUID(self.resource.backend_id))
        patch_request = respx.patch(
            f"{self.api_url}/api/marketplace-resources/{canonical_uuid}/"
        ).respond(200, json={"uuid": canonical_uuid, "name": "resource"})

        self.resource.end_date = end_date
        self.resource.save()

        utils.push_resource_end_date(self.resource)

        self.assertTrue(patch_request.called)
        request_json = json.loads(patch_request.calls[0].request.content.decode())
        self.assertEqual(request_json["end_date"], end_date.isoformat())

    @respx.mock
    @freeze_time("2025-01-15")
    def test_reconcile_task_updates_remote_when_end_date_differs(self):
        local_end_date = datetime.date(2025, 2, 1)
        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.push_resource_end_date"
        ):
            self.resource.end_date = local_end_date
            self.resource.state = ResourceStates.OK
            self.resource.save()

        resource_uuid = str(uuid.UUID(self.resource.backend_id))

        respx.get(f"{self.api_url}/api/marketplace-resources/{resource_uuid}/").respond(
            200,
            json={
                "uuid": resource_uuid,
                "name": "resource",
                "end_date": "2025-01-01",
            },
        )
        patch_request = respx.patch(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/"
        ).respond(
            200,
            json={
                "uuid": resource_uuid,
                "name": "resource",
                "end_date": local_end_date.isoformat(),
            },
        )

        tasks.reconcile_resource_end_dates()

        self.assertTrue(patch_request.called)
        request_json = json.loads(patch_request.calls[0].request.content.decode())
        self.assertEqual(request_json["end_date"], local_end_date.isoformat())

    @respx.mock
    @freeze_time("2025-01-15")
    def test_reconcile_task_does_not_update_when_end_date_is_same(self):
        local_end_date = datetime.date(2025, 2, 1)
        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.push_resource_end_date"
        ):
            self.resource.end_date = local_end_date
            self.resource.state = ResourceStates.OK
            self.resource.save()

        canonical_uuid = str(uuid.UUID(self.resource.backend_id))

        respx.get(
            f"{self.api_url}/api/marketplace-resources/{canonical_uuid}/"
        ).respond(
            200,
            json={
                "uuid": canonical_uuid,
                "name": "resource",
                "end_date": local_end_date.isoformat(),
            },
        )
        patch_request = respx.patch(
            f"{self.api_url}/api/marketplace-resources/{canonical_uuid}/"
        ).respond(
            200,
            json={
                "uuid": canonical_uuid,
                "name": "resource",
                "end_date": local_end_date.isoformat(),
            },
        )

        tasks.reconcile_resource_end_dates()

        self.assertFalse(patch_request.called)

    @respx.mock
    @freeze_time("2025-03-01")
    def test_push_resource_end_date_skips_past_date(self):
        """push_resource_end_date should not push a date that is in the past."""
        past_date = datetime.date(2025, 2, 15)
        canonical_uuid = str(uuid.UUID(self.resource.backend_id))
        patch_request = respx.patch(
            f"{self.api_url}/api/marketplace-resources/{canonical_uuid}/"
        ).respond(200, json={"uuid": canonical_uuid, "name": "resource"})

        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.push_resource_end_date",
            wraps=utils.push_resource_end_date,
        ):
            self.resource.end_date = past_date
            self.resource.save()

        utils.push_resource_end_date(self.resource)

        self.assertFalse(patch_request.called)

    @respx.mock
    @freeze_time("2025-03-01")
    def test_reconcile_pulls_remote_date_when_local_is_past(self):
        """When local end_date is past and remote has a valid future date, pull it."""
        past_local_date = datetime.date(2025, 2, 15)
        future_remote_date = "2025-06-01"
        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.push_resource_end_date"
        ):
            self.resource.end_date = past_local_date
            self.resource.state = ResourceStates.OK
            self.resource.save()

        resource_uuid = str(uuid.UUID(self.resource.backend_id))

        respx.get(f"{self.api_url}/api/marketplace-resources/{resource_uuid}/").respond(
            200,
            json={
                "uuid": resource_uuid,
                "name": "resource",
                "end_date": future_remote_date,
            },
        )
        # Mock the events endpoint
        respx.get(f"{self.api_url}/api/events/").respond(200, json=[])

        # Mock push to avoid signal handler side effects
        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.push_resource_end_date"
        ):
            tasks.reconcile_resource_end_dates()

        # Should update local resource with remote date
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, datetime.date(2025, 6, 1))

    @respx.mock
    @freeze_time("2025-03-01")
    def test_reconcile_skips_push_when_local_past_and_remote_also_past(self):
        """When both local and remote dates are past, skip entirely."""
        past_local_date = datetime.date(2025, 2, 15)
        past_remote_date = "2025-02-10"
        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.push_resource_end_date"
        ):
            self.resource.end_date = past_local_date
            self.resource.state = ResourceStates.OK
            self.resource.save()

        resource_uuid = str(uuid.UUID(self.resource.backend_id))

        respx.get(f"{self.api_url}/api/marketplace-resources/{resource_uuid}/").respond(
            200,
            json={
                "uuid": resource_uuid,
                "name": "resource",
                "end_date": past_remote_date,
            },
        )
        patch_request = respx.patch(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/"
        ).respond(200, json={})

        tasks.reconcile_resource_end_dates()

        # Should NOT push to remote
        self.assertFalse(patch_request.called)
        # Should NOT update local resource
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, past_local_date)

    @respx.mock
    def test_reconcile_skips_terminated_resources(self):
        """TERMINATED resources should be excluded from reconciliation."""
        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.push_resource_end_date"
        ):
            self.resource.end_date = datetime.date(2025, 6, 1)
            self.resource.state = ResourceStates.TERMINATED
            self.resource.save()

        resource_uuid = str(uuid.UUID(self.resource.backend_id))
        get_request = respx.get(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/"
        ).respond(200, json={})

        tasks.reconcile_resource_end_dates()

        # Should not even fetch the remote resource
        self.assertFalse(get_request.called)

    @respx.mock
    @freeze_time("2025-03-01")
    @override_settings(task_always_eager=True)
    def test_reconcile_sends_notification_when_pulling_remote_date(self):
        """Notification email should be sent when pulling remote date."""
        past_local_date = datetime.date(2025, 2, 15)
        future_remote_date = "2025-06-01"
        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.push_resource_end_date"
        ):
            self.resource.end_date = past_local_date
            self.resource.state = ResourceStates.OK
            self.resource.save()

        resource_uuid = str(uuid.UUID(self.resource.backend_id))

        respx.get(f"{self.api_url}/api/marketplace-resources/{resource_uuid}/").respond(
            200,
            json={
                "uuid": resource_uuid,
                "name": "resource",
                "end_date": future_remote_date,
            },
        )
        respx.get(f"{self.api_url}/api/events/").respond(200, json=[])

        event_type = "resource_end_date_pulled_from_remote"
        NotificationFactory(key=f"marketplace_remote.{event_type}")

        # Grant APPROVE_ORDER permission to a role and assign user
        from waldur_core.permissions.enums import PermissionEnum
        from waldur_core.permissions.fixtures import ProjectRole

        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.resource.project.add_user(self.fixture.owner, ProjectRole.ADMIN)

        # Mock push to avoid signal handler side effects
        with mock.patch(
            "waldur_mastermind.marketplace_remote.utils.push_resource_end_date"
        ):
            tasks.reconcile_resource_end_dates()

        self.assertTrue(len(mail.outbox) > 0)


class ResourceEndDateTerminationGapTest(testcases.TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.backend_id = uuid.uuid4().hex
        self.resource.state = ResourceStates.OK
        self.resource.end_date = datetime.date(2025, 1, 1)
        self.resource.save()

    @freeze_time("2025-01-02")
    def test_terminate_resource_skips_when_recent_erred_terminate_order_exists(self):
        """Should not create duplicate TERMINATE orders when a recent one is ERRED."""
        from waldur_mastermind.marketplace import utils as marketplace_utils

        # Create a recent ERRED TERMINATE order
        factories.OrderFactory(
            resource=self.resource,
            project=self.resource.project,
            offering=self.resource.offering,
            type=OrderTypes.TERMINATE,
            state=OrderStates.ERRED,
        )

        user = self.fixture.staff

        result = marketplace_utils.terminate_resource(self.resource, user)

        self.assertIsNone(result)

    @freeze_time("2025-01-04")
    def test_terminate_resource_retries_after_old_erred_terminate_order(self):
        """Should allow retry when the ERRED TERMINATE order is older than 1 day."""
        from waldur_mastermind.marketplace import utils as marketplace_utils

        # Create an old ERRED TERMINATE order (more than 1 day ago)
        old_order = factories.OrderFactory(
            resource=self.resource,
            project=self.resource.project,
            offering=self.resource.offering,
            type=OrderTypes.TERMINATE,
            state=OrderStates.ERRED,
        )
        # Backdate the modified field
        models.Order.objects.filter(pk=old_order.pk).update(
            modified=timezone.now() - datetime.timedelta(days=2)
        )

        user = self.fixture.staff

        result = marketplace_utils.terminate_resource(self.resource, user)

        # Should have created a new order (returns Response)
        self.assertIsNotNone(result)


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
        self.offering.type = REMOTE_OFFERING
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


class OfferingUserListPullTaskTest(testcases.TransactionTestCase):
    def setUp(self):
        self.api_url = "http://example.com"
        self.offering_with_option = factories.OfferingFactory(
            type=REMOTE_OFFERING,
            secret_options={"api_url": self.api_url, "token": "token"},
            plugin_options={"service_provider_can_create_offering_user": True},
            backend_id=uuid.uuid4().hex,
        )
        self.offering_without_option = factories.OfferingFactory(
            type=REMOTE_OFFERING,
            secret_options={"api_url": self.api_url, "token": "token"},
            plugin_options={},  # Explicitly set empty dict to avoid JSONField issues
            backend_id=uuid.uuid4().hex,
        )

    def test_offering_with_service_provider_option_is_excluded(self):
        """
        Test that offerings with service_provider_can_create_offering_user=True
        are excluded from the pull task.
        """
        task = tasks.OfferingUserListPullTask()
        pulled_objects = list(task.get_pulled_objects())

        self.assertNotIn(
            self.offering_with_option,
            pulled_objects,
            "Offering with service_provider_can_create_offering_user=True should be excluded",
        )
        self.assertIn(
            self.offering_without_option,
            pulled_objects,
            "Offering without the option should be included",
        )

    def test_offering_with_false_option_is_included(self):
        """
        Test that offerings with service_provider_can_create_offering_user=False
        are included in the pull task.
        """
        self.offering_with_option.plugin_options = {
            "service_provider_can_create_offering_user": False
        }
        self.offering_with_option.save()

        task = tasks.OfferingUserListPullTask()
        pulled_objects = task.get_pulled_objects()

        self.assertIn(
            self.offering_with_option,
            pulled_objects,
            "Offering with service_provider_can_create_offering_user=False should be included",
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
        self.offering.type = REMOTE_OFFERING
        self.offering.backend_id = uuid.uuid4().hex
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

    @respx.mock
    def test_new_remote_users_are_created_locally(self):
        """Test that new users from remote are created as OfferingUser locally."""
        # Create a local user that exists in the remote system
        local_user = UserFactory()

        # Mock remote API to return this user
        respx.get("https://example.com/api/marketplace-offering-users/").respond(
            200,
            json=[
                {
                    "uuid": uuid.uuid4().hex,
                    "offering_uuid": self.offering.backend_id,
                    "user_uuid": uuid.uuid4().hex,
                    "user_username": local_user.username,
                    "username": "remote_username_for_user",
                }
            ],
        )

        task = tasks.OfferingUserPullTask()
        task.pull(self.offering)

        # Verify OfferingUser was created
        self.assertTrue(
            models.OfferingUser.objects.filter(
                user=local_user, offering=self.offering
            ).exists()
        )
        offering_user = models.OfferingUser.objects.get(
            user=local_user, offering=self.offering
        )
        self.assertEqual(offering_user.username, "remote_username_for_user")

    @respx.mock
    def test_stale_users_are_removed(self):
        """Test that users not in remote are removed locally."""
        # Create a local user with OfferingUser
        local_user = UserFactory()
        models.OfferingUser.objects.create(
            user=local_user,
            offering=self.offering,
            username="local_username",
        )

        # Mock remote API to return empty (user no longer exists remotely)
        respx.get("https://example.com/api/marketplace-offering-users/").respond(
            200, json=[]
        )

        task = tasks.OfferingUserPullTask()
        task.pull(self.offering)

        # Verify OfferingUser was removed
        self.assertFalse(
            models.OfferingUser.objects.filter(
                user=local_user, offering=self.offering
            ).exists()
        )

    @respx.mock
    def test_pull_with_multiple_users_uses_select_related(self):
        """
        Test that pulling multiple users doesn't cause N+1 queries.
        Fixes PUHURI-PORTALS-DX3.
        """
        # Create multiple local users with OfferingUser records
        users = [UserFactory() for _ in range(5)]
        for user in users:
            models.OfferingUser.objects.create(
                user=user,
                offering=self.offering,
                username=f"username_{user.username}",
            )

        # Mock remote API to return all users
        remote_users = [
            {
                "uuid": uuid.uuid4().hex,
                "offering_uuid": self.offering.backend_id,
                "user_uuid": uuid.uuid4().hex,
                "user_username": user.username,
                "username": f"username_{user.username}",
            }
            for user in users
        ]
        respx.get("https://example.com/api/marketplace-offering-users/").respond(
            200, json=remote_users
        )

        task = tasks.OfferingUserPullTask()

        # The task should complete without N+1 queries
        # With select_related("user"), fetching local_offering_users should be 1 query
        # Without it, it would be 1 + N queries (1 for OfferingUser, N for each User)
        task.pull(self.offering)

        # Verify all users still exist
        for user in users:
            self.assertTrue(
                models.OfferingUser.objects.filter(
                    user=user, offering=self.offering
                ).exists()
            )


@freeze_time("2024-01-01T00:00:00Z")
class UsagePullTest(testcases.TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.backend_id = uuid.uuid4().hex
        self.resource.save()
        self.resource.offering.type = REMOTE_OFFERING
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

    def mock_component_usages(self, usages):
        respx.get(
            f"{self.api_url}/api/marketplace-component-usages/",
            params={"resource_uuid": self.resource.backend_id},
        ).respond(200, json=usages)

    def mock_component_user_usages(self, usages):
        # Mock the upfront fetch of ALL user usages for the resource
        # This matches the optimized API call that fetches all user usages at once
        respx.get(
            f"{self.api_url}/api/marketplace-component-user-usages/",
            params={"resource_uuid": self.resource.backend_id},
        ).respond(200, json=usages)

    def test_component_usage_and_user_usage_are_created(self):
        models.OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="cpu_k_hours",
            name="CPU Hours",
        )

        usage_data = {
            "uuid": uuid.uuid4().hex,
            "type": "cpu_k_hours",
            "usage": 100,
            "description": "Test usage",
            "created": "2024-03-01T00:00:00Z",
            "date": "2024-04-01T00:00:00Z",
            "recurring": False,
            "billing_period": "2024-03-01",
        }
        user_usage_data = {
            "username": "test_user",
            "usage": 50,
            "component_type": "cpu_k_hours",
            "billing_period": "2024-03-01",  # Must match component usage billing_period for grouping
        }
        user = UserFactory(username="test_user")
        offering_user = models.OfferingUser.objects.create(
            offering=self.resource.offering,
            username="test_user",
            user=user,
        )
        self.mock_component_usages([usage_data])
        self.mock_component_user_usages([user_usage_data])
        tasks.UsagePullTask().pull(self.resource)

        component_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
        )
        user_usage = models.ComponentUserUsage.objects.get(
            component_usage=component_usage,
            username="test_user",
        )
        self.assertEqual(component_usage.usage, 100)
        self.assertEqual(component_usage.description, "Test usage")
        self.assertEqual(component_usage.backend_id, usage_data["uuid"])
        self.assertEqual(user_usage.usage, 50)
        self.assertEqual(user_usage.user, offering_user)

    def _usage_payloads(self, usernames, billing_period):
        """One component usage plus one user usage per username."""
        usage = {
            "uuid": uuid.uuid4().hex,
            "type": "cpu_k_hours",
            "usage": 100,
            "description": "Test usage",
            "created": "2024-03-01T00:00:00Z",
            "date": "2024-04-01T00:00:00Z",
            "recurring": False,
            "billing_period": billing_period,
        }
        user_usages = [
            {
                "username": username,
                "usage": 10,
                "component_type": "cpu_k_hours",
                "billing_period": billing_period,
            }
            for username in usernames
        ]
        return usage, user_usages

    def _prepare(self, usernames, billing_periods):
        models.OfferingComponent.objects.get_or_create(
            offering=self.resource.offering,
            type="cpu_k_hours",
            defaults={"name": "CPU Hours"},
        )
        for username in usernames:
            user = core_models.User.objects.filter(username=username).first()
            if user is None:
                user = UserFactory(username=username)
            models.OfferingUser.objects.get_or_create(
                offering=self.resource.offering,
                user=user,
                defaults={"username": username},
            )
        usages, user_usages = [], []
        for billing_period in billing_periods:
            usage, period_user_usages = self._usage_payloads(usernames, billing_period)
            usages.append(usage)
            user_usages.extend(period_user_usages)
        self.mock_component_usages(usages)
        self.mock_component_user_usages(user_usages)

    def _offering_user_selects(self, captured):
        return [
            q
            for q in captured.captured_queries
            if "marketplace_offeringuser" in q["sql"] and q["sql"].startswith("SELECT")
        ]

    def test_offering_users_are_loaded_once_per_pull(self):
        """The offering user lookup used to run once per user usage."""
        self._prepare([f"user_{i}" for i in range(5)], ["2024-03-01"])

        # Only the pull is measured; the fixtures above issue their own
        # queries.
        with django_test.CaptureQueriesContext(connection) as captured:
            tasks.UsagePullTask().pull(self.resource)

        self.assertEqual(
            len(self._offering_user_selects(captured)),
            1,
            "offering users must be loaded once per pull, not once per user usage",
        )

    def test_offering_user_query_count_is_flat_across_usage_volume(self):
        self._prepare([f"user_{i}" for i in range(2)], ["2024-03-01"])
        with django_test.CaptureQueriesContext(connection) as small:
            tasks.UsagePullTask().pull(self.resource)

        self._prepare([f"user_{i}" for i in range(6)], ["2024-03-01", "2024-04-01"])
        with django_test.CaptureQueriesContext(connection) as large:
            tasks.UsagePullTask().pull(self.resource)

        self.assertEqual(
            len(self._offering_user_selects(small)),
            len(self._offering_user_selects(large)),
        )

    def test_duplicate_usernames_resolve_deterministically(self):
        """(offering, username) is not unique - only (offering, user) is."""
        models.OfferingComponent.objects.create(
            offering=self.resource.offering, type="cpu_k_hours", name="CPU Hours"
        )
        first = models.OfferingUser.objects.create(
            offering=self.resource.offering,
            username="shared",
            user=UserFactory(username="a"),
        )
        models.OfferingUser.objects.create(
            offering=self.resource.offering,
            username="shared",
            user=UserFactory(username="b"),
        )
        usage, user_usages = self._usage_payloads(["shared"], "2024-03-01")
        self.mock_component_usages([usage])
        self.mock_component_user_usages(user_usages)

        tasks.UsagePullTask().pull(self.resource)

        user_usage = models.ComponentUserUsage.objects.get(username="shared")
        self.assertEqual(user_usage.user, first)

    def test_invalid_usage_date_is_skipped(self):
        """
        Test that invalid usage dates are skipped.
        """
        models.OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="cpu_k_hours",
            name="CPU Hours",
        )

        usage_data = {
            "uuid": uuid.uuid4().hex,
            "type": "cpu_k_hours",
            "usage": 100,
            "description": "Test usage",
            "created": "2024-03-01T00:00:00Z",
            "date": "2022-01-01T00:00:00Z",
            "recurring": False,
            "billing_period": "2024-03-01",
        }
        self.mock_component_usages([usage_data])
        self.mock_component_user_usages([])  # No user usages

        tasks.UsagePullTask().pull(self.resource)

        self.assertFalse(
            models.ComponentUsage.objects.filter(
                resource=self.resource,
            ).exists()
        )

    def test_multiple_component_types_with_same_username_handled_correctly(self):
        """
        Test that when multiple component types have user usages for the same username,
        each component gets the correct usage value instead of being overwritten.
        """
        cpu_component = models.OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="cpu_k_hours",
            name="CPU k hours",
        )
        gpu_component = models.OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="gpu_hours",
            name="GPU hours",
        )

        cpu_usage_data = {
            "uuid": uuid.uuid4().hex,
            "type": "cpu_k_hours",
            "usage": 11.37,
            "description": "CPU usage",
            "created": "2024-03-01T00:00:00Z",
            "date": "2024-04-01T00:00:00Z",
            "recurring": False,
            "billing_period": "2024-03-01",
        }
        gpu_usage_data = {
            "uuid": uuid.uuid4().hex,
            "type": "gpu_hours",
            "usage": 0.00,
            "description": "GPU usage",
            "created": "2024-03-01T00:00:00Z",
            "date": "2024-04-01T00:00:00Z",
            "recurring": False,
            "billing_period": "2024-03-01",
        }

        self.mock_component_usages([cpu_usage_data, gpu_usage_data])

        # Mock ALL user usages in a single call (optimized N+1 fix)
        self.mock_component_user_usages(
            [
                {
                    "username": "testuserusername",
                    "usage": 11.37,
                    "component_type": "cpu_k_hours",
                    "billing_period": "2024-03-01",
                },
                {
                    "username": "testuserusername",
                    "usage": 0.00,
                    "component_type": "gpu_hours",
                    "billing_period": "2024-03-01",
                },
            ]
        )

        user = UserFactory(username="testuserusername")
        offering_user = models.OfferingUser.objects.create(
            offering=self.resource.offering,
            username="testuserusername",
            user=user,
        )

        tasks.UsagePullTask().pull(self.resource)

        cpu_component_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=cpu_component,
        )
        gpu_component_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
            component=gpu_component,
        )

        self.assertEqual(cpu_component_usage.usage, Decimal("11.37"))
        self.assertEqual(gpu_component_usage.usage, Decimal("0.00"))

        cpu_user_usage = models.ComponentUserUsage.objects.get(
            component_usage=cpu_component_usage,
            username="testuserusername",
        )
        gpu_user_usage = models.ComponentUserUsage.objects.get(
            component_usage=gpu_component_usage,
            username="testuserusername",
        )

        self.assertEqual(
            cpu_user_usage.usage,
            Decimal("11.37"),
            "CPU user usage should be 11.37, not overwritten by GPU usage",
        )
        self.assertEqual(
            gpu_user_usage.usage, Decimal("0.00"), "GPU user usage should be 0.00"
        )

        self.assertEqual(cpu_user_usage.user, offering_user)
        self.assertEqual(gpu_user_usage.user, offering_user)

    def test_user_usages_fetched_in_single_api_call(self):
        """
        Test that user usages are fetched in a single API call (N+1 optimization).

        Previously, the pull() method made N+1 API calls - one per component usage.
        After optimization, it should make only 2 API calls total:
        1. One for component usages
        2. One for ALL user usages (grouped in memory by billing_period + type)
        """
        cpu_component = models.OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="cpu_k_hours",
            name="CPU k hours",
        )
        gpu_component = models.OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="gpu_hours",
            name="GPU hours",
        )
        mem_component = models.OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="mem_gb_hours",
            name="Memory GB hours",
        )

        # Create 3 component usages - previously would trigger 3 user usage API calls
        cpu_usage = {
            "uuid": uuid.uuid4().hex,
            "type": "cpu_k_hours",
            "usage": 100,
            "description": "CPU",
            "created": "2024-03-01T00:00:00Z",
            "date": "2024-04-01T00:00:00Z",
            "recurring": False,
            "billing_period": "2024-03-01",
        }
        gpu_usage = {
            "uuid": uuid.uuid4().hex,
            "type": "gpu_hours",
            "usage": 50,
            "description": "GPU",
            "created": "2024-03-01T00:00:00Z",
            "date": "2024-04-01T00:00:00Z",
            "recurring": False,
            "billing_period": "2024-03-01",
        }
        mem_usage = {
            "uuid": uuid.uuid4().hex,
            "type": "mem_gb_hours",
            "usage": 200,
            "description": "Memory",
            "created": "2024-03-01T00:00:00Z",
            "date": "2024-04-01T00:00:00Z",
            "recurring": False,
            "billing_period": "2024-03-01",
        }

        self.mock_component_usages([cpu_usage, gpu_usage, mem_usage])

        # Mock ALL user usages in a single response - this proves the optimization
        # If N+1 bug still existed, this mock wouldn't be hit (different params)
        all_user_usages = [
            {
                "username": "user1",
                "usage": 30,
                "component_type": "cpu_k_hours",
                "billing_period": "2024-03-01",
            },
            {
                "username": "user2",
                "usage": 70,
                "component_type": "cpu_k_hours",
                "billing_period": "2024-03-01",
            },
            {
                "username": "user1",
                "usage": 50,
                "component_type": "gpu_hours",
                "billing_period": "2024-03-01",
            },
            {
                "username": "user1",
                "usage": 200,
                "component_type": "mem_gb_hours",
                "billing_period": "2024-03-01",
            },
        ]
        self.mock_component_user_usages(all_user_usages)

        user1 = UserFactory(username="user1")
        user2 = UserFactory(username="user2")
        models.OfferingUser.objects.create(
            offering=self.resource.offering, username="user1", user=user1
        )
        models.OfferingUser.objects.create(
            offering=self.resource.offering, username="user2", user=user2
        )

        tasks.UsagePullTask().pull(self.resource)

        # Verify component usages created
        self.assertEqual(
            models.ComponentUsage.objects.filter(resource=self.resource).count(),
            3,
            "Should have 3 component usages",
        )

        # Verify user usages correctly distributed (grouped from single API response)
        cpu_component_usage = models.ComponentUsage.objects.get(
            resource=self.resource, component=cpu_component
        )
        gpu_component_usage = models.ComponentUsage.objects.get(
            resource=self.resource, component=gpu_component
        )
        mem_component_usage = models.ComponentUsage.objects.get(
            resource=self.resource, component=mem_component
        )

        # CPU should have 2 user usages (user1, user2)
        cpu_user_usages = models.ComponentUserUsage.objects.filter(
            component_usage=cpu_component_usage
        )
        self.assertEqual(cpu_user_usages.count(), 2)

        # GPU should have 1 user usage (user1)
        gpu_user_usages = models.ComponentUserUsage.objects.filter(
            component_usage=gpu_component_usage
        )
        self.assertEqual(gpu_user_usages.count(), 1)

        # Memory should have 1 user usage (user1)
        mem_user_usages = models.ComponentUserUsage.objects.filter(
            component_usage=mem_component_usage
        )
        self.assertEqual(mem_user_usages.count(), 1)

    def test_missing_plan_period_is_created_during_sync(self):
        """
        Test that missing ResourcePlanPeriod is automatically created during usage sync.
        """
        models.OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="cpu_k_hours",
            name="CPU Hours",
        )

        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.assertIsNotNone(self.resource.plan, "Resource is missing a plan")
        local_plan = self.resource.plan

        usage_data = {
            "uuid": uuid.uuid4().hex,
            "type": "cpu_k_hours",
            "usage": 100,
            "description": "Test usage",
            "created": "2024-03-01T00:00:00Z",
            "date": "2024-04-01T00:00:00Z",
            "recurring": False,
            "billing_period": "2024-03-01",
        }

        self.mock_component_usages([usage_data])
        self.mock_component_user_usages([])

        tasks.UsagePullTask().pull(self.resource)

        plan_period = models.ResourcePlanPeriod.objects.get(resource=self.resource)

        self.assertEqual(plan_period.plan, local_plan)
        self.assertEqual(plan_period.start, self.resource.created)
        self.assertIsNone(plan_period.end)

        component_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
        )
        self.assertEqual(component_usage.plan_period, plan_period)
        self.assertEqual(component_usage.usage, 100)

    def test_plan_period_not_created_for_resource_without_plan(self):
        """
        Test that plan period is not created for resources without a plan.
        """
        models.OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="cpu_k_hours",
            name="CPU Hours",
        )

        self.resource.plan = None
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.assertFalse(
            models.ResourcePlanPeriod.objects.filter(resource=self.resource).exists(),
            "Resource should not have any plan periods",
        )

        usage_data = {
            "uuid": uuid.uuid4().hex,
            "type": "cpu_k_hours",
            "usage": 100,
            "description": "Test usage",
            "created": "2024-03-01T00:00:00Z",
            "date": "2024-04-01T00:00:00Z",
            "recurring": False,
            "billing_period": "2024-03-01",
        }

        self.mock_component_usages([usage_data])
        self.mock_component_user_usages([])

        tasks.UsagePullTask().pull(self.resource)

        self.assertFalse(
            models.ResourcePlanPeriod.objects.filter(resource=self.resource).exists()
        )

        component_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
        )
        self.assertIsNone(component_usage.plan_period)
        self.assertEqual(component_usage.usage, 100)

    def test_existing_plan_period_is_reused(self):
        """
        Test that existing plan period is reused instead of creating a new one.
        """
        models.OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="cpu_k_hours",
            name="CPU Hours",
        )

        self.resource.state = ResourceStates.OK
        self.resource.save()

        # Plan period is created by signal handler (create_resource_plan_period_when_resource_is_created)
        existing_plan_period = models.ResourcePlanPeriod.objects.get(
            resource=self.resource
        )
        self.assertEqual(
            models.ResourcePlanPeriod.objects.filter(resource=self.resource).count(),
            1,
            "Resource should have exactly one plan period",
        )

        usage_data = {
            "uuid": uuid.uuid4().hex,
            "type": "cpu_k_hours",
            "usage": 100,
            "description": "Test usage",
            "created": "2024-03-01T00:00:00Z",
            "date": "2024-04-01T00:00:00Z",
            "recurring": False,
            "billing_period": "2024-03-01",
        }

        self.mock_component_usages([usage_data])
        self.mock_component_user_usages([])

        tasks.UsagePullTask().pull(self.resource)

        self.assertEqual(
            models.ResourcePlanPeriod.objects.filter(resource=self.resource).count(),
            1,
            f"Expected 1 plan period, got {models.ResourcePlanPeriod.objects.filter(resource=self.resource).count()}",
        )

        component_usage = models.ComponentUsage.objects.get(
            resource=self.resource,
        )
        self.assertEqual(component_usage.plan_period, existing_plan_period)
        self.assertEqual(component_usage.usage, 100)


class RobotAccountStatesTest(testcases.TestCase):
    """Test that the monkey-patched RobotAccountStates works with both enum versions.

    The waldur_api_client can have two different enum implementations:
    - IntEnum version: VALUE_1=1, VALUE_2=2, VALUE_3=3, etc.
    - StrEnum version: REQUESTED="Requested", CREATING="Creating", OK="OK", etc.

    The monkey-patch should handle both versions and convert between string/int representations.

    Fixes PUHURI-PORTALS-DC4: ValueError: 3 is not a valid RobotAccountStates
    """

    def setUp(self):
        # The patch is installed lazily on first robot-account pull (so importing
        # tasks.py does not load the SDK enum at process startup). These tests
        # exercise the patched enum in isolation, so apply it explicitly here.
        from waldur_mastermind.marketplace_remote.tasks import (
            _patch_robot_account_states,
        )

        _patch_robot_account_states()

    def test_robot_account_states_enum_handles_string_display_values(self):
        """Test that display string values like 'OK' work."""
        from waldur_api_client.models.robot_account_states import RobotAccountStates

        # These display strings should work regardless of enum version
        state = RobotAccountStates("OK")
        self.assertIsInstance(state, RobotAccountStates)

        state = RobotAccountStates("Creating")
        self.assertIsInstance(state, RobotAccountStates)

        state = RobotAccountStates("Requested")
        self.assertIsInstance(state, RobotAccountStates)

        state = RobotAccountStates("Requested deletion")
        self.assertIsInstance(state, RobotAccountStates)

        state = RobotAccountStates("Deleted")
        self.assertIsInstance(state, RobotAccountStates)

        state = RobotAccountStates("Error")
        self.assertIsInstance(state, RobotAccountStates)

    def test_robot_account_states_enum_handles_integer_values(self):
        """Test that integer values like 3 work."""
        from waldur_api_client.models.robot_account_states import RobotAccountStates

        # Integer values should work regardless of enum version
        for i in range(1, 7):
            state = RobotAccountStates(i)
            self.assertIsInstance(state, RobotAccountStates)

    def test_robot_account_states_enum_handles_numeric_string_values(self):
        """Test that numeric string values like '3' work.

        Fixes PUHURI-PORTALS-DC4: ValueError: 3 is not a valid RobotAccountStates
        """
        from waldur_api_client.models.robot_account_states import RobotAccountStates

        # Numeric string values should work regardless of enum version
        for i in range(1, 7):
            state = RobotAccountStates(str(i))
            self.assertIsInstance(state, RobotAccountStates)

    def test_robot_account_states_enum_handles_invalid_string_values(self):
        """Test that invalid string values raise ValueError."""
        from waldur_api_client.models.robot_account_states import RobotAccountStates

        with self.assertRaises(ValueError):
            RobotAccountStates("InvalidState")
