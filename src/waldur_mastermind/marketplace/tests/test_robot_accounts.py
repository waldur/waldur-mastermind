from unittest import mock

from constance.test.unittest import override_config
from ddt import data, ddt
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.models import get_ssh_key_fingerprints
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import RobotAccountStates
from waldur_mastermind.marketplace.tasks import (
    reconcile_robot_account_access,
    remove_users_from_robot_accounts_on_permission_loss,
)
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class RobotAccountTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_RESOURCE_ROBOT_ACCOUNT)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT)
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_RESOURCE_ROBOT_ACCOUNT)

        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.CREATE_RESOURCE_ROBOT_ACCOUNT
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.DELETE_RESOURCE_ROBOT_ACCOUNT
        )

    @data("staff", "service_manager", "service_owner")
    def test_authorized_user_can_create_robot_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.RobotAccountFactory.get_list_url()
        resource_url = factories.ResourceFactory.get_url(self.fixture.resource)
        response = self.client.post(url, {"resource": resource_url, "type": "cicd"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_create_robot_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.RobotAccountFactory.get_list_url()
        resource_url = factories.ResourceFactory.get_url(self.fixture.resource)
        response = self.client.post(url, {"resource": resource_url, "type": "cicd"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        "staff",
        "service_manager",
        "service_owner",
        "customer_support",
        "admin",
        "manager",
    )
    def test_authorized_user_can_get_robot_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.RobotAccountFactory(resource=self.fixture.resource)
        url = factories.RobotAccountFactory.get_url(account)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data("staff", "service_manager", "service_owner")
    def test_authorized_user_can_update_robot_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.RobotAccountFactory(resource=self.fixture.resource)
        url = factories.RobotAccountFactory.get_url(account)

        response = self.client.patch(url, {"username": "foo"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        account.refresh_from_db()
        self.assertEqual(account.username, "foo")

    @data("admin", "manager")
    def test_unauthorized_user_can_not_update_robot_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.RobotAccountFactory(resource=self.fixture.resource)
        url = factories.RobotAccountFactory.get_url(account)

        response = self.client.patch(url, {"username": "foo"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    def test_robot_account_response_contains_key_fingerprints(self):
        self.client.force_authenticate(self.fixture.service_owner)
        ssh_keys = [
            "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDRmKSYeNxfyNGIoYqQCXUjLlMFJSCX/Jx+k0ODlg0xpMMlBEEK test"
        ]
        account = factories.RobotAccountFactory(
            resource=self.fixture.resource, keys=ssh_keys
        )
        url = factories.RobotAccountFactory.get_url(account)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(1, len(response.data["keys"]))
        fingerprint_md5, fingerprint_sha256, fingerprint_sha512 = (
            get_ssh_key_fingerprints(ssh_keys[0])
        )

        self.assertEqual(fingerprint_md5, response.data["fingerprints"][0]["md5"])
        self.assertEqual(fingerprint_sha256, response.data["fingerprints"][0]["sha256"])
        self.assertEqual(fingerprint_sha512, response.data["fingerprints"][0]["sha512"])

    def test_robot_account_state_returns_string_not_integer(self):
        """Test that the state field returns string values like 'OK' not integers"""
        self.client.force_authenticate(self.fixture.service_owner)

        # Test each state to ensure it returns the correct string value
        test_cases = [
            (RobotAccountStates.REQUESTED, "Requested", "type1"),
            (RobotAccountStates.CREATING, "Creating", "type2"),
            (RobotAccountStates.OK, "OK", "type3"),
            (RobotAccountStates.REQUESTED_DELETION, "Requested deletion", "type4"),
            (RobotAccountStates.DELETED, "Deleted", "type5"),
            (RobotAccountStates.ERROR, "Error", "type6"),
        ]

        for state_value, expected_string, robot_type in test_cases:
            account = factories.RobotAccountFactory(
                resource=self.fixture.resource, state=state_value, type=robot_type
            )
            url = factories.RobotAccountFactory.get_url(account)
            response = self.client.get(url)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("state", response.data)
            self.assertEqual(
                response.data["state"],
                expected_string,
                f"Expected state to be '{expected_string}' but got '{response.data['state']}' for state value {state_value}",
            )
            # Ensure it's a string, not an integer
            self.assertIsInstance(response.data["state"], str)
            self.assertNotIsInstance(response.data["state"], int)

    def test_robot_account_list_state_returns_string(self):
        """Test that the state field returns string values in list view"""
        self.client.force_authenticate(self.fixture.service_owner)

        # Create accounts with different states and types to avoid unique constraint
        factories.RobotAccountFactory(
            resource=self.fixture.resource, state=RobotAccountStates.OK, type="list1"
        )
        factories.RobotAccountFactory(
            resource=self.fixture.resource, state=RobotAccountStates.ERROR, type="list2"
        )

        url = factories.RobotAccountFactory.get_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 2)

        # Check that all states are strings
        for account_data in response.data:
            if "state" in account_data:  # Some list views might not include all fields
                self.assertIsInstance(
                    account_data["state"],
                    str,
                    f"State should be a string, got {type(account_data['state'])}",
                )
                self.assertIn(
                    account_data["state"],
                    [
                        "Requested",
                        "Creating",
                        "OK",
                        "Requested deletion",
                        "Deleted",
                        "Error",
                    ],
                    f"Unexpected state value: {account_data['state']}",
                )

    def test_create_robot_account_with_invalid_user_shows_user_name_in_error(self):
        """Test that creating a robot account with a user not in the resource's project/organization shows the user's name in the error"""
        self.client.force_authenticate(self.fixture.service_owner)

        invalid_user = structure_factories.UserFactory(
            first_name="John", last_name="Snow", email="john.snow@example.com"
        )

        url = factories.RobotAccountFactory.get_list_url()
        resource_url = factories.ResourceFactory.get_url(self.fixture.resource)
        user_url = structure_factories.UserFactory.get_url(invalid_user)

        response = self.client.post(
            url,
            {
                "resource": resource_url,
                "type": "cicd",
                "users": [user_url],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        error_message = str(response.data)
        self.assertIn(
            "Users John Snow should belong to the same project or organization as the resource.",
            error_message,
        )

    def test_update_robot_account_with_invalid_user_shows_user_name_in_error(self):
        """Test that updating a robot account with a user not in the resource's project/organization shows the user's name in the error"""
        self.client.force_authenticate(self.fixture.service_owner)

        account = factories.RobotAccountFactory(resource=self.fixture.resource)
        url = factories.RobotAccountFactory.get_url(account)

        invalid_user = structure_factories.UserFactory(
            first_name="Jane", last_name="Smith", email="jane.smith@example.com"
        )

        user_url = structure_factories.UserFactory.get_url(invalid_user)

        response = self.client.patch(
            url,
            {
                "users": [user_url],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        error_message = str(response.data)
        self.assertIn(
            "Users Jane Smith should belong to the same project or organization as the resource.",
            error_message,
        )

    def test_update_robot_account_with_invalid_responsible_user_shows_user_name_in_error(
        self,
    ):
        """Test that updating a robot account with a responsible_user not in the resource's project/organization shows the user's name in the error"""
        self.client.force_authenticate(self.fixture.service_owner)

        account = factories.RobotAccountFactory(resource=self.fixture.resource)
        url = factories.RobotAccountFactory.get_url(account)

        invalid_user = structure_factories.UserFactory(
            first_name="John", last_name="Snow", email="john.snow@example.com"
        )

        user_url = structure_factories.UserFactory.get_url(invalid_user)

        response = self.client.patch(
            url,
            {
                "responsible_user": user_url,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        error_message = str(response.data)
        self.assertIn(
            "The responsible user John Snow should belong to the same project or organization as the resource.",
            error_message,
        )


class RobotAccountStateTransitionTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT
        )
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.resource = self.fixture.resource
        self.resource.save()
        self.offering.save()
        self.robot_account = factories.RobotAccountFactory(resource=self.resource)

    def get_action_url(self, account, action):
        # Construct the action URL
        base_url = factories.RobotAccountFactory.get_url(account)
        return f"{base_url}{action}/"

    def verify_state_transition(
        self,
        initial_state,
        action,
        expected_state,
        data=None,
        expected_status=status.HTTP_200_OK,
    ):
        """Helper method to verify state transitions and logging"""
        self.robot_account.state = initial_state
        self.robot_account.save()

        self.client.force_login(self.fixture.offering_owner)
        url = self.get_action_url(self.robot_account, action)

        with mock.patch("waldur_core.logging.event_logger.emit") as logger_mock:
            response = self.client.post(url, data=data, format="json" if data else None)
            self.assertEqual(
                response.status_code,
                expected_status,
                f"Request failed with response: {response.data}, expected status: {expected_status}, got: {response.status_code}",
            )

            if expected_status == status.HTTP_200_OK:
                self.robot_account.refresh_from_db()
                # Check that the state is set correctly
                self.assertEqual(self.robot_account.state, expected_state)

                # Verify logging
                state_change_calls = [
                    call
                    for call in logger_mock.call_args_list
                    if call[1]["event_type"] == "resource_robot_account_updated"
                ]
                # Check that the state change log call is made
                self.assertEqual(len(state_change_calls), 1)

                from_state = RobotAccountStates.CHOICES[initial_state - 1][1]
                to_state = RobotAccountStates.CHOICES[expected_state - 1][1]
                expected_message = f"Robot account waldur has been updated. Robot account 'waldur' state changed from '{from_state}' to '{to_state}'."
                self.assertEqual(state_change_calls[0][0][0], expected_message)

    def test_requested_to_creating_transition(self):
        """Test transition from REQUESTED to CREATING"""
        self.verify_state_transition(
            initial_state=RobotAccountStates.REQUESTED,
            action="set_state_creating",
            expected_state=RobotAccountStates.CREATING,
        )

    def test_invalid_transition_fails(self):
        """
        Test that we can't go from REQUESTED directly to OK
        """
        self.client.force_login(self.fixture.offering_owner)
        url = self.get_action_url(self.robot_account, "set_state_ok")
        response = self.client.post(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_409_CONFLICT,
            f"Expected status code 409, but got {response.status_code}",
        )

    def test_ok_to_requested_deletion_transition(self):
        """Test transition from OK to REQUESTED_DELETION"""
        self.verify_state_transition(
            initial_state=RobotAccountStates.OK,
            action="set_state_request_deletion",
            expected_state=RobotAccountStates.REQUESTED_DELETION,
        )

    def test_requested_deletion_to_deleted_transition(self):
        """Test transition from REQUESTED_DELETION to DELETED"""
        self.verify_state_transition(
            initial_state=RobotAccountStates.REQUESTED_DELETION,
            action="set_state_deleted",
            expected_state=RobotAccountStates.DELETED,
        )

    def test_transition_to_error_state(self):
        """Test transition to ERROR state with error message"""
        self.verify_state_transition(
            initial_state=RobotAccountStates.OK,
            action="set_state_erred",
            expected_state=RobotAccountStates.ERROR,
            data={"error_message": "Something went wrong"},
        )

    def test_invalid_transitions(self):
        """Test various invalid state transitions"""
        invalid_transitions = [
            (RobotAccountStates.REQUESTED, "set_state_deleted"),
            (RobotAccountStates.CREATING, "set_state_request_deletion"),
            (RobotAccountStates.DELETED, "set_state_ok"),
        ]

        for initial_state, action in invalid_transitions:
            self.verify_state_transition(
                initial_state=initial_state,
                action=action,
                expected_state=None,  # No expected state for failed transitions
                expected_status=status.HTTP_409_CONFLICT,
            )

    def test_error_message_persistence(self):
        """Test that error message is correctly stored"""
        error_message = "Test error message"
        self.verify_state_transition(
            initial_state=RobotAccountStates.OK,
            action="set_state_erred",
            expected_state=RobotAccountStates.ERROR,
            data={"error_message": error_message},
        )
        self.robot_account.refresh_from_db()
        self.assertEqual(self.robot_account.error_message, error_message)

    def test_error_state_without_message(self):
        """Test transition to error state without providing message"""
        self.verify_state_transition(
            initial_state=RobotAccountStates.OK,
            action="set_state_erred",
            expected_state=RobotAccountStates.ERROR,
            data={},
        )
        self.robot_account.refresh_from_db()
        # Check that the error message is empty
        self.assertEqual(self.robot_account.error_message, "")

    def test_robot_account_state_filtering(self):
        """Test endpoint filtering robot accounts by state"""
        # Create accounts in different states
        ok_account = factories.RobotAccountFactory(
            resource=self.resource, state=RobotAccountStates.OK, type="test1"
        )
        creating_account = factories.RobotAccountFactory(
            resource=self.resource,
            state=RobotAccountStates.CREATING,
            type="test2",
        )

        url = factories.RobotAccountFactory.get_list_url()
        self.client.force_login(self.fixture.staff)

        # Test filtering for OK state
        response = self.client.get(url, {"state": RobotAccountStates.OK})
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Expected status code 200, but got {response.status_code}",
        )
        self.assertEqual(
            len(response.data),
            1,
            f"Expected 1 robot account, but got {len(response.data)}",
        )
        self.assertEqual(
            response.data[0]["uuid"],
            ok_account.uuid.hex,
            f"Expected {ok_account.uuid.hex}, but got {response.data[0]['uuid']}",
        )

        # Test filtering for CREATING state
        response = self.client.get(url, {"state": RobotAccountStates.CREATING})
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Expected status code 200, but got {response.status_code}",
        )
        self.assertEqual(
            len(response.data),
            1,
            f"Expected 1 robot account, but got {len(response.data)}",
        )
        self.assertEqual(
            response.data[0]["uuid"],
            creating_account.uuid.hex,
            f"Expected {creating_account.uuid.hex}, but got {response.data[0]['uuid']}",
        )


class RobotAccountAccessTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.robot_account = factories.RobotAccountFactory(
            resource=self.fixture.resource
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT
        )

    def test_unauthorized_user_list_access(self):
        """Test that unauthorized user sees empty list"""
        self.client.force_login(self.fixture.user)
        list_url = factories.RobotAccountFactory.get_list_url()
        response = self.client.get(list_url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Expected status code 200, but got {response.status_code}",
        )
        self.assertEqual(
            len(response.data),
            0,
            f"Expected 0 robot accounts, but got {len(response.data)}",
        )

    def test_unauthorized_user_detail_access(self):
        """Test that unauthorized user can't see specific robot account"""
        self.client.force_login(self.fixture.user)
        detail_url = factories.RobotAccountFactory.get_url(self.robot_account)
        response = self.client.get(detail_url)
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            f"Expected status code 404, but got {response.status_code}",
        )

    def test_unauthorized_user_state_transition(self):
        """Test that unauthorized user gets 404 for state transitions"""
        self.client.force_login(self.fixture.user)
        url = self.get_action_url(self.robot_account, "set_state_ok")
        response = self.client.post(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            f"Expected status code 404, but got {response.status_code}",
        )

    def test_read_only_user_state_transition(self):
        """Test that user without update permission can't perform state transitions"""
        self.client.force_login(self.fixture.manager)
        url = self.get_action_url(self.robot_account, "set_state_ok")
        response = self.client.post(url)
        # A project manager should not have update permission
        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
            f"Expected status code 403, but got {response.status_code}",
        )

    def test_service_owner_state_transition(self):
        """Test that service owner can perform state transitions"""
        self.client.force_login(self.fixture.service_owner)
        self.robot_account.state = RobotAccountStates.CREATING
        self.robot_account.save()

        url = self.get_action_url(self.robot_account, "set_state_ok")
        response = self.client.post(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Expected status code 200, but got {response.status_code}",
        )

    def get_action_url(self, account, action):
        base_url = factories.RobotAccountFactory.get_url(account)
        return f"{base_url}{action}/"


class RobotAccountRoleRevocationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.robot_account = factories.RobotAccountFactory(
            resource=self.fixture.resource
        )
        # Add a user to the robot account
        self.test_user = self.fixture.user
        self.robot_account.users.add(self.test_user)
        self.robot_account.responsible_user = self.test_user
        self.robot_account.save()

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.remove_users_from_robot_accounts_on_permission_loss.delay"
    )
    def test_role_revoked_signal_triggers_task(self, mock_task):
        """Test that revoking a user role triggers the robot account cleanup task"""

        # Create a user role for the test user in the project
        user_role = UserRole.objects.create(
            user=self.test_user,
            role=ProjectRole.ADMIN,
            scope=self.fixture.project,
            is_active=True,
        )

        # Revoke the role - this should trigger the signal
        user_role.revoke()

        # Verify that the task was scheduled
        mock_task.assert_called_once_with(user_role.id)

    def test_remove_users_from_robot_accounts_task_removes_user_access(self):
        """Test that the task correctly removes users from robot accounts when they lose project access"""

        # Create and immediately revoke a user role
        user_role = UserRole.objects.create(
            user=self.test_user,
            role=ProjectRole.ADMIN,
            scope=self.fixture.project,
            is_active=False,  # Already revoked
        )

        # Verify initial state
        self.assertTrue(self.robot_account.users.filter(id=self.test_user.id).exists())
        self.assertEqual(self.robot_account.responsible_user, self.test_user)

        # Run the task
        remove_users_from_robot_accounts_on_permission_loss(user_role.id)

        # Refresh the robot account
        self.robot_account.refresh_from_db()

        # Verify user was removed from robot account
        self.assertFalse(self.robot_account.users.filter(id=self.test_user.id).exists())
        self.assertIsNone(self.robot_account.responsible_user)

    @mock.patch("waldur_core.logging.event_logger.emit")
    def test_remove_users_task_logs_removal_events(self, mock_event_logger):
        """Test that the task logs appropriate events when removing users from robot accounts"""

        # Create and immediately revoke a user role
        user_role = UserRole.objects.create(
            user=self.test_user,
            role=ProjectRole.ADMIN,
            scope=self.fixture.project,
            is_active=False,  # Already revoked
        )

        # Run the task
        remove_users_from_robot_accounts_on_permission_loss(user_role.id)

        # Verify that event logging was called (expect at least 2 calls for our specific removals)
        self.assertGreaterEqual(mock_event_logger.call_count, 2)

        # Find our specific calls in the list
        user_removal_calls = [
            call
            for call in mock_event_logger.call_args_list
            if "has been removed from robot account" in call[0][0]
            and "Responsible user" not in call[0][0]
        ]
        responsible_user_calls = [
            call
            for call in mock_event_logger.call_args_list
            if "Responsible user" in call[0][0]
            and "has been removed from robot account" in call[0][0]
        ]

        # Check user removal call
        self.assertEqual(len(user_removal_calls), 1)
        user_call = user_removal_calls[0]
        self.assertIn("has been removed from robot account", user_call[0][0])
        self.assertEqual(
            user_call[1]["event_type"], EventType.RESOURCE_ROBOT_ACCOUNT_UPDATED
        )
        self.assertEqual(
            user_call[1]["event_context"]["reason"], "project_access_revoked"
        )
        self.assertEqual(user_call[1]["event_context"]["user"], self.test_user)
        self.assertEqual(
            user_call[1]["event_context"]["robot_account"], self.robot_account
        )

        # Check responsible user removal call
        self.assertEqual(len(responsible_user_calls), 1)
        responsible_call = responsible_user_calls[0]
        self.assertIn("Responsible user", responsible_call[0][0])
        self.assertIn("has been removed from robot account", responsible_call[0][0])
        self.assertEqual(
            responsible_call[1]["event_type"], EventType.RESOURCE_ROBOT_ACCOUNT_UPDATED
        )
        self.assertEqual(
            responsible_call[1]["event_context"]["reason"], "project_access_revoked"
        )
        self.assertEqual(
            responsible_call[1]["event_context"]["action"], "responsible_user_cleared"
        )

    def test_remove_users_task_preserves_access_if_user_has_other_roles(self):
        """Test that the task doesn't remove users who still have active project roles"""

        # Create two roles for the user
        role1 = UserRole.objects.create(
            user=self.test_user,
            role=ProjectRole.ADMIN,
            scope=self.fixture.project,
            is_active=False,  # This one is revoked
        )

        UserRole.objects.create(
            user=self.test_user,
            role=ProjectRole.MANAGER,
            scope=self.fixture.project,
            is_active=True,  # This one is still active
        )

        # Run the task for the revoked role
        remove_users_from_robot_accounts_on_permission_loss(role1.id)

        # Refresh the robot account
        self.robot_account.refresh_from_db()

        # Verify user still has access since they have another active role
        self.assertTrue(self.robot_account.users.filter(id=self.test_user.id).exists())
        self.assertEqual(self.robot_account.responsible_user, self.test_user)

    def test_remove_users_task_handles_non_project_scope(self):
        """Test that the task ignores role revocations that are not project-scoped"""

        # Create a customer role (non-project scope)
        user_role = UserRole.objects.create(
            user=self.test_user,
            role=CustomerRole.OWNER,
            scope=self.fixture.customer,
            is_active=False,
        )

        # Run the task
        remove_users_from_robot_accounts_on_permission_loss(user_role.id)

        # Refresh the robot account
        self.robot_account.refresh_from_db()

        # Verify user access is preserved (task should ignore non-project scopes)
        self.assertTrue(self.robot_account.users.filter(id=self.test_user.id).exists())
        self.assertEqual(self.robot_account.responsible_user, self.test_user)

    def test_remove_users_task_handles_nonexistent_role(self):
        """Test that the task handles gracefully when UserRole doesn't exist"""

        # Use a non-existent role ID
        nonexistent_id = 99999

        # This should not raise an exception
        remove_users_from_robot_accounts_on_permission_loss(nonexistent_id)

        # Refresh the robot account
        self.robot_account.refresh_from_db()

        # Verify user access is preserved
        self.assertTrue(self.robot_account.users.filter(id=self.test_user.id).exists())
        self.assertEqual(self.robot_account.responsible_user, self.test_user)

    def test_reconcile_robot_account_access_task(self):
        """Test the periodic reconciliation task"""

        # Create another user and robot account for more comprehensive testing
        another_user = structure_factories.UserFactory()
        # Create another resource to avoid unique constraint violation
        another_resource = factories.ResourceFactory(project=self.fixture.project)
        another_robot_account = factories.RobotAccountFactory(
            resource=another_resource,
            type="test",  # Different type to avoid conflict
        )
        another_robot_account.users.add(another_user)
        another_robot_account.responsible_user = another_user
        another_robot_account.save()

        # Create roles for both users
        UserRole.objects.create(
            user=self.test_user,
            role=ProjectRole.ADMIN,
            scope=self.fixture.project,
            is_active=True,  # This user should keep access
        )

        # Don't create a role for another_user - they should lose access

        # Verify initial state
        self.assertTrue(self.robot_account.users.filter(id=self.test_user.id).exists())
        self.assertEqual(self.robot_account.responsible_user, self.test_user)
        self.assertTrue(another_robot_account.users.filter(id=another_user.id).exists())
        self.assertEqual(another_robot_account.responsible_user, another_user)

        # Run the reconciliation task
        result = reconcile_robot_account_access()

        # Refresh from database
        self.robot_account.refresh_from_db()
        another_robot_account.refresh_from_db()

        # Verify results
        self.assertTrue(
            self.robot_account.users.filter(id=self.test_user.id).exists()
        )  # Should keep access
        self.assertEqual(
            self.robot_account.responsible_user, self.test_user
        )  # Should keep responsible role

        self.assertFalse(
            another_robot_account.users.filter(id=another_user.id).exists()
        )  # Should lose access
        self.assertIsNone(
            another_robot_account.responsible_user
        )  # Should lose responsible role

        # Check task results
        self.assertEqual(result["accounts_processed"], 2)
        self.assertEqual(result["users_removed"], 1)

    @mock.patch("waldur_core.logging.event_logger.emit")
    def test_reconcile_task_logs_removal_events(self, mock_event_logger):
        """Test that the reconciliation task logs appropriate events when removing users"""

        # User has no active roles, should be removed during reconciliation

        # Run the reconciliation task
        reconcile_robot_account_access()

        # Should have logged events for user removal and responsible user removal
        removal_events = [
            call
            for call in mock_event_logger.call_args_list
            if "during access reconciliation" in call[0][0]
        ]

        self.assertGreaterEqual(len(removal_events), 1)  # At least one removal event

        # Check that reconciliation reason is properly set
        for event in removal_events:
            self.assertEqual(
                event[1]["event_type"], EventType.RESOURCE_ROBOT_ACCOUNT_UPDATED
            )
            self.assertEqual(
                event[1]["event_context"]["reason"], "access_reconciliation"
            )


def _grant_offering_consent(user, offering, *, revoked=False):
    return models.UserOfferingConsent.objects.create(
        user=user,
        offering=offering,
        version="1.0",
        revocation_date=timezone.now() if revoked else None,
    )


@override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class RobotAccountConsentTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_RESOURCE_ROBOT_ACCOUNT)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT)
        self.offering = self.fixture.offering
        self.resource = self.fixture.resource
        self.member = self.fixture.admin
        self.member_url = structure_factories.UserFactory.get_url(self.member)
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Test ToS",
            version="1.0",
            is_active=True,
        )
        self.list_url = factories.RobotAccountFactory.get_list_url()
        self.resource_url = factories.ResourceFactory.get_url(self.resource)

    def _account_with_member(self):
        account = factories.RobotAccountFactory(resource=self.resource)
        account.users.add(self.member)
        account.responsible_user = self.member
        account.save(update_fields=["responsible_user"])
        structure_factories.SshPublicKeyFactory(user=self.member)
        return account

    def test_provider_cannot_attach_user_without_consent(self):
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.post(
            self.list_url,
            {
                "resource": self.resource_url,
                "type": "cicd",
                "users": [self.member_url],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Terms of Service", str(response.data))

    def test_provider_cannot_set_responsible_user_without_consent(self):
        self.client.force_authenticate(self.fixture.service_owner)
        account = factories.RobotAccountFactory(resource=self.resource)
        response = self.client.patch(
            factories.RobotAccountFactory.get_url(account),
            {"responsible_user": self.member_url},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("responsible user", str(response.data).lower())

    def test_provider_can_attach_user_with_active_consent(self):
        _grant_offering_consent(self.member, self.offering)
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.post(
            self.list_url,
            {
                "resource": self.resource_url,
                "type": "cicd",
                "users": [self.member_url],
                "responsible_user": self.member_url,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_revoked_consent_is_not_enough_to_attach_user(self):
        _grant_offering_consent(self.member, self.offering, revoked=True)
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.post(
            self.list_url,
            {
                "resource": self.resource_url,
                "type": "cicd",
                "users": [self.member_url],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_cannot_attach_user_without_consent(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.list_url,
            {
                "resource": self.resource_url,
                "type": "cicd",
                "users": [self.member_url],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_provider_detail_hides_user_without_consent(self):
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(factories.RobotAccountFactory.get_url(account))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["users"], [])
        self.assertIsNone(response.data["responsible_user"])
        self.assertEqual(response.data["user_keys"], [])

    def test_provider_detail_includes_user_with_consent(self):
        _grant_offering_consent(self.member, self.offering)
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(factories.RobotAccountFactory.get_url(account))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [self.member.uuid.hex],
            [user["uuid"] for user in response.data["users"]],
        )
        self.assertEqual(
            self.member.uuid.hex, response.data["responsible_user"]["uuid"]
        )
        self.assertEqual(1, len(response.data["user_keys"]))

    def test_consumer_detail_still_shows_user_without_consent(self):
        account = self._account_with_member()
        self.client.force_authenticate(self.member)
        response = self.client.get(factories.RobotAccountFactory.get_url(account))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [self.member.uuid.hex],
            [user["uuid"] for user in response.data["users"]],
        )
        self.assertEqual(
            self.member.uuid.hex, response.data["responsible_user"]["uuid"]
        )
        self.assertEqual(1, len(response.data["user_keys"]))

    def test_staff_detail_shows_user_without_consent(self):
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.RobotAccountFactory.get_url(account))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(1, len(response.data["users"]))

    def test_provider_detail_hides_user_with_revoked_consent(self):
        _grant_offering_consent(self.member, self.offering, revoked=True)
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(factories.RobotAccountFactory.get_url(account))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["users"], [])
        self.assertIsNone(response.data["responsible_user"])
        self.assertEqual(response.data["user_keys"], [])

    def test_provider_detail_keeps_only_consenting_users_and_their_keys(self):
        consented = self.fixture.manager
        _grant_offering_consent(consented, self.offering)
        account = factories.RobotAccountFactory(resource=self.resource)
        account.users.add(self.member, consented)
        account.responsible_user = self.member
        account.save(update_fields=["responsible_user"])
        structure_factories.SshPublicKeyFactory(user=self.member)
        structure_factories.SshPublicKeyFactory(user=consented)
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(factories.RobotAccountFactory.get_url(account))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [consented.uuid.hex],
            [user["uuid"] for user in response.data["users"]],
        )
        self.assertIsNone(response.data["responsible_user"])
        self.assertEqual(
            [consented.uuid.hex],
            [key["user_uuid"] for key in response.data["user_keys"]],
        )

    def test_provider_list_hides_user_without_consent(self):
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(self.list_url)
        row = self._row(response, account)
        self.assertEqual(row["users"], [])
        self.assertIsNone(row["responsible_user"])

    def test_username_only_update_keeps_user_without_consent(self):
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.patch(
            factories.RobotAccountFactory.get_url(account),
            {"username": "builder"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        account.refresh_from_db()
        self.assertEqual(account.username, "builder")
        self.assertCountEqual([self.member], list(account.users.all()))
        self.assertEqual(account.responsible_user, self.member)

    def test_provider_save_of_redacted_users_keeps_hidden_user(self):
        consented = self.fixture.manager
        _grant_offering_consent(consented, self.offering)
        account = factories.RobotAccountFactory(resource=self.resource)
        account.users.add(self.member, consented)
        account.responsible_user = self.member
        account.save(update_fields=["responsible_user"])
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.patch(
            factories.RobotAccountFactory.get_url(account),
            {
                "users": [structure_factories.UserFactory.get_url(consented)],
                "responsible_user": "",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        account.refresh_from_db()
        self.assertCountEqual([self.member, consented], list(account.users.all()))
        self.assertEqual(account.responsible_user, self.member)

    def test_provider_can_detach_consenting_user(self):
        _grant_offering_consent(self.member, self.offering)
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.patch(
            factories.RobotAccountFactory.get_url(account),
            {"users": [], "responsible_user": None},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        account.refresh_from_db()
        self.assertFalse(account.users.exists())
        self.assertIsNone(account.responsible_user)

    def test_provider_update_response_hides_user_without_consent(self):
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.patch(
            factories.RobotAccountFactory.get_url(account),
            {"username": "builder"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["users"], [])
        self.assertIsNone(response.data["responsible_user"])
        self.assertNotIn(self.member.uuid.hex, str(response.data))

    def test_provider_update_response_keeps_user_with_consent(self):
        _grant_offering_consent(self.member, self.offering)
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.patch(
            factories.RobotAccountFactory.get_url(account),
            {"username": "builder"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual([self.member_url], response.data["users"])
        self.assertEqual(self.member_url, response.data["responsible_user"])

    def test_staff_can_resave_existing_user_without_consent(self):
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.RobotAccountFactory.get_url(account),
            {
                "username": "builder",
                "users": [self.member_url],
                "responsible_user": self.member_url,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        account.refresh_from_db()
        self.assertCountEqual([self.member], list(account.users.all()))
        self.assertEqual(account.responsible_user, self.member)

    def test_staff_cannot_add_another_user_without_consent(self):
        account = self._account_with_member()
        newcomer = self.fixture.manager
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.RobotAccountFactory.get_url(account),
            {
                "users": [
                    self.member_url,
                    structure_factories.UserFactory.get_url(newcomer),
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(newcomer.full_name, str(response.data))
        self.assertNotIn(self.member.full_name, str(response.data))

    def test_provider_member_on_consumer_side_sees_every_user(self):
        account = self._account_with_member()
        self.fixture.project.add_user(self.fixture.service_owner, ProjectRole.MEMBER)
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(factories.RobotAccountFactory.get_url(account))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [self.member.uuid.hex],
            [user["uuid"] for user in response.data["users"]],
        )
        self.assertEqual(
            self.member.uuid.hex, response.data["responsible_user"]["uuid"]
        )
        self.assertEqual(1, len(response.data["user_keys"]))

    def test_provider_member_on_consumer_side_can_detach_user(self):
        account = self._account_with_member()
        self.fixture.project.add_user(self.fixture.service_owner, ProjectRole.MEMBER)
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.patch(
            factories.RobotAccountFactory.get_url(account),
            {"users": [], "responsible_user": None},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        account.refresh_from_db()
        self.assertFalse(account.users.exists())
        self.assertIsNone(account.responsible_user)

    @override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False)
    def test_provider_detail_shows_user_when_enforcement_is_off(self):
        account = self._account_with_member()
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(factories.RobotAccountFactory.get_url(account))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [self.member.uuid.hex],
            [user["uuid"] for user in response.data["users"]],
        )
        self.assertEqual(
            self.member.uuid.hex, response.data["responsible_user"]["uuid"]
        )

    def test_provider_list_consent_lookup_does_not_repeat_per_account(self):
        for index in range(3):
            account = factories.RobotAccountFactory(
                resource=self.resource, type=f"t{index}"
            )
            account.users.add(self.member)
            account.responsible_user = self.member
            account.save(update_fields=["responsible_user"])
        self.client.force_authenticate(self.fixture.service_owner)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        consent_queries = [
            query
            for query in ctx.captured_queries
            if "userofferingconsent" in query["sql"].lower()
        ]
        terms_queries = [
            query
            for query in ctx.captured_queries
            if "offeringtermsofservice" in query["sql"].lower()
        ]
        self.assertEqual(len(consent_queries), 1)
        self.assertEqual(len(terms_queries), 1)

    def _row(self, response, account):
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = response.data
        if isinstance(rows, dict):
            rows = rows["results"]
        return next(row for row in rows if row["uuid"] == account.uuid.hex)


@override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class RobotAccountConsentWithoutTermsTest(test.APITestCase):
    def test_enforcement_without_active_terms_does_not_block_attach(self):
        fixture = fixtures.MarketplaceFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_RESOURCE_ROBOT_ACCOUNT)
        self.client.force_authenticate(fixture.service_owner)
        response = self.client.post(
            factories.RobotAccountFactory.get_list_url(),
            {
                "resource": factories.ResourceFactory.get_url(fixture.resource),
                "type": "cicd",
                "users": [structure_factories.UserFactory.get_url(fixture.admin)],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


@override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class RobotAccountLinkableUsersTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.resource = self.fixture.resource
        self.project_user = self.fixture.admin
        self.organization_user = self.fixture.owner
        # Created up front: fixture users are lazy and must exist before a request.
        self.member_without_consent = self.fixture.member
        self.outsider = structure_factories.UserFactory()
        self.terms = models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Test ToS",
            version="1.0",
            is_active=True,
        )
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="robot_account_users"
        )

    def _grant(self, permission=PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT):
        CustomerRole.OWNER.add_permission(permission)

    def _uuids(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {row["uuid"] for row in response.data}

    def test_provider_sees_only_users_with_active_consent(self):
        self._grant()
        _grant_offering_consent(self.project_user, self.offering)
        _grant_offering_consent(self.organization_user, self.offering)
        revoked = self.fixture.manager
        _grant_offering_consent(revoked, self.offering, revoked=True)
        self.client.force_authenticate(self.fixture.service_owner)
        uuids = self._uuids(self.client.get(self.url))
        self.assertIn(self.project_user.uuid.hex, uuids)
        self.assertIn(self.organization_user.uuid.hex, uuids)
        self.assertNotIn(revoked.uuid.hex, uuids)
        self.assertNotIn(self.member_without_consent.uuid.hex, uuids)

    def test_staff_sees_only_users_with_active_consent(self):
        _grant_offering_consent(self.project_user, self.offering)
        self.client.force_authenticate(self.fixture.staff)
        uuids = self._uuids(self.client.get(self.url))
        self.assertIn(self.project_user.uuid.hex, uuids)
        self.assertNotIn(self.organization_user.uuid.hex, uuids)

    def test_every_resource_user_is_returned_without_active_terms(self):
        self._grant()
        self.terms.is_active = False
        self.terms.save(update_fields=["is_active"])
        self.client.force_authenticate(self.fixture.service_owner)
        uuids = self._uuids(self.client.get(self.url))
        self.assertTrue(
            {
                self.project_user.uuid.hex,
                self.organization_user.uuid.hex,
                self.member_without_consent.uuid.hex,
            }
            <= uuids
        )

    @override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False)
    def test_every_resource_user_is_returned_when_enforcement_is_off(self):
        self._grant()
        self.client.force_authenticate(self.fixture.service_owner)
        uuids = self._uuids(self.client.get(self.url))
        self.assertIn(self.project_user.uuid.hex, uuids)
        self.assertIn(self.organization_user.uuid.hex, uuids)

    def test_users_outside_the_resource_are_never_returned(self):
        self._grant()
        _grant_offering_consent(self.outsider, self.offering)
        self.client.force_authenticate(self.fixture.service_owner)
        self.assertNotIn(self.outsider.uuid.hex, self._uuids(self.client.get(self.url)))

    def test_create_permission_is_enough(self):
        self._grant(PermissionEnum.CREATE_RESOURCE_ROBOT_ACCOUNT)
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_provider_without_robot_account_permission_is_forbidden(self):
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unrelated_user_cannot_see_the_resource(self):
        self._grant()
        self.client.force_authenticate(self.outsider)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_full_name_search_narrows_the_list(self):
        self._grant()
        for user in (self.project_user, self.organization_user):
            _grant_offering_consent(user, self.offering)
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(self.url, {"full_name": self.project_user.full_name})
        self.assertEqual({self.project_user.uuid.hex}, self._uuids(response))

    def test_user_keyword_matches_email_and_username(self):
        self._grant()
        by_email = structure_factories.UserFactory(email="keyword-mail@example.com")
        by_username = structure_factories.UserFactory(username="keyword-login")
        for user in (by_email, by_username):
            self.fixture.project.add_user(user, ProjectRole.MEMBER)
            _grant_offering_consent(user, self.offering)
        self.client.force_authenticate(self.fixture.service_owner)
        self.assertEqual(
            {by_email.uuid.hex},
            self._uuids(self.client.get(self.url, {"user_keyword": "keyword-mail"})),
        )
        self.assertEqual(
            {by_username.uuid.hex},
            self._uuids(self.client.get(self.url, {"user_keyword": "keyword-login"})),
        )
        self.assertEqual(
            set(), self._uuids(self.client.get(self.url, {"user_keyword": "no-match"}))
        )

    def test_users_are_ordered_by_name(self):
        self._grant()
        # Created in reverse alphabetical order so the result cannot follow ids.
        zed = structure_factories.UserFactory(first_name="Zed", last_name="Order")
        amy = structure_factories.UserFactory(first_name="Amy", last_name="Order")
        for user in (zed, amy):
            self.fixture.project.add_user(user, ProjectRole.MEMBER)
            _grant_offering_consent(user, self.offering)
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(self.url, {"user_keyword": "Order"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [amy.uuid.hex, zed.uuid.hex], [row["uuid"] for row in response.data]
        )

    def test_response_carries_the_fields_the_picker_posts_back(self):
        self._grant()
        _grant_offering_consent(self.project_user, self.offering)
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = next(r for r in response.data if r["uuid"] == self.project_user.uuid.hex)
        self.assertEqual(
            structure_factories.UserFactory.get_url(self.project_user), row["url"]
        )
        self.assertEqual(self.project_user.full_name, row["full_name"])
        self.assertEqual(self.project_user.email, row["email"])

    def test_listed_user_can_be_linked(self):
        self._grant(PermissionEnum.CREATE_RESOURCE_ROBOT_ACCOUNT)
        _grant_offering_consent(self.project_user, self.offering)
        self.client.force_authenticate(self.fixture.service_owner)
        row = next(
            r
            for r in self.client.get(self.url).data
            if r["uuid"] == self.project_user.uuid.hex
        )
        response = self.client.post(
            factories.RobotAccountFactory.get_list_url(),
            {
                "resource": factories.ResourceFactory.get_url(self.resource),
                "type": "cicd",
                "users": [row["url"]],
                "responsible_user": row["url"],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
