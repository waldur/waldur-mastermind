import datetime

import httpx
import respx
from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
)
from waldur_core.structure.enums import ProjectKind
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import CourseAccountState
from waldur_mastermind.marketplace.tests import factories, fixtures

COURSE_ACCOUNT_URL = "http://example.com/api/course-accounts"
COURSE_ACCOUNT_TOKEN_URL = "http://example.com/api/token"


@override_waldur_core_settings(
    COURSE_ACCOUNT_USE_API=True,  # Note: typo exists in original code
    COURSE_ACCOUNT_URL=COURSE_ACCOUNT_URL,
    COURSE_ACCOUNT_TOKEN_URL=COURSE_ACCOUNT_TOKEN_URL,
    COURSE_ACCOUNT_TOKEN_CLIENT_ID="test-client-id",
    COURSE_ACCOUNT_TOKEN_SECRET="test-client-secret",
)
@ddt
class CourseAccountPermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Setup respx for API mocking
        respx.start()

        # Mock course account API responses
        self.test_username = "test_user"
        self.test_email = "test_user@example.com"
        self.test_token = "test-token"
        self.test_user = self.fixture.user

        course_account_response = {
            "tempAccount": {
                "username": self.test_username,
                "email": self.test_email,
            }
        }

        # Mock token request
        respx.post(COURSE_ACCOUNT_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": self.test_token})
        )

        # Mock course account creation
        respx.post(COURSE_ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json=course_account_response)
        )

        # Mock course account deletion
        respx.put(COURSE_ACCOUNT_URL + f"/{self.test_user.username}/close").mock(
            return_value=httpx.Response(200, json={})
        )

        respx.get(COURSE_ACCOUNT_URL + f"/{self.test_user.username}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tempAccounts": [
                        {
                            "username": "test-username",
                        }
                    ]
                },
            )
        )

        # Create a course project for course accounts
        self.course_project = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            end_date=datetime.date.today() + datetime.timedelta(days=30),
        )

        # Add MANAGE_COURSE_ACCOUNT permission to relevant roles
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_COURSE_ACCOUNT)
        ProjectRole.MANAGER.add_permission(PermissionEnum.MANAGE_COURSE_ACCOUNT)
        ProjectRole.ADMIN.add_permission(PermissionEnum.MANAGE_COURSE_ACCOUNT)

        # Setup users with appropriate roles for both projects
        self.fixture.project.add_user(self.fixture.manager, ProjectRole.MANAGER)
        self.fixture.project.add_user(self.fixture.admin, ProjectRole.ADMIN)
        self.fixture.project.customer.add_user(self.fixture.owner, ProjectRole.MANAGER)

        # Add users to course project as well
        self.course_project.add_user(self.fixture.manager, ProjectRole.MANAGER)
        self.course_project.add_user(self.fixture.admin, ProjectRole.ADMIN)
        self.course_project.add_user(self.fixture.owner, ProjectRole.MANAGER)

    def tearDown(self):
        respx.stop()
        super().tearDown()

    @data("staff", "manager", "admin", "owner")
    def test_user_can_create_course_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "project": self.course_project.uuid,
                "email": "test@example.com",
                "description": "Test course account",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        # Check that course account was created
        account = models.CourseAccount.objects.filter(
            project=self.course_project,
            email="test@example.com",
        ).first()
        self.assertIsNotNone(account)
        # Check that user was created from API response
        self.assertEqual(account.user.username, self.test_username)
        self.assertEqual(account.user.email, self.test_email)

    @data("user", "customer_support", "member")
    def test_unauthorized_user_can_not_create_course_account(self, user):
        """Test that unauthorized user can't create course account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "project": self.course_project.uuid,
                "email": "test@example.com",
                "description": "Test course account",
            },
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
            f"Expected status code 403, got: {response.status_code}. Response data: {response.data}",
        )

    @data("staff", "manager", "admin", "owner")
    def test_authorized_user_can_get_course_account(self, user):
        """Test that authorized user can get course account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
        )
        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.get(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            response.data,
        )

    @data("user", "customer_support", "member")
    def test_unauthorized_user_can_not_get_course_account(self, user):
        """Test that unauthorized user can't get course account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
        )
        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.get(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            f"Expected status code 404, got: {response.status_code}. Response data: {response.data}",
        )

    @data("staff", "manager", "admin", "owner")
    def test_authorized_user_can_delete_course_account(self, user):
        """Test that authorized user can delete course account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
        )
        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.delete(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_204_NO_CONTENT,
            response.data,
        )
        account.refresh_from_db()
        self.assertEqual(account.state, CourseAccountState.CLOSED)

    @data("user", "customer_support", "member")
    def test_unauthorized_user_can_not_delete_course_account(self, user):
        """Test that unauthorized user can't delete course account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
        )
        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.delete(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            f"Expected status code 404, got: {response.status_code}. Response data: {response.data}",
        )

    def test_delete_course_account_sets_deactivation_reason(self):
        """Test that deleting a course account sets deactivation_reason on the user."""
        self.client.force_authenticate(self.fixture.staff)
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
        )
        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.delete(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_204_NO_CONTENT,
            response.data,
        )
        self.test_user.refresh_from_db()
        self.assertFalse(self.test_user.is_active)
        self.assertEqual(
            self.test_user.deactivation_reason,
            f"Course account {account.uuid} closed",
        )

    def test_delete_course_account_not_found_at_backend_sets_deactivation_reason(self):
        """Test that deactivation_reason is set when backend account is not found."""
        self.client.force_authenticate(self.fixture.staff)
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
        )
        # Override the GET mock to return 404 (account not found at backend)
        respx.get(COURSE_ACCOUNT_URL + f"/{self.test_user.username}").mock(
            return_value=httpx.Response(404)
        )
        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.delete(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_204_NO_CONTENT,
            response.data,
        )
        self.test_user.refresh_from_db()
        self.assertFalse(self.test_user.is_active)
        self.assertEqual(
            self.test_user.deactivation_reason,
            f"Course account {account.uuid} not found at backend",
        )

    def test_delete_erred_course_account_without_user(self):
        """Deleting an ERRED account with no user (task never created one) must not crash."""
        self.client.force_authenticate(self.fixture.staff)
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=None,
            email="no-user@example.com",
            state=CourseAccountState.ERRED,
        )
        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        account.refresh_from_db()
        self.assertEqual(account.state, CourseAccountState.CLOSED)

    def test_update_operations_are_disabled(self):
        """Test that update operations are disabled"""
        self.client.force_authenticate(self.fixture.staff)
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
        )
        url = factories.CourseAccountFactory.get_url(account)

        # Test partial update
        response = self.client.patch(url, {"description": "Updated description"})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        # Test full update
        response = self.client.put(
            url,
            {
                "project": self.fixture.project.uuid,
                "user": self.test_user.uuid,
                "email": "updated@example.com",
                "description": "Updated description",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_list_course_accounts_filtering_by_project(self):
        """Test that course accounts can be filtered by project"""
        self.client.force_authenticate(self.fixture.staff)

        # Create accounts in different projects
        account1 = factories.CourseAccountFactory(project=self.course_project)
        other_project = structure_factories.ProjectFactory(kind=ProjectKind.COURSE)
        factories.CourseAccountFactory(project=other_project)

        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url, {"project_uuid": self.course_project.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(account1.uuid))

    def test_list_course_accounts_filtering_by_state(self):
        """Test that course accounts can be filtered by state"""
        self.client.force_authenticate(self.fixture.staff)

        # Create accounts with different states
        account1 = factories.CourseAccountFactory(
            project=self.course_project, state=CourseAccountState.OK
        )
        factories.CourseAccountFactory(
            project=self.course_project, state=CourseAccountState.CLOSED
        )

        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url, {"state": CourseAccountState.OK.name})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(account1.uuid))

    def test_list_course_accounts_filtering_by_email(self):
        """Test that course accounts can be filtered by email"""
        self.client.force_authenticate(self.fixture.staff)

        # Create accounts with different emails
        account1 = factories.CourseAccountFactory(
            project=self.course_project, email="test@example.com"
        )
        factories.CourseAccountFactory(
            project=self.course_project, email="other@example.com"
        )

        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url, {"email": "test"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(account1.uuid))

    def test_course_account_state_transitions(self):
        """Test course account state transitions"""
        account = factories.CourseAccountFactory(
            project=self.course_project, state=CourseAccountState.OK
        )

        # Test OK -> CLOSED transition
        account.set_state_closed()
        self.assertEqual(account.state, CourseAccountState.CLOSED)

        # Create another account to test ERRED -> OK transition
        account2 = factories.CourseAccountFactory(
            project=self.course_project, state=CourseAccountState.ERRED
        )
        account2.set_state_ok()
        self.assertEqual(account2.state, CourseAccountState.OK)

    def test_staff_can_see_all_course_accounts(self):
        """Test that staff users can see all course accounts"""
        self.client.force_authenticate(self.fixture.staff)

        # Create accounts in different projects
        factories.CourseAccountFactory(project=self.course_project)
        other_project = structure_factories.ProjectFactory(kind=ProjectKind.COURSE)
        factories.CourseAccountFactory(project=other_project)

        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_project_user_can_only_see_own_project_accounts(self):
        """Test that project users can only see accounts from their projects"""
        self.client.force_authenticate(self.fixture.manager)

        # Create account in user's project
        account1 = factories.CourseAccountFactory(project=self.course_project)

        # Create account in different project
        other_project = structure_factories.ProjectFactory(kind=ProjectKind.COURSE)
        factories.CourseAccountFactory(project=other_project)

        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(account1.uuid))

    @data("staff", "manager", "admin", "owner")
    def test_authorized_user_can_retry_erred_course_account(self, user):
        """Test that authorized user can retry an ERRED course account."""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
            state=CourseAccountState.ERRED,
            error_message="External API error",
            error_traceback="Traceback ...",
        )
        url = factories.CourseAccountFactory.get_url(account) + "retry/"
        response = self.client.post(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_202_ACCEPTED,
            response.data,
        )
        account.refresh_from_db()
        self.assertEqual(account.state, CourseAccountState.PENDING)
        self.assertEqual(account.error_message, "")
        self.assertEqual(account.error_traceback, "")

    @data("user", "customer_support", "member")
    def test_unauthorized_user_can_not_retry_course_account(self, user):
        """Test that unauthorized user can't retry a course account."""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
            state=CourseAccountState.ERRED,
        )
        url = factories.CourseAccountFactory.get_url(account) + "retry/"
        response = self.client.post(url)
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
            f"Expected 403 or 404, got: {response.status_code}. Response data: {response.data}",
        )

    def test_retry_ok_course_account_returns_conflict(self):
        """Test that retrying an OK account returns 409 Conflict."""
        self.client.force_authenticate(self.fixture.staff)
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
            state=CourseAccountState.OK,
        )
        url = factories.CourseAccountFactory.get_url(account) + "retry/"
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_retry_closed_course_account_returns_conflict(self):
        """Test that retrying a CLOSED account returns 409 Conflict."""
        self.client.force_authenticate(self.fixture.staff)
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
            state=CourseAccountState.CLOSED,
        )
        url = factories.CourseAccountFactory.get_url(account) + "retry/"
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


