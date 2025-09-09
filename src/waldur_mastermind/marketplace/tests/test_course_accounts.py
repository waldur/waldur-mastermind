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
class CourseAccountPermissionTest(test.APITransactionTestCase):
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
            customer=self.fixture.project.customer, kind=ProjectKind.COURSE
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


@override_waldur_core_settings(
    COURSE_ACCOUNT_USE_API=True,  # Note: typo exists in original code
    COURSE_ACCOUNT_URL=COURSE_ACCOUNT_URL,
    COURSE_ACCOUNT_TOKEN_URL=COURSE_ACCOUNT_TOKEN_URL,
    COURSE_ACCOUNT_TOKEN_CLIENT_ID="test-client-id",
    COURSE_ACCOUNT_TOKEN_SECRET="test-client-secret",
)
class CourseAccountHandlerTest(test.APITransactionTestCase):
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
