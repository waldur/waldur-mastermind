from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core.models import get_ssh_key_fingerprints
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class RobotAccountTest(test.APITransactionTestCase):
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


class RobotAccountStateTransitionTest(test.APITransactionTestCase):
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

        with mock.patch(
            "waldur_mastermind.marketplace.log.event_logger.marketplace_robot_account.info"
        ) as logger_mock:
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

                from_state = models.RobotAccount.States.CHOICES[initial_state - 1][1]
                to_state = models.RobotAccount.States.CHOICES[expected_state - 1][1]
                expected_message = f"Robot account waldur has been updated. Robot account 'waldur' state changed from '{from_state}' to '{to_state}'."
                self.assertEqual(state_change_calls[0][0][0], expected_message)

    def test_requested_to_creating_transition(self):
        """Test transition from REQUESTED to CREATING"""
        self.verify_state_transition(
            initial_state=models.RobotAccount.States.REQUESTED,
            action="set_state_creating",
            expected_state=models.RobotAccount.States.CREATING,
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
            initial_state=models.RobotAccount.States.OK,
            action="set_state_request_deletion",
            expected_state=models.RobotAccount.States.REQUESTED_DELETION,
        )

    def test_requested_deletion_to_deleted_transition(self):
        """Test transition from REQUESTED_DELETION to DELETED"""
        self.verify_state_transition(
            initial_state=models.RobotAccount.States.REQUESTED_DELETION,
            action="set_state_deleted",
            expected_state=models.RobotAccount.States.DELETED,
        )

    def test_transition_to_error_state(self):
        """Test transition to ERROR state with error message"""
        self.verify_state_transition(
            initial_state=models.RobotAccount.States.OK,
            action="set_state_erred",
            expected_state=models.RobotAccount.States.ERROR,
            data={"error_message": "Something went wrong"},
        )

    def test_invalid_transitions(self):
        """Test various invalid state transitions"""
        invalid_transitions = [
            (models.RobotAccount.States.REQUESTED, "set_state_deleted"),
            (models.RobotAccount.States.CREATING, "set_state_request_deletion"),
            (models.RobotAccount.States.DELETED, "set_state_ok"),
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
            initial_state=models.RobotAccount.States.OK,
            action="set_state_erred",
            expected_state=models.RobotAccount.States.ERROR,
            data={"error_message": error_message},
        )
        self.robot_account.refresh_from_db()
        self.assertEqual(self.robot_account.error_message, error_message)

    def test_error_state_without_message(self):
        """Test transition to error state without providing message"""
        self.verify_state_transition(
            initial_state=models.RobotAccount.States.OK,
            action="set_state_erred",
            expected_state=models.RobotAccount.States.ERROR,
            data={},
        )
        self.robot_account.refresh_from_db()
        # Check that the error message is empty
        self.assertEqual(self.robot_account.error_message, "")

    def test_robot_account_state_filtering(self):
        """Test endpoint filtering robot accounts by state"""
        # Create accounts in different states
        ok_account = factories.RobotAccountFactory(
            resource=self.resource, state=models.RobotAccount.States.OK, type="test1"
        )
        creating_account = factories.RobotAccountFactory(
            resource=self.resource,
            state=models.RobotAccount.States.CREATING,
            type="test2",
        )

        url = factories.RobotAccountFactory.get_list_url()
        self.client.force_login(self.fixture.staff)

        # Test filtering for OK state
        response = self.client.get(url, {"state": models.RobotAccount.States.OK})
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
        response = self.client.get(url, {"state": models.RobotAccount.States.CREATING})
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


class RobotAccountAccessTest(test.APITransactionTestCase):
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
        self.robot_account.state = models.RobotAccount.States.CREATING
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
