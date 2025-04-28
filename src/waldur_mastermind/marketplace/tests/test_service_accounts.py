from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

import httpx
import respx
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures

WEBHOOK_URL = "http://example.com/api/"
WEBHOOK_TOKEN_CLIENT_ID = "test-client-id"
WEBHOOK_TOKEN_SECRET = "test-client-secret"


@override_waldur_core_settings(
    SERVICE_ACCOUNT_USE_WEBHOOKS=True,
    SERVICE_ACCOUNT_WEBHOOK_TOKEN_URL=WEBHOOK_URL,
    SERVICE_ACCOUNT_WEBHOOK_TOKEN_CLIENT_ID=WEBHOOK_TOKEN_CLIENT_ID,
    SERVICE_ACCOUNT_WEBHOOK_TOKEN_SECRET=WEBHOOK_TOKEN_SECRET,
)
@ddt
class ServiceAccountPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # Add MANAGE_SERVICE_ACCOUNT permission to relevant roles
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_SERVICE_ACCOUNT)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.MANAGE_SERVICE_ACCOUNT
        )
        ProjectRole.MANAGER.add_permission(PermissionEnum.MANAGE_SERVICE_ACCOUNT)
        ProjectRole.ADMIN.add_permission(PermissionEnum.MANAGE_SERVICE_ACCOUNT)

        # Setup users with appropriate roles
        # Add service_manager to project and customer
        self.fixture.project.customer.add_user(
            self.fixture.service_manager, ServiceProviderRole.MANAGER
        )
        self.fixture.project.add_user(self.fixture.service_manager, ProjectRole.MANAGER)

        # Add service_owner to project and customer
        self.fixture.project.customer.add_user(
            self.fixture.service_owner, CustomerRole.OWNER
        )
        self.fixture.project.add_user(self.fixture.service_owner, ProjectRole.ADMIN)

        # Add service_manager to offering_customer
        self.fixture.offering_customer.add_user(
            self.fixture.service_manager, CustomerRole.OWNER
        )

        # Setup mock responses
        token = "test-token"
        respx.start()
        respx.post(
            WEBHOOK_URL,
            content=f"grant_type=client_credentials&client_id={WEBHOOK_TOKEN_CLIENT_ID}&client_secret={WEBHOOK_TOKEN_SECRET}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ).mock(return_value=httpx.Response(200, json={"access_token": token}))

        respx.post(
            WEBHOOK_URL,
            headers={"Authorization": f"Bearer {token}"},
        ).mock(return_value=httpx.Response(200, json={"token": token}))

    def tearDown(self):
        super().tearDown()
        respx.stop()

    @data("staff", "service_manager", "service_owner", "manager", "admin")
    def test_user_can_create_project_service_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectServiceAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "project": self.fixture.project.uuid,
                "username": "project-user",
                "description": "project test",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("token", response.data)

    @data("staff", "service_manager", "service_owner")
    def test_authorized_user_can_get_customer_service_account(self, user):
        """Test that authorized user can get service account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.CustomerServiceAccountFactory(
            customer=self.fixture.offering_customer,
        )
        url = factories.CustomerServiceAccountFactory.get_url(account)
        response = self.client.get(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            response.data,
        )

    @data("manager", "admin")
    def test_project_level_users_cannot_get_customer_service_account(self, user):
        """Test that project-level users cannot get customer service accounts"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.CustomerServiceAccountFactory(
            customer=self.fixture.offering_customer,
        )
        url = factories.CustomerServiceAccountFactory.get_url(account)
        response = self.client.get(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            f"Expected status code 404, got: {response.status_code}. Response data: {response.data}",
        )

    @data("staff", "service_manager", "service_owner")
    def test_user_can_create_customer_service_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerServiceAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "customer": self.fixture.project.customer.uuid,
                "username": "customer-user",
                "description": "customer test",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("token", response.data)

    @data("manager", "admin")
    def test_project_level_users_cannot_create_customer_service_account(self, user):
        """Test that project-level users cannot create customer service accounts"""
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerServiceAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "customer": self.fixture.project.customer.uuid,
                "username": "customer-user",
                "description": "customer test",
            },
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
            f"Expected status code 403, got: {response.status_code}. Response data: {response.data}",
        )

    @data("user", "customer_support", "member")
    def test_unauthorized_user_can_not_create_project_service_account(self, user):
        """Test that unauthorized user can't create project service account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectServiceAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "project": self.fixture.project.uuid,
                "username": "test-account",
                "description": "Test account",
            },
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
            f"Expected status code 403, got: {response.status_code}. Response data: {response.data}",
        )

    @data("user", "customer_support", "member")
    def test_unauthorized_user_can_not_create_customer_service_account(self, user):
        """Test that unauthorized user can't create customer service account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerServiceAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "customer": self.fixture.project.customer.uuid,
                "username": "test-account",
                "description": "Test account",
            },
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
            f"Expected status code 403, got: {response.status_code}. Response data: {response.data}",
        )

    @data("staff", "service_manager", "service_owner", "manager", "admin")
    def test_authorized_user_can_get_project_service_account(self, user):
        """Test that authorized user can get service account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.ProjectServiceAccountFactory(
            project=self.fixture.project,
        )
        url = factories.ProjectServiceAccountFactory.get_url(account)
        response = self.client.get(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            response.data,
        )

    @data("staff", "service_manager", "service_owner", "manager", "admin")
    def test_authorized_user_can_update_project_service_account(self, user):
        """Test that authorized user can update project service account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.ProjectServiceAccountFactory(
            project=self.fixture.project,
        )
        url = factories.ProjectServiceAccountFactory.get_url(account)
        response = self.client.patch(url, {"username": "foo"})
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            response.data,
        )

        account.refresh_from_db()
        self.assertEqual(account.username, "foo")

    @data("user", "customer_support", "member")
    def test_unauthorized_user_can_not_update_project_service_account(self, user):
        """Test that unauthorized user can't update project service account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.ProjectServiceAccountFactory(project=self.fixture.project)
        url = factories.ProjectServiceAccountFactory.get_url(account)

        response = self.client.patch(
            url, {"username": "foo", "project": self.fixture.project.uuid}
        )
        # We check for 404 because queryset filters out the users without MANAGE_SERVICE_ACCOUNT permission
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            f"Expected status code 404, got: {response.status_code}. Response data: {response.data}",
        )

    @data("manager", "admin")
    def test_project_manager_with_permission_can_manage_service_account(self, user):
        """Test that project manager with correct permission can create and delete service accounts"""
        self.client.force_authenticate(getattr(self.fixture, user))
        self.account_username = "project-user"
        url = factories.ProjectServiceAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "project": self.fixture.project.uuid,
                "username": self.account_username,
                "description": "project test",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("token", response.data)
        # Test deletion
        account = models.ProjectServiceAccount.objects.get(
            username=self.account_username
        )
        url = factories.ProjectServiceAccountFactory.get_url(account)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(
                username=self.account_username
            ).exists()
        )

    @data("manager", "admin")
    def test_project_level_permissions_do_not_grant_customer_access(self, user):
        """Test that having project-level permissions doesn't grant access to customer service accounts"""
        self.client.force_authenticate(getattr(self.fixture, user))

        # Create a customer service account with offering_customer different from project
        account = factories.CustomerServiceAccountFactory(
            customer=self.fixture.offering_customer,
        )
        url = factories.CustomerServiceAccountFactory.get_url(account)

        # Try to get the customer service account
        response = self.client.get(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            f"Expected status code 404, got: {response.status_code}. Response data: {response.data}",
        )

    @data("staff", "service_manager", "service_owner", "manager", "admin")
    def test_authorized_user_can_delete_project_service_account(self, user):
        """Test that authorized user can delete project service account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.ProjectServiceAccountFactory(
            project=self.fixture.project,
        )
        url = factories.ProjectServiceAccountFactory.get_url(account)
        response = self.client.delete(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_204_NO_CONTENT,
            response.data,
        )
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(pk=account.pk).exists()
        )

    @data("user", "customer_support", "member")
    def test_unauthorized_user_can_not_delete_project_service_account(self, user):
        """Test that unauthorized user can't delete project service account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.ProjectServiceAccountFactory(project=self.fixture.project)
        url = factories.ProjectServiceAccountFactory.get_url(account)

        response = self.client.delete(url)
        # We check for 404 because queryset filters out the users without MANAGE_SERVICE_ACCOUNT permission
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            f"Expected status code 404, got: {response.status_code}. Response data: {response.data}",
        )

    @data("staff", "service_manager", "service_owner")
    def test_authorized_user_can_delete_customer_service_account(self, user):
        """Test that authorized user can delete customer service account"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.CustomerServiceAccountFactory(
            customer=self.fixture.offering_customer,
        )
        url = factories.CustomerServiceAccountFactory.get_url(account)
        response = self.client.delete(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_204_NO_CONTENT,
            response.data,
        )
        self.assertFalse(
            models.CustomerServiceAccount.objects.filter(pk=account.pk).exists()
        )

    @data("manager", "admin")
    def test_project_level_users_cannot_delete_customer_service_account(self, user):
        """Test that project-level users cannot delete customer service accounts"""
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.CustomerServiceAccountFactory(
            customer=self.fixture.offering_customer,
        )
        url = factories.CustomerServiceAccountFactory.get_url(account)
        response = self.client.delete(url)
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            f"Expected status code 404, got: {response.status_code}. Response data: {response.data}",
        )


@override_waldur_core_settings(
    SERVICE_ACCOUNT_USE_WEBHOOKS=True,
    SERVICE_ACCOUNT_WEBHOOK_TOKEN_URL="http://example.com/api/",
    SERVICE_ACCOUNT_WEBHOOK_TOKEN_CLIENT_ID="test-client-id",
    SERVICE_ACCOUNT_WEBHOOK_TOKEN_SECRET="test-client-secret",
)
class ScopedServiceAccountAPITest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.client.force_authenticate(self.fixture.staff)
        self.url = factories.ProjectServiceAccountFactory.get_list_url()

    @mock.patch("waldur_mastermind.marketplace.utils.post_service_account_to_url")
    def test_create_service_account_success(self, mock_post):
        """Test that service account creation succeeds"""
        # Mock successful API response
        mock_post.return_value.json.return_value = {"token": "test-token"}
        mock_post.return_value.status_code = 200

        response = self.client.post(
            self.url,
            {
                "project": self.fixture.project.uuid,
                "username": "test-account",
                "description": "Test account",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["token"], "test-token")
        mock_post.assert_called_once()

    @mock.patch("waldur_mastermind.marketplace.utils.post_service_account_to_url")
    def test_create_service_account_failure(self, mock_post):
        """Test that service account creation fails when API call fails"""
        # Mock failed API response
        mock_post.side_effect = httpx.HTTPError("API Error")

        response = self.client.post(
            self.url,
            {
                "project": self.fixture.project.uuid,
                "username": "test-account",
                "description": "Test account",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        account = models.ProjectServiceAccount.objects.get(username="test-account")
        self.assertEqual(account.error_message, "API Error")
