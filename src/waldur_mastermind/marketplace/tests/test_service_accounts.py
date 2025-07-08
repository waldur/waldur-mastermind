import unittest

import httpx
import respx
from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core.pagination import RESULT_COUNT_HEADER
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures

TOKEN_URL = "http://example.com/api/token"
SERVICE_ACCOUNT_URL = "http://example.com/api/service-accounts"
TOKEN_CLIENT_ID = "test-client-id"
TOKEN_SECRET = "test-client-secret"


class BaseServiceAccountTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

        # Setup respx
        respx.start()
        self.token = "test-token"

        self.account_username = "waldur"

        self.test_identifier = "test-identifier"

        service_account_response = {
            "serviceAccount": {
                "status": "active",
                "username": self.account_username,
                "email": "test@example.com",
                "description": "test description",
                "unixUid": 1000,
                "unixGid": 1000,
                "scopeType": "scope",
                "scopeName": "Test scope",
                "scopeSlug": "test-scope",
                "owner": {
                    "username": "test-owner",
                    "email": "owner@example.com",
                },
            },
            "apiKey": {
                "apiKey": self.token,
                "createdAt": "2025-04-28T12:00:00Z",
                "expiresAt": "2025-05-28T12:00:00Z",
                "ttl": 2592000,
            },
        }

        # Mock token request
        respx.post(
            TOKEN_URL,
            content=f"grant_type=client_credentials&client_id={TOKEN_CLIENT_ID}&client_secret={TOKEN_SECRET}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ).mock(return_value=httpx.Response(200, json={"access_token": self.token}))

        # Mock service account creation request
        respx.post(
            SERVICE_ACCOUNT_URL,
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(return_value=httpx.Response(200, json=service_account_response))

        # Mock service account deletion request
        respx.put(
            f"{SERVICE_ACCOUNT_URL}/{self.account_username}/close",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(return_value=httpx.Response(200, json={}))

        respx.get(
            f"{SERVICE_ACCOUNT_URL}/{self.account_username}",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(return_value=httpx.Response(200, json=service_account_response))

    def tearDown(self):
        respx.stop()
        super().tearDown()


@override_waldur_core_settings(
    SERVICE_ACCOUNT_USE_API=True,
    SERVICE_ACCOUNT_TOKEN_URL=TOKEN_URL,
    SERVICE_ACCOUNT_URL=SERVICE_ACCOUNT_URL,
    SERVICE_ACCOUNT_TOKEN_CLIENT_ID=TOKEN_CLIENT_ID,
    SERVICE_ACCOUNT_TOKEN_SECRET=TOKEN_SECRET,
)
@ddt
class ServiceAccountPermissionTest(BaseServiceAccountTest):
    def setUp(self):
        super().setUp()
        # Add MANAGE_SERVICE_ACCOUNT permission to relevant roles
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_SERVICE_ACCOUNT)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.MANAGE_SERVICE_ACCOUNT
        )
        ProjectRole.MANAGER.add_permission(PermissionEnum.MANAGE_SERVICE_ACCOUNT)
        ProjectRole.ADMIN.add_permission(PermissionEnum.MANAGE_SERVICE_ACCOUNT)

        # Setup users with appropriate roles
        self.fixture.project.customer.add_user(
            self.fixture.service_manager, ServiceProviderRole.MANAGER
        )
        self.fixture.project.add_user(self.fixture.service_manager, ProjectRole.MANAGER)
        self.fixture.project.customer.add_user(
            self.fixture.service_owner, CustomerRole.OWNER
        )
        self.fixture.project.add_user(self.fixture.service_owner, ProjectRole.ADMIN)
        self.fixture.offering_customer.add_user(
            self.fixture.service_manager, CustomerRole.OWNER
        )

    @data("staff", "service_manager", "service_owner", "manager", "admin")
    def test_user_can_create_project_service_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectServiceAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "project": self.fixture.project.uuid,
                "description": "project test",
                "preferred_identifier": self.test_identifier,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("token", response.data)
        account = models.ProjectServiceAccount.objects.get(
            username=self.account_username
        )
        self.assertEqual(account.preferred_identifier, self.test_identifier)

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
                "description": "customer test",
                "preferred_identifier": self.test_identifier,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("token", response.data)
        account = models.CustomerServiceAccount.objects.get(
            username=self.account_username
        )
        self.assertEqual(account.preferred_identifier, self.test_identifier)

    @data("manager", "admin")
    def test_project_level_users_cannot_create_customer_service_account(self, user):
        """Test that project-level users cannot create customer service accounts"""
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerServiceAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "customer": self.fixture.project.customer.uuid,
                "description": "customer test",
                "preferred_identifier": self.test_identifier,
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
                "description": "Test account",
                "preferred_identifier": self.test_identifier,
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
                "description": "Test account",
                "preferred_identifier": self.test_identifier,
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
        respx.put(
            f"{SERVICE_ACCOUNT_URL}/{self.account_username}",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "serviceAccount": {
                        "email": "new email",
                        "description": "new description",
                    }
                },
            )
        )
        url = factories.ProjectServiceAccountFactory.get_url(account)
        response = self.client.patch(url, {"description": "foo"})
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            response.data,
        )

        account.refresh_from_db()
        self.assertEqual(account.description, "foo")

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
        url = factories.ProjectServiceAccountFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "project": self.fixture.project.uuid,
                "description": "project test",
                "preferred_identifier": self.test_identifier,
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

    @data("staff", "service_manager", "service_owner", "manager", "admin")
    def test_can_create_project_service_account_under_limit(self, user):
        """Test that service account can be created when under project limit"""
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectServiceAccountFactory.get_list_url()

        response = self.client.post(
            url,
            {
                "project": self.fixture.project.uuid,
                "description": "project test 1",
                "preferred_identifier": f"{self.test_identifier}-1",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        response = self.client.post(
            url,
            {
                "project": self.fixture.project.uuid,
                "description": "project test 2",
                "preferred_identifier": f"{self.test_identifier}-2",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("staff", "service_manager", "service_owner", "manager", "admin")
    def test_cannot_create_project_service_account_over_limit(self, user):
        """Test that service account cannot be created when project limit exceeded"""
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectServiceAccountFactory.get_list_url()

        # Create first service account
        response = self.client.post(
            url,
            {
                "project": self.fixture.project.uuid,
                "description": "project test 1",
                "preferred_identifier": f"{self.test_identifier}-1",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # Create second service account
        response = self.client.post(
            url,
            {
                "project": self.fixture.project.uuid,
                "description": "project test 2",
                "preferred_identifier": f"{self.test_identifier}-2",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # Try to create third service account
        response = self.client.post(
            url,
            {
                "project": self.fixture.project.uuid,
                "description": "project test 3",
                "preferred_identifier": f"{self.test_identifier}-3",
            },
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("Maximum number of service accounts", response.data["detail"])

    @data("staff", "service_manager", "service_owner")
    def test_can_create_customer_service_account_under_limit(self, user):
        """Test that service account can be created when under customer limit"""
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerServiceAccountFactory.get_list_url()

        # Create first service account
        response = self.client.post(
            url,
            {
                "customer": self.fixture.offering_customer.uuid,
                "description": "customer test 1",
                "preferred_identifier": f"{self.test_identifier}-1",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # Create second service account
        response = self.client.post(
            url,
            {
                "customer": self.fixture.offering_customer.uuid,
                "description": "customer test 2",
                "preferred_identifier": f"{self.test_identifier}-2",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("staff", "service_manager", "service_owner")
    def test_cannot_create_customer_service_account_over_limit(self, user):
        """Test that service account cannot be created when customer limit exceeded"""
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerServiceAccountFactory.get_list_url()

        # Create first service account
        response = self.client.post(
            url,
            {
                "customer": self.fixture.offering_customer.uuid,
                "description": "customer test 1",
                "preferred_identifier": f"{self.test_identifier}-1",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # Create second service account
        response = self.client.post(
            url,
            {
                "customer": self.fixture.offering_customer.uuid,
                "description": "customer test 2",
                "preferred_identifier": f"{self.test_identifier}-2",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # Try to create third service account
        response = self.client.post(
            url,
            {
                "customer": self.fixture.offering_customer.uuid,
                "description": "customer test 3",
                "preferred_identifier": f"{self.test_identifier}-3",
            },
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("Maximum number of service accounts", response.data["detail"])

    def test_list_project_service_accounts_pagination(self):
        # Create more project service accounts than the default page size (assume 10)
        project = self.fixture.project
        for i in range(15):
            factories.ProjectServiceAccountFactory(project=project)
        self.client.force_authenticate(self.fixture.provider_owner)
        url = factories.OfferingFactory.get_url(
            self.fixture.offering, action="list_project_service_accounts"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIsInstance(data, list)
        self.assertLessEqual(len(data), 10)  # default page size is 10
        headers = dict(response.headers)
        self.assertIn(RESULT_COUNT_HEADER, headers)
        self.assertGreaterEqual(int(headers[RESULT_COUNT_HEADER]), 15)
        self.assertIn("Link", headers)

    @unittest.skip("SPs cannot see service accounts yet")
    def test_list_customer_service_accounts_pagination(self):
        # Create more customer service accounts than the default page size (assume 10)
        customer = self.fixture.offering_customer
        for i in range(15):
            factories.CustomerServiceAccountFactory(customer=customer)
        self.client.force_authenticate(self.fixture.provider_owner)
        url = factories.OfferingFactory.get_url(
            self.fixture.offering, action="list_customer_service_accounts"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIsInstance(data, list)
        self.assertLessEqual(len(data), 10)  # default page size is 10
        headers = dict(response.headers)
        self.assertIn(RESULT_COUNT_HEADER, headers)
        self.assertGreaterEqual(int(headers[RESULT_COUNT_HEADER]), 15)
        self.assertIn("Link", headers)


@override_waldur_core_settings(
    SERVICE_ACCOUNT_USE_API=True,
    SERVICE_ACCOUNT_TOKEN_URL=TOKEN_URL,
    SERVICE_ACCOUNT_URL=SERVICE_ACCOUNT_URL,
    SERVICE_ACCOUNT_TOKEN_CLIENT_ID=TOKEN_CLIENT_ID,
    SERVICE_ACCOUNT_TOKEN_SECRET=TOKEN_SECRET,
)
class ScopedServiceAccountAPITest(BaseServiceAccountTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.fixture.staff)

        self.url = factories.ProjectServiceAccountFactory.get_list_url()

        self.payload = {
            "project": self.fixture.project.uuid,
            "description": "Test account",
            "preferred_identifier": self.test_identifier,
        }
        self.new_api_key = "new-rotated-key-123"
        self.new_expires_at = "2025-05-28T12:00:00Z"

    def test_create_service_account_success(self):
        """Test that service account creation succeeds"""
        response = self.client.post(
            self.url,
            self.payload,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["token"], self.token)
        self.assertTrue(
            models.ProjectServiceAccount.objects.filter(
                preferred_identifier=self.test_identifier
            ).exists()
        )

    def test_create_service_account_failure(self):
        """Test that service account creation fails when API call fails"""
        # Mock token request (failure)
        respx.post(
            TOKEN_URL,
            content=f"grant_type=client_credentials&client_id={TOKEN_CLIENT_ID}&client_secret={TOKEN_SECRET}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ).mock(return_value=httpx.Response(400))

        response = self.client.post(
            self.url,
            self.payload,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.ProjectServiceAccount.objects.filter(
                preferred_identifier=self.test_identifier
            ).exists()
        )

    def test_rotate_project_service_account_api_key(self):
        """Test that service account API key rotation succeeds"""
        account = factories.ProjectServiceAccountFactory(project=self.fixture.project)
        url = factories.ProjectServiceAccountFactory.get_url(account)

        # Mock API key rotation response
        respx.put(
            f"{SERVICE_ACCOUNT_URL}/{account.username}/rotate-api-key",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "apiKey": {
                        "apiKey": self.new_api_key,
                        "expiresAt": self.new_expires_at,
                    }
                },
            )
        )
        response = self.client.post(f"{url}rotate_api_key/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["token"], self.new_api_key)
        self.assertEqual(response.data["expires_at"], self.new_expires_at)

    def test_rotate_customer_service_account_api_key(self):
        """Test that service account API key rotation succeeds"""
        account = factories.CustomerServiceAccountFactory(
            customer=self.fixture.offering_customer
        )
        url = factories.CustomerServiceAccountFactory.get_url(account)

        # Mock API key rotation response
        respx.put(
            f"{SERVICE_ACCOUNT_URL}/{account.username}/rotate-api-key",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "apiKey": {
                        "apiKey": self.new_api_key,
                        "expiresAt": self.new_expires_at,
                    }
                },
            )
        )
        response = self.client.post(f"{url}rotate_api_key/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["token"], self.new_api_key)
        self.assertEqual(response.data["expires_at"], self.new_expires_at)

    def test_update_service_account_details(self):
        """Test that service account API key rotation succeeds"""
        account = factories.CustomerServiceAccountFactory(
            customer=self.fixture.offering_customer
        )
        url = factories.CustomerServiceAccountFactory.get_url(account)
        new_email = "new-user@example.com"
        new_description = "New description"

        # Mock API account details response
        respx.put(
            f"{SERVICE_ACCOUNT_URL}/{self.account_username}",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "serviceAccount": {
                        "email": new_email,
                        "description": new_description,
                    }
                },
            )
        )

        response = self.client.patch(
            f"{url}",
            {
                "email": new_email,
                "description": new_description,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["email"], new_email)
        self.assertEqual(response.data["description"], new_description)


class ServiceAccountOfferingTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.provider_owner = self.fixture.provider_owner
        self.resource = self.fixture.resource
        self.resource.state = models.Resource.States.OK
        self.resource.save()

        self.project_service_accounts = (
            factories.ProjectServiceAccountFactory.create_batch(
                3, project=self.resource.project
            )
        )
        self.customer_service_accounts = (
            factories.CustomerServiceAccountFactory.create_batch(
                2, customer=self.resource.customer
            )
        )

    def test_service_provider_owner_can_list_project_service_accounts(self):
        """Test that service provider owner can list project service accounts"""
        self.client.force_authenticate(self.provider_owner)
        url = factories.OfferingFactory.get_url(
            self.offering, action="list_project_service_accounts"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(3, len(response.data), response.data)
        self.assertEqual(
            set([account["username"] for account in response.data]),
            set([account.username for account in self.project_service_accounts]),
        )

    def test_service_provider_owner_can_list_customer_service_accounts(self):
        """Test that service provider owner can list customer service accounts"""
        self.client.force_authenticate(self.provider_owner)
        url = factories.OfferingFactory.get_url(
            self.offering, action="list_customer_service_accounts"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(2, len(response.data), response.data)
        self.assertEqual(
            set([account["username"] for account in response.data]),
            set([account.username for account in self.customer_service_accounts]),
        )