@override_waldur_core_settings(
    COURSE_ACCOUNT_USE_API=True,  # Note: typo exists in original code
    COURSE_ACCOUNT_URL=COURSE_ACCOUNT_URL,
    COURSE_ACCOUNT_TOKEN_URL=COURSE_ACCOUNT_TOKEN_URL,
    COURSE_ACCOUNT_TOKEN_CLIENT_ID="test-client-id",
    COURSE_ACCOUNT_TOKEN_SECRET="test-client-secret",
)
class CourseAccountHandlerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Setup respx for API mocking
        respx.start()

        # Mock course account API responses
        self.test_username = "test_user"
        self.test_email = "test_user@example.com"
        self.test_token = "test-token"
        self.test_user = self.fixture.user

        # Mock token request
        respx.post(COURSE_ACCOUNT_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": self.test_token})
        )

        # Create a course project for course accounts
        self.course_project = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer, kind=ProjectKind.COURSE
        )

    def tearDown(self):
        respx.stop()
        super().tearDown()

    def test_course_accounts_closed_when_project_deleted_with_valid_token(self):
        """Test that course accounts are closed when project is deleted and API token is available"""
        # Create course accounts in the project
        account1 = factories.CourseAccountFactory(
            project=self.course_project,
            state=CourseAccountState.OK,
            user=self.test_user,
        )
        account2 = factories.CourseAccountFactory(
            project=self.course_project, state=CourseAccountState.OK
        )

        # Mock GET requests to check if accounts exist
        respx.get(COURSE_ACCOUNT_URL + f"/{account1.user.username}").mock(
            return_value=httpx.Response(
                200, json={"tempAccounts": [{"username": account1.user.username}]}
            )
        )
        respx.get(COURSE_ACCOUNT_URL + f"/{account2.user.username}").mock(
            return_value=httpx.Response(
                200, json={"tempAccounts": [{"username": account2.user.username}]}
            )
        )

        # Mock successful close account calls
        respx.put(COURSE_ACCOUNT_URL + f"/{account1.user.username}/close").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.put(COURSE_ACCOUNT_URL + f"/{account2.user.username}/close").mock(
            return_value=httpx.Response(200, json={})
        )

        # Delete the project - this should trigger the handler
        self.course_project.delete()

        # Verify that close account API was called for each account
        close_requests = [
            call
            for call in respx.calls
            if call.request.method == "PUT" and "/close" in str(call.request.url)
        ]
        self.assertEqual(len(close_requests), 2)

    def test_course_accounts_handler_skips_when_token_request_fails(self):
        """Test that handler gracefully handles token request failures"""
        # Create course accounts in the project
        factories.CourseAccountFactory(
            project=self.course_project, state=CourseAccountState.OK
        )

        # Mock failed token request
        respx.reset()
        respx.post(COURSE_ACCOUNT_TOKEN_URL).mock(
            return_value=httpx.Response(500, json={"error": "Internal server error"})
        )

        # Delete the project - this should trigger the handler but skip account closure
        self.course_project.delete()

        # Verify no close account requests were made since token failed
        close_requests = [
            call
            for call in respx.calls
            if call.request.method == "PUT" and "/close" in str(call.request.url)
        ]
        self.assertEqual(len(close_requests), 0)

    def test_course_accounts_handler_processes_account_not_found_case(self):
        """Test handler behavior when course account is not found in remote API"""
        # Create course account in the project
        account = factories.CourseAccountFactory(
            project=self.course_project,
            state=CourseAccountState.OK,
            user=self.test_user,
        )

        # Mock GET request to return 404 (account not found)
        respx.get(COURSE_ACCOUNT_URL + f"/{account.user.username}").mock(
            return_value=httpx.Response(404, json={"error": "Account not found"})
        )

        # Delete the project - this should trigger the handler
        self.course_project.delete()

        # Verify no close request was made since account wasn't found
        close_requests = [
            call
            for call in respx.calls
            if call.request.method == "PUT" and "/close" in str(call.request.url)
        ]
        self.assertEqual(len(close_requests), 0)

        # Verify the account is now in CLOSED state locally
        account.refresh_from_db()
        self.assertEqual(account.state, CourseAccountState.CLOSED)

        # Verify the user is deactivated
        account.user.refresh_from_db()
        self.assertFalse(account.user.is_active)

    def test_course_accounts_handler_only_processes_project_accounts(self):
        """Test that handler only processes accounts belonging to the deleted project"""
        # Create course accounts in the project being deleted
        account1 = factories.CourseAccountFactory(
            project=self.course_project,
            state=CourseAccountState.OK,
            user=self.test_user,
        )

        # Create course accounts in a different project
        other_project = structure_factories.ProjectFactory(kind=ProjectKind.COURSE)
        account2 = factories.CourseAccountFactory(
            project=other_project, state=CourseAccountState.OK
        )

        # Mock GET request for the account that will be processed
        respx.get(COURSE_ACCOUNT_URL + f"/{account1.user.username}").mock(
            return_value=httpx.Response(
                200, json={"tempAccounts": [{"username": account1.user.username}]}
            )
        )

        # Mock close account calls
        respx.put(COURSE_ACCOUNT_URL + f"/{account1.user.username}/close").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.put(COURSE_ACCOUNT_URL + f"/{account2.user.username}/close").mock(
            return_value=httpx.Response(200, json={})
        )

        # Delete only the first project
        self.course_project.delete()

        # Verify only the first account's close was attempted
        close_requests = [
            call
            for call in respx.calls
            if call.request.method == "PUT" and "/close" in str(call.request.url)
        ]
        self.assertEqual(len(close_requests), 1)
        self.assertIn(account1.user.username, str(close_requests[0].request.url))

    def test_course_accounts_handler_no_op_when_no_accounts(self):
        """Test that handler works correctly when project has no course accounts"""
        # No course accounts created for this project

        # Delete the project - this should trigger the handler but do nothing
        self.course_project.delete()

        # Verify no close account requests were made
        close_requests = [
            call
            for call in respx.calls
            if call.request.method == "PUT" and "/close" in str(call.request.url)
        ]
        self.assertEqual(len(close_requests), 0)

    def test_course_account_creation_api_error_handling(self):
        """Test that HTTP errors during course account creation are properly handled"""
        self.client.force_authenticate(self.fixture.staff)

        # Mock API to return HTTP error
        respx.reset()
        respx.post(COURSE_ACCOUNT_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": self.test_token})
        )
        respx.post(COURSE_ACCOUNT_URL).mock(
            return_value=httpx.Response(500, json={"error": "Internal server error"})
        )

        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "project": self.course_project.uuid,
                "email": "test@example.com",
                "description": "Test course account",
            },
        )

        # The API should return an error status due to the HTTP 500 from external service
        # The exception is caught and converted to a 400 Bad Request
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # No course account should be created in the database
        account = models.CourseAccount.objects.filter(
            project=self.course_project,
            email="test@example.com",
        ).first()
        self.assertIsNone(account)

    def test_course_account_deletion_api_error_handling(self):
        """Test that HTTP errors during course account deletion are properly handled"""
        self.client.force_authenticate(self.fixture.staff)

        # Create a course account to delete
        account = factories.CourseAccountFactory(
            project=self.course_project,
            user=self.test_user,
            state=CourseAccountState.OK,
        )

        # Mock API to return HTTP error during deletion
        respx.reset()
        respx.post(COURSE_ACCOUNT_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": self.test_token})
        )
        respx.get(COURSE_ACCOUNT_URL + f"/{self.test_user.username}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "tempAccounts": [
                        {
                            "username": self.test_user.username,
                        }
                    ]
                },
            )
        )
        respx.put(COURSE_ACCOUNT_URL + f"/{self.test_user.username}/close").mock(
            return_value=httpx.Response(500, json={"error": "Internal server error"})
        )

        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.delete(url)

        # The API should return an error status due to the HTTP 500 from external service
        # The exception is caught and converted to a 400 Bad Request
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Refresh the course account from database
        account.refresh_from_db()

        # Check if the account state changed (might be ERRED or still OK depending on transaction handling)
        # Since the error is properly logged, the important thing is that the exception was caught and handled
        # We verify this by checking the HTTP status response and that no CLOSED state was reached
        self.assertNotEqual(account.state, CourseAccountState.CLOSED)

        # If the account is in ERRED state, check error details
        if account.state == CourseAccountState.ERRED:
            self.assertIsNotNone(account.error_message)
            self.assertIsNotNone(account.error_traceback)
            self.assertIn("Internal server error", account.error_message)


@override_waldur_core_settings(
    COURSE_ACCOUNT_USE_API=True,
    COURSE_ACCOUNT_URL=COURSE_ACCOUNT_URL,
    COURSE_ACCOUNT_TOKEN_URL=COURSE_ACCOUNT_TOKEN_URL,
    COURSE_ACCOUNT_TOKEN_CLIENT_ID="test-client-id",
    COURSE_ACCOUNT_TOKEN_SECRET="test-client-secret",
)
@ddt
class CourseAccountBulkCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Setup respx for API mocking
        respx.start()

        # Mock token request
        self.test_token = "test-token"
        respx.post(COURSE_ACCOUNT_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": self.test_token})
        )

        # Create a course project for course accounts
        self.course_project = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            end_date=datetime.date.today() + datetime.timedelta(days=30),
        )

        # Add MANAGE_COURSE_ACCOUNT permission to relevant roles
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_COURSE_ACCOUNT)
        ProjectRole.MANAGER.add_permission(PermissionEnum.MANAGE_COURSE_ACCOUNT)
        ProjectRole.ADMIN.add_permission(PermissionEnum.MANAGE_COURSE_ACCOUNT)

        # Setup users with appropriate roles for course project
        self.course_project.add_user(self.fixture.manager, ProjectRole.MANAGER)
        self.course_project.add_user(self.fixture.admin, ProjectRole.ADMIN)
        self.course_project.customer.add_user(self.fixture.owner, CustomerRole.OWNER)

        # Setup URL for bulk creation
        self.bulk_url = factories.CourseAccountFactory.get_list_url() + "create_bulk/"

    def tearDown(self):
        respx.stop()
        super().tearDown()

    @data("staff", "manager", "admin", "owner")
    def test_authorized_user_can_bulk_create_course_accounts(self, user):
        """Test that authorized users can bulk create course accounts.

        The endpoint returns 202 immediately with PENDING accounts;
        the external API calls happen asynchronously via Celery tasks.
        """
        self.client.force_authenticate(getattr(self.fixture, user))

        payload = {
            "course_accounts": [
                {"email": "test1@example.com", "description": "Test account 1"},
                {"email": "test2@example.com", "description": "Test account 2"},
                {"email": "test3@example.com", "description": ""},
            ],
            "project": str(self.course_project.uuid),
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        # Verify all accounts were created in PENDING state
        accounts = models.CourseAccount.objects.filter(project=self.course_project)
        self.assertEqual(accounts.count(), 3)
        self.assertTrue(accounts.filter(state=CourseAccountState.PENDING).count() == 3)

        # Verify response structure
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 3)
        for i, account_data in enumerate(response.data, 1):
            self.assertIn("uuid", account_data)
            self.assertIn("email", account_data)
            self.assertEqual(account_data["email"], f"test{i}@example.com")
            self.assertEqual(account_data["state"], "Pending")

    @data("user", "customer_support", "member")
    def test_unauthorized_user_cannot_bulk_create_course_accounts(self, user):
        """Test that unauthorized users cannot bulk create course accounts"""
        self.client.force_authenticate(getattr(self.fixture, user))

        payload = {
            "course_accounts": [
                {"email": "test@example.com", "description": "Test account"},
            ],
            "project": str(self.course_project.uuid),
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    def test_bulk_create_validates_project_uuid(self):
        """Test that bulk create validates project UUID"""
        self.client.force_authenticate(self.fixture.staff)

        payload = {
            "course_accounts": [
                {"email": "test@example.com", "description": "Test account"},
            ],
            "project": "invalid-uuid",
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project", response.data)

    def test_bulk_create_validates_email_format(self):
        """Test that bulk create validates email format"""
        self.client.force_authenticate(self.fixture.staff)

        payload = {
            "course_accounts": [
                {"email": "invalid-email", "description": "Test account"},
            ],
            "project": str(self.course_project.uuid),
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("course_accounts", response.data)

    def test_bulk_create_requires_course_project(self):
        """Test that bulk create requires a course project (not regular project)"""
        self.client.force_authenticate(self.fixture.staff)

        # Create a non-course project
        regular_project = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.DEFAULT,
        )

        payload = {
            "course_accounts": [
                {"email": "test@example.com", "description": "Test account"},
            ],
            "project": str(regular_project.uuid),
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project", response.data)

    def test_bulk_create_requires_project_end_date(self):
        """Test that bulk create requires project to have an end_date"""
        self.client.force_authenticate(self.fixture.staff)

        # Create a course project without end_date
        project_no_end_date = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            end_date=None,
        )

        payload = {
            "course_accounts": [
                {"email": "test@example.com", "description": "Test account"},
            ],
            "project": str(project_no_end_date.uuid),
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_create_handles_api_errors(self):
        """Test that bulk create returns 202 immediately; API errors surface in the task.

        The view creates PENDING accounts and returns before calling the external API.
        External API failures are handled by the background Celery task.
        """
        self.client.force_authenticate(self.fixture.staff)

        payload = {
            "course_accounts": [
                {"email": "test@example.com", "description": "Test account"},
            ],
            "project": str(self.course_project.uuid),
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        # Account is created in PENDING state (external API call deferred to task)
        accounts = models.CourseAccount.objects.filter(project=self.course_project)
        self.assertEqual(accounts.count(), 1)
        self.assertEqual(accounts.first().state, CourseAccountState.PENDING)

        # Response contains the pending account
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["email"], "test@example.com")
        self.assertEqual(response.data[0]["state"], "Pending")

    def test_bulk_create_with_empty_course_accounts_list(self):
        """Test bulk create with empty course accounts list"""
        self.client.force_authenticate(self.fixture.staff)

        payload = {
            "course_accounts": [],
            "project": str(self.course_project.uuid),
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("course_accounts", response.data)

    def test_bulk_create_deduplicates_emails_in_request(self):
        """Test that duplicate emails in the same request are deduplicated."""
        self.client.force_authenticate(self.fixture.staff)

        payload = {
            "course_accounts": [
                {"email": "test@example.com", "description": "First account"},
                {"email": "test@example.com", "description": "Duplicate account"},
            ],
            "project": str(self.course_project.uuid),
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        # Only one account created (duplicate skipped)
        accounts = models.CourseAccount.objects.filter(
            project=self.course_project, email="test@example.com"
        )
        self.assertEqual(accounts.count(), 1)
        self.assertEqual(len(response.data), 1)

    def test_bulk_create_skips_already_existing_emails(self):
        """Test that emails already in Waldur for the project are skipped."""
        self.client.force_authenticate(self.fixture.staff)

        # Pre-create an account for this email
        models.CourseAccount.objects.create(
            project=self.course_project,
            email="existing@example.com",
            state=CourseAccountState.OK,
        )

        payload = {
            "course_accounts": [
                {"email": "existing@example.com", "description": "Already exists"},
                {"email": "new@example.com", "description": "New account"},
            ],
            "project": str(self.course_project.uuid),
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        # Only the new account is created
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["email"], "new@example.com")
        self.assertEqual(
            models.CourseAccount.objects.filter(project=self.course_project).count(),
            2,  # pre-existing + new
        )

    def test_bulk_create_with_mixed_valid_and_invalid_data(self):
        """Test bulk create returns all accounts in PENDING state immediately.

        External API validation (e.g., blocked email domains) is reported
        by the background task, not by the HTTP response.
        """
        self.client.force_authenticate(self.fixture.staff)

        payload = {
            "course_accounts": [
                {"email": "valid@example.com", "description": "Valid account"},
                {"email": "other@example.com", "description": "Other account"},
            ],
            "project": str(self.course_project.uuid),
        }

        response = self.client.post(self.bulk_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        # Both accounts created in PENDING state
        accounts = models.CourseAccount.objects.filter(project=self.course_project)
        self.assertEqual(accounts.count(), 2)
        self.assertTrue(accounts.filter(state=CourseAccountState.PENDING).count() == 2)

        self.assertEqual(len(response.data), 2)


@override_waldur_core_settings(
    COURSE_ACCOUNT_USE_API=False,  # Disable API calls for these tests
)
class CourseAccountDateFieldsTest(test.APITestCase):
    """Test for CourseAccount serializer project date fields"""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.start_date = datetime.date(2024, 1, 1)
        self.end_date = datetime.date(2024, 12, 31)

        # Create a course project with specific start and end dates
        self.course_project = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            start_date=self.start_date,
            end_date=self.end_date,
        )

        # Create a course account
        self.course_account = factories.CourseAccountFactory(
            project=self.course_project, email="test@example.com"
        )

        # Add permissions
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_COURSE_ACCOUNT)
        self.fixture.project.customer.add_user(self.fixture.owner, CustomerRole.OWNER)

    def test_serializer_includes_project_date_fields(self):
        """Test that serializer returns project_start_date and project_end_date fields"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_url(self.course_account)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("project_start_date", response.data)
        self.assertIn("project_end_date", response.data)
        self.assertIn("project_slug", response.data)

        # Check the date values are correctly formatted
        self.assertEqual(response.data["project_start_date"], self.start_date)
        self.assertEqual(response.data["project_end_date"], self.end_date)

    def test_list_includes_project_date_fields(self):
        """Test that list endpoint includes project date fields"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)

        account_data = response.data[0]
        self.assertIn("project_start_date", account_data)
        self.assertIn("project_end_date", account_data)
        self.assertIn("project_slug", account_data)

    def test_project_dates_are_read_only(self):
        """Test that project date fields are read-only and cannot be modified"""
        # The project date fields are read-only, meaning they come from the related project
        # and cannot be set directly when creating a course account.
        # We verify this by checking that the serializer correctly retrieves dates from the project.
        self.client.force_authenticate(self.fixture.staff)

        # Verify that the dates in the response come from the project
        url = factories.CourseAccountFactory.get_url(self.course_account)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["project_start_date"], self.start_date)
        self.assertEqual(response.data["project_end_date"], self.end_date)

    def test_null_project_start_date(self):
        """Test that null project start_date is serialized as null"""
        project_no_start = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            start_date=None,
            end_date=datetime.date.today() + datetime.timedelta(days=30),
        )

        account = factories.CourseAccountFactory(
            project=project_no_start, email="nostart@example.com"
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["project_start_date"])
        self.assertIsNotNone(response.data["project_end_date"])

    def test_null_project_end_date(self):
        """Test that null project end_date is serialized as null"""
        project_no_end = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            start_date=datetime.date.today(),
            end_date=None,
        )

        account = factories.CourseAccountFactory(
            project=project_no_end, email="noend@example.com"
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data["project_start_date"])
        self.assertIsNone(response.data["project_end_date"])

    def test_both_project_dates_null(self):
        """Test that both null project dates are serialized as null"""
        project_no_dates = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            start_date=None,
            end_date=None,
        )

        account = factories.CourseAccountFactory(
            project=project_no_dates, email="nodates@example.com"
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_url(account)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["project_start_date"])
        self.assertIsNone(response.data["project_end_date"])

    def test_list_with_null_project_dates(self):
        """Test that list endpoint handles accounts with null project dates"""
        project_no_dates = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            start_date=None,
            end_date=None,
        )

        factories.CourseAccountFactory(
            project=project_no_dates, email="nulldates@example.com"
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return both accounts (one with dates, one without) without errors
        self.assertEqual(len(response.data), 2)
        null_account = next(
            a for a in response.data if a["email"] == "nulldates@example.com"
        )
        self.assertIsNone(null_account["project_start_date"])
        self.assertIsNone(null_account["project_end_date"])


@override_waldur_core_settings(
    COURSE_ACCOUNT_USE_API=False,  # Disable API calls for these tests
)
class CourseAccountDateFilterTest(test.APITestCase):
    """Test filtering CourseAccounts by project start and end dates"""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Create course projects with different date ranges
        self.project_jan = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            start_date=datetime.date(2024, 1, 1),
            end_date=datetime.date(2024, 1, 31),
        )

        self.project_jun = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            start_date=datetime.date(2024, 6, 1),
            end_date=datetime.date(2024, 6, 30),
        )

        self.project_dec = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            start_date=datetime.date(2024, 12, 1),
            end_date=datetime.date(2024, 12, 31),
        )

        # Create course accounts for each project
        self.account_jan = factories.CourseAccountFactory(
            project=self.project_jan, email="jan@example.com"
        )
        self.account_jun = factories.CourseAccountFactory(
            project=self.project_jun, email="jun@example.com"
        )
        self.account_dec = factories.CourseAccountFactory(
            project=self.project_dec, email="dec@example.com"
        )

        # Add permissions
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_COURSE_ACCOUNT)
        self.fixture.project.customer.add_user(self.fixture.owner, CustomerRole.OWNER)

    def test_filter_by_project_start_date_after(self):
        """Test filtering accounts by project start date after a given date"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()

        # Get accounts with projects starting after May 1, 2024
        response = self.client.get(url, {"project_start_date_after": "2024-05-01"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        emails = [account["email"] for account in response.data]
        self.assertIn("jun@example.com", emails)
        self.assertIn("dec@example.com", emails)
        self.assertNotIn("jan@example.com", emails)

    def test_filter_by_project_start_date_before(self):
        """Test filtering accounts by project start date before a given date"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()

        # Get accounts with projects starting before July 1, 2024
        response = self.client.get(url, {"project_start_date_before": "2024-07-01"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        emails = [account["email"] for account in response.data]
        self.assertIn("jan@example.com", emails)
        self.assertIn("jun@example.com", emails)
        self.assertNotIn("dec@example.com", emails)

    def test_filter_by_project_end_date_after(self):
        """Test filtering accounts by project end date after a given date"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()

        # Get accounts with projects ending after February 1, 2024
        response = self.client.get(url, {"project_end_date_after": "2024-02-01"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        emails = [account["email"] for account in response.data]
        self.assertIn("jun@example.com", emails)
        self.assertIn("dec@example.com", emails)
        self.assertNotIn("jan@example.com", emails)

    def test_filter_by_project_end_date_before(self):
        """Test filtering accounts by project end date before a given date"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()

        # Get accounts with projects ending before November 1, 2024
        response = self.client.get(url, {"project_end_date_before": "2024-11-01"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        emails = [account["email"] for account in response.data]
        self.assertIn("jan@example.com", emails)
        self.assertIn("jun@example.com", emails)
        self.assertNotIn("dec@example.com", emails)

    def test_filter_by_date_range(self):
        """Test filtering accounts by a date range using both min and max"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()

        # Get accounts with projects starting between March and August 2024
        response = self.client.get(
            url,
            {
                "project_start_date_after": "2024-03-01",
                "project_start_date_before": "2024-08-01",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["email"], "jun@example.com")

    def test_combine_date_filters_with_other_filters(self):
        """Test combining date filters with other existing filters"""
        # Create another account for June project
        factories.CourseAccountFactory(
            project=self.project_jun, email="jun2@example.com"
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()

        # Filter by both project dates and email
        response = self.client.get(
            url,
            {
                "project_start_date_after": "2024-05-01",
                "email": "jun2",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["email"], "jun2@example.com")

    def test_filter_with_invalid_date_format(self):
        """Test that invalid date format is handled gracefully"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()

        # Try with invalid date format
        response = self.client.get(url, {"project_start_date_after": "not-a-date"})

        # Should return 400 Bad Request or ignore the filter
        # The exact behavior depends on the filter implementation
        self.assertIn(
            response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_200_OK]
        )


@override_waldur_core_settings(
    COURSE_ACCOUNT_USE_API=False,  # Disable API calls for these tests
)
class CourseAccountOrderingTest(test.APITestCase):
    """Test ordering CourseAccounts using the OrderingFilter"""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Create course accounts with different attributes
        self.project1 = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            name="Alpha Project",
            start_date=datetime.date(2024, 1, 1),
            end_date=datetime.date(2024, 12, 31),
        )

        self.project2 = structure_factories.ProjectFactory(
            customer=self.fixture.project.customer,
            kind=ProjectKind.COURSE,
            name="Beta Project",
            start_date=datetime.date(2024, 6, 1),
            end_date=datetime.date(2024, 6, 30),
        )

        # Create users with different usernames
        self.user1 = structure_factories.UserFactory(username="alice")
        self.user2 = structure_factories.UserFactory(username="bob")

        # Create course accounts with different attributes
        self.account1 = factories.CourseAccountFactory(
            project=self.project1,
            user=self.user1,
            email="alice@example.com",
            state=CourseAccountState.OK,
        )

        # Use freezegun or manual created field update to ensure different created times
        self.account2 = factories.CourseAccountFactory(
            project=self.project2,
            user=self.user2,
            email="bob@example.com",
            state=CourseAccountState.CLOSED,
        )

        # Add permissions
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_COURSE_ACCOUNT)
        self.fixture.project.customer.add_user(self.fixture.owner, CustomerRole.OWNER)

    def test_order_by_created_ascending(self):
        """Test ordering by created date in ascending order"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url, {"o": "created"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # First created account should come first
        self.assertEqual(response.data[0]["email"], "alice@example.com")
        self.assertEqual(response.data[1]["email"], "bob@example.com")

    def test_order_by_created_descending(self):
        """Test ordering by created date in descending order"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url, {"o": "-created"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # Last created account should come first
        self.assertEqual(response.data[0]["email"], "bob@example.com")
        self.assertEqual(response.data[1]["email"], "alice@example.com")

    def test_order_by_email(self):
        """Test ordering by email"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url, {"o": "email"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # Alphabetically: alice@example.com comes before bob@example.com
        self.assertEqual(response.data[0]["email"], "alice@example.com")
        self.assertEqual(response.data[1]["email"], "bob@example.com")

    def test_order_by_username(self):
        """Test ordering by username using the field alias"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url, {"o": "username"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # Alphabetically: alice comes before bob
        self.assertEqual(response.data[0]["username"], "alice")
        self.assertEqual(response.data[1]["username"], "bob")

    def test_order_by_project_name(self):
        """Test ordering by project name using the field alias"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url, {"o": "project_name"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # Alphabetically: Alpha Project comes before Beta Project
        self.assertEqual(response.data[0]["project_name"], "Alpha Project")
        self.assertEqual(response.data[1]["project_name"], "Beta Project")

    def test_order_by_project_start_date(self):
        """Test ordering by project start date"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url, {"o": "project_start_date"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # January 1 comes before June 1
        self.assertEqual(
            response.data[0]["project_start_date"], datetime.date(2024, 1, 1)
        )
        self.assertEqual(
            response.data[1]["project_start_date"], datetime.date(2024, 6, 1)
        )

    def test_order_by_state(self):
        """Test ordering by state"""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()
        response = self.client.get(url, {"o": "state"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # State OK (1) comes before CLOSED (2)
        self.assertEqual(response.data[0]["state"], "OK")
        self.assertEqual(response.data[1]["state"], "Closed")

    def test_multiple_ordering(self):
        """Test that multiple ordering parameters are handled correctly"""
        # Create additional account with same project but different email
        factories.CourseAccountFactory(
            project=self.project1,
            user=structure_factories.UserFactory(username="charlie"),
            email="charlie@example.com",
            state=CourseAccountState.OK,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.CourseAccountFactory.get_list_url()

        # Order by project_name, then by email
        response = self.client.get(url, {"o": "project_name,email"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)

        # First two should be from Alpha Project (ordered by email)
        self.assertEqual(response.data[0]["project_name"], "Alpha Project")
        self.assertEqual(response.data[0]["email"], "alice@example.com")
        self.assertEqual(response.data[1]["project_name"], "Alpha Project")
        self.assertEqual(response.data[1]["email"], "charlie@example.com")
        # Last should be from Beta Project
        self.assertEqual(response.data[2]["project_name"], "Beta Project")
