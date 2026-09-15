from unittest import mock

import pytest
import responses
from constance import config
from constance.test.unittest import override_config
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support.backend import SupportBackendType
from waldur_mastermind.support.backend.atlassian import ClientCredentialsAuth
from waldur_mastermind.support.backend.atlassian_discovery import (
    AtlassianDiscoveryService,
    TemporaryCredentials,
)

GATEWAY_URL = "https://api.atlassian.com/ex/jira/cloud-1"


class TestAtlassianDiscoveryService:
    """Unit tests for AtlassianDiscoveryService."""

    def test_create_client_with_api_token(self):
        creds = TemporaryCredentials(
            api_url="https://test.atlassian.net",
            auth_method="api_token",
            email="test@example.com",
            token="test-token",
        )
        service = AtlassianDiscoveryService(creds)

        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.ServiceDesk"
        ) as mock_sd:
            _ = service.client
            mock_sd.assert_called_once()
            call_kwargs = mock_sd.call_args[1]
            assert call_kwargs["username"] == "test@example.com"
            assert call_kwargs["password"] == "test-token"
            assert call_kwargs["cloud"] is True

    def test_create_client_with_personal_access_token(self):
        creds = TemporaryCredentials(
            api_url="https://jira.example.com",
            auth_method="personal_access_token",
            personal_access_token="pat-12345",
        )
        service = AtlassianDiscoveryService(creds)

        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.ServiceDesk"
        ) as mock_sd:
            _ = service.client
            mock_sd.assert_called_once()
            call_kwargs = mock_sd.call_args[1]
            assert call_kwargs["token"] == "pat-12345"

    def test_create_client_with_basic_auth(self):
        creds = TemporaryCredentials(
            api_url="https://jira.example.com",
            auth_method="basic",
            username="admin",
            password="secret",
        )
        service = AtlassianDiscoveryService(creds)

        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.ServiceDesk"
        ) as mock_sd:
            _ = service.client
            mock_sd.assert_called_once()
            call_kwargs = mock_sd.call_args[1]
            assert call_kwargs["username"] == "admin"
            assert call_kwargs["password"] == "secret"

    @responses.activate
    def test_oauth2_client_uses_gateway_and_client_credentials(self):
        responses.get(
            "https://test.atlassian.net/_edge/tenant_info", json={"cloudId": "cloud-1"}
        )
        creds = TemporaryCredentials(
            api_url="https://test.atlassian.net",
            auth_method="oauth2_client_credentials",
            client_id="client-id",
            client_secret="client-secret",
        )
        service = AtlassianDiscoveryService(creds)

        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.ServiceDesk"
        ) as mock_sd:
            _ = service.client

        assert service.api_url == GATEWAY_URL
        assert mock_sd.call_args[1]["url"] == GATEWAY_URL + "/"
        auth = mock_sd.return_value._session.auth
        assert isinstance(auth, ClientCredentialsAuth)
        assert (auth.client_id, auth.client_secret) == ("client-id", "client-secret")

    def test_oauth2_gateway_url_is_used_as_given(self):
        creds = TemporaryCredentials(
            api_url=GATEWAY_URL + "/",
            auth_method="oauth2_client_credentials",
            client_id="client-id",
            client_secret="client-secret",
        )

        assert AtlassianDiscoveryService(creds).api_url == GATEWAY_URL

    def test_oauth2_is_refused_for_a_non_cloud_site(self):
        creds = TemporaryCredentials(
            api_url="https://jira.example.com",
            auth_method="oauth2_client_credentials",
            client_id="client-id",
            client_secret="client-secret",
        )

        result = AtlassianDiscoveryService(creds).validate_credentials()

        assert result["valid"] is False
        assert "Atlassian Cloud" in result["error"]

    def test_validate_credentials_success(self):
        creds = TemporaryCredentials(
            api_url="https://test.atlassian.net",
            auth_method="api_token",
            email="test@example.com",
            token="test-token",
        )

        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.ServiceDesk"
        ) as mock_sd_class:
            mock_client = mock.MagicMock()
            mock_client.get_info.return_value = {
                "version": "9.0.0",
                "deploymentType": "Cloud",
            }
            mock_sd_class.return_value = mock_client

            service = AtlassianDiscoveryService(creds)
            result = service.validate_credentials()

        assert result["valid"] is True
        assert "server_info" in result

    def test_validate_credentials_failure(self):
        creds = TemporaryCredentials(
            api_url="https://test.atlassian.net",
            auth_method="api_token",
            email="test@example.com",
            token="invalid-token",
        )

        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.ServiceDesk"
        ) as mock_sd_class:
            from atlassian.errors import ApiError

            mock_client = mock.MagicMock()
            mock_client.get_info.side_effect = ApiError("Authentication failed")
            mock_sd_class.return_value = mock_client

            service = AtlassianDiscoveryService(creds)
            result = service.validate_credentials()

        assert result["valid"] is False
        assert "error" in result

    def test_discover_projects_success(self):
        creds = TemporaryCredentials(
            api_url="https://test.atlassian.net",
            auth_method="api_token",
            email="test@example.com",
            token="test-token",
        )

        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.ServiceDesk"
        ) as mock_sd_class:
            mock_client = mock.MagicMock()
            mock_client.get_service_desks.return_value = {
                "values": [
                    {
                        "id": "1",
                        "projectKey": "SD",
                        "projectName": "Service Desk",
                        "description": "Main service desk",
                    },
                    {
                        "id": "2",
                        "projectKey": "IT",
                        "projectName": "IT Support",
                        "description": "",
                    },
                ]
            }
            mock_sd_class.return_value = mock_client

            service = AtlassianDiscoveryService(creds)
            projects = service.discover_projects()

        assert len(projects) == 2
        assert projects[0]["id"] == "1"
        assert projects[0]["key"] == "SD"
        assert projects[0]["name"] == "Service Desk"

    def test_discover_request_types_success(self):
        creds = TemporaryCredentials(
            api_url="https://test.atlassian.net",
            auth_method="api_token",
            email="test@example.com",
            token="test-token",
        )

        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.ServiceDesk"
        ) as mock_sd_class:
            mock_client = mock.MagicMock()
            mock_client.get_request_types.return_value = {
                "values": [
                    {
                        "id": "10",
                        "name": "Service Request",
                        "description": "General request",
                        "issueTypeId": "100",
                    },
                    {
                        "id": "11",
                        "name": "Incident",
                        "description": "Report an issue",
                        "issueTypeId": "101",
                    },
                ]
            }
            mock_sd_class.return_value = mock_client

            service = AtlassianDiscoveryService(creds)
            request_types = service.discover_request_types("1")

        assert len(request_types) == 2
        assert request_types[0]["id"] == "10"
        assert request_types[0]["name"] == "Service Request"
        mock_client.get_request_types.assert_called_once_with("1")

    def test_discover_priorities_success(self):
        creds = TemporaryCredentials(
            api_url="https://test.atlassian.net",
            auth_method="api_token",
            email="test@example.com",
            token="test-token",
        )

        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.ServiceDesk"
        ) as mock_sd_class:
            mock_client = mock.MagicMock()
            mock_client.get.return_value = [
                {"id": "1", "name": "High", "description": "High priority"},
                {"id": "2", "name": "Medium", "description": "Medium priority"},
                {"id": "3", "name": "Low", "description": "Low priority"},
            ]
            mock_sd_class.return_value = mock_client

            service = AtlassianDiscoveryService(creds)
            priorities = service.discover_priorities()

        assert len(priorities) == 3
        assert priorities[0]["name"] == "High"
        mock_client.get.assert_called_once_with("rest/api/2/priority/")


@pytest.mark.override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.ATLASSIAN,
)
class TestAtlassianSettingsDiscoveryViewSet(test.APITestCase):
    """API tests for AtlassianSettingsDiscoveryViewSet."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.regular_user = structure_factories.UserFactory(is_staff=False)

    def get_validate_credentials_url(self):
        return "/api/support/settings/atlassian/validate_credentials/"

    def get_discover_projects_url(self):
        return "/api/support/settings/atlassian/discover_projects/"

    def get_discover_request_types_url(self):
        return "/api/support/settings/atlassian/discover_request_types/"

    def get_discover_priorities_url(self):
        return "/api/support/settings/atlassian/discover_priorities/"

    def get_current_settings_url(self):
        return "/api/support/settings/atlassian/current_settings/"

    def get_preview_settings_url(self):
        return "/api/support/settings/atlassian/preview_settings/"

    def get_save_settings_url(self):
        return "/api/support/settings/atlassian/save_settings/"

    def test_validate_credentials_requires_staff(self):
        self.client.force_authenticate(self.regular_user)
        response = self.client.post(
            self.get_validate_credentials_url(),
            {
                "api_url": "https://test.atlassian.net",
                "auth_method": "api_token",
                "email": "test@example.com",
                "token": "test-token",
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_validate_credentials_allows_staff(self):
        self.client.force_authenticate(self.staff_user)
        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.AtlassianDiscoveryService.validate_credentials"
        ) as mock_validate:
            mock_validate.return_value = {"valid": True, "message": "OK"}
            response = self.client.post(
                self.get_validate_credentials_url(),
                {
                    "api_url": "https://test.atlassian.net",
                    "auth_method": "api_token",
                    "email": "test@example.com",
                    "token": "test-token",
                },
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["valid"] is True

    def test_validate_credentials_requires_email_for_api_token(self):
        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.get_validate_credentials_url(),
            {
                "api_url": "https://test.atlassian.net",
                "auth_method": "api_token",
                "token": "test-token",  # Missing email
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "email" in response.data

    def test_validate_credentials_requires_pat_for_pat_method(self):
        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.get_validate_credentials_url(),
            {
                "api_url": "https://jira.example.com",
                "auth_method": "personal_access_token",
                # Missing personal_access_token
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "personal_access_token" in response.data

    def test_discover_projects_success(self):
        self.client.force_authenticate(self.staff_user)
        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.AtlassianDiscoveryService.discover_projects"
        ) as mock_discover:
            mock_discover.return_value = [
                {"id": "1", "key": "SD", "name": "Service Desk", "description": ""}
            ]
            response = self.client.post(
                self.get_discover_projects_url(),
                {
                    "api_url": "https://test.atlassian.net",
                    "auth_method": "api_token",
                    "email": "test@example.com",
                    "token": "test-token",
                },
            )
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]["key"] == "SD"

    def test_discover_request_types_requires_project_id(self):
        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.get_discover_request_types_url(),
            {
                "api_url": "https://test.atlassian.net",
                "auth_method": "api_token",
                "email": "test@example.com",
                "token": "test-token",
                # Missing project_id
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "project_id" in response.data

    def test_discover_request_types_success(self):
        self.client.force_authenticate(self.staff_user)
        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.AtlassianDiscoveryService.discover_request_types"
        ) as mock_discover:
            mock_discover.return_value = [
                {
                    "id": "10",
                    "name": "Service Request",
                    "description": "",
                    "issue_type_id": "100",
                }
            ]
            response = self.client.post(
                self.get_discover_request_types_url(),
                {
                    "api_url": "https://test.atlassian.net",
                    "auth_method": "api_token",
                    "email": "test@example.com",
                    "token": "test-token",
                    "project_id": "1",
                },
            )
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        mock_discover.assert_called_once_with("1")

    def test_current_settings_requires_staff(self):
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self.get_current_settings_url())
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_current_settings_returns_masked_data(self):
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(self.get_current_settings_url())
        assert response.status_code == status.HTTP_200_OK
        assert "ATLASSIAN_API_URL" in response.data
        assert "ATLASSIAN_PROJECT_ID" in response.data
        # Secrets should not be in the response
        assert "ATLASSIAN_TOKEN" not in response.data
        assert "ATLASSIAN_PASSWORD" not in response.data

    def test_preview_settings_validates_credentials(self):
        self.client.force_authenticate(self.staff_user)
        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.AtlassianDiscoveryService.validate_credentials"
        ) as mock_validate:
            mock_validate.return_value = {"valid": False, "error": "Auth failed"}
            response = self.client.post(
                self.get_preview_settings_url(),
                {
                    "api_url": "https://test.atlassian.net",
                    "auth_method": "api_token",
                    "email": "test@example.com",
                    "token": "invalid-token",
                    "project_id": "1",
                },
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["valid"] is False

    def test_preview_settings_returns_preview(self):
        self.client.force_authenticate(self.staff_user)
        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.AtlassianDiscoveryService.validate_credentials"
        ) as mock_validate:
            mock_validate.return_value = {"valid": True}
            response = self.client.post(
                self.get_preview_settings_url(),
                {
                    "api_url": "https://test.atlassian.net",
                    "auth_method": "api_token",
                    "email": "test@example.com",
                    "token": "test-token",
                    "project_id": "SD",
                    "reporter_field": "Reporter",
                },
            )
        assert response.status_code == status.HTTP_200_OK
        assert "preview" in response.data
        preview = response.data["preview"]
        assert preview["ATLASSIAN_API_URL"] == "https://test.atlassian.net"
        assert preview["ATLASSIAN_PROJECT_ID"] == "SD"
        assert preview["ATLASSIAN_REPORTER_FIELD"] == "Reporter"
        # Token should be hidden
        assert preview["ATLASSIAN_TOKEN"] == "***HIDDEN***"

    def test_save_settings_requires_confirm(self):
        self.client.force_authenticate(self.staff_user)
        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.AtlassianDiscoveryService.validate_credentials"
        ) as mock_validate:
            mock_validate.return_value = {"valid": True}
            response = self.client.post(
                self.get_save_settings_url(),
                {
                    "api_url": "https://test.atlassian.net",
                    "auth_method": "api_token",
                    "email": "test@example.com",
                    "token": "test-token",
                    "project_id": "SD",
                    "confirm_save": False,  # Must be True
                },
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "confirm_save" in response.data

    def test_save_settings_success(self):
        self.client.force_authenticate(self.staff_user)
        with mock.patch(
            "waldur_mastermind.support.backend.atlassian_discovery.AtlassianDiscoveryService.validate_credentials"
        ) as mock_validate:
            mock_validate.return_value = {"valid": True}
            response = self.client.post(
                self.get_save_settings_url(),
                {
                    "api_url": "https://test.atlassian.net",
                    "auth_method": "api_token",
                    "email": "test@example.com",
                    "token": "test-token",
                    "project_id": "SD",
                    "confirm_save": True,
                },
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["saved"] is True

        # Verify settings were saved
        from constance import config

        assert config.ATLASSIAN_API_URL == "https://test.atlassian.net"
        assert config.ATLASSIAN_EMAIL == "test@example.com"
        assert config.ATLASSIAN_TOKEN == "test-token"
        assert config.ATLASSIAN_PROJECT_ID == "SD"

    OAUTH2_CREDENTIALS = {
        "api_url": "https://test.atlassian.net",
        "auth_method": "oauth2_client_credentials",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }

    def _post_with_valid_credentials(self, url, data):
        self.client.force_authenticate(self.staff_user)
        with (
            mock.patch(
                "waldur_mastermind.support.backend.atlassian_discovery.AtlassianDiscoveryService.validate_credentials",
                return_value={"valid": True},
            ),
            mock.patch(
                "waldur_mastermind.support.backend.atlassian_discovery.get_cloud_gateway_url",
                return_value=GATEWAY_URL,
            ),
        ):
            return self.client.post(url, data)

    def test_validate_credentials_requires_client_secret_for_oauth2(self):
        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.get_validate_credentials_url(),
            {**self.OAUTH2_CREDENTIALS, "client_secret": ""},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "client_secret" in response.data

    def test_preview_settings_shows_gateway_url_for_oauth2(self):
        response = self._post_with_valid_credentials(
            self.get_preview_settings_url(),
            {**self.OAUTH2_CREDENTIALS, "project_id": "10"},
        )
        assert response.status_code == status.HTTP_200_OK
        preview = response.data["preview"]
        assert preview["ATLASSIAN_API_URL"] == GATEWAY_URL
        assert preview["ATLASSIAN_OAUTH2_CLIENT_ID"] == "client-id"
        assert preview["ATLASSIAN_OAUTH2_CLIENT_SECRET"] == "***HIDDEN***"

    def test_save_oauth2_stores_gateway_url_and_clears_other_credentials(self):
        config.ATLASSIAN_EMAIL = "old@example.com"
        config.ATLASSIAN_TOKEN = "old-token"
        config.ATLASSIAN_OAUTH2_ACCESS_TOKEN = "old-access-token"

        response = self._post_with_valid_credentials(
            self.get_save_settings_url(),
            {**self.OAUTH2_CREDENTIALS, "project_id": "10", "confirm_save": True},
        )

        assert response.status_code == status.HTTP_200_OK
        assert config.ATLASSIAN_API_URL == GATEWAY_URL
        assert config.ATLASSIAN_OAUTH2_CLIENT_ID == "client-id"
        assert config.ATLASSIAN_OAUTH2_CLIENT_SECRET == "client-secret"
        assert config.ATLASSIAN_EMAIL == ""
        assert config.ATLASSIAN_TOKEN == ""
        assert config.ATLASSIAN_OAUTH2_ACCESS_TOKEN == ""

    def test_save_api_token_clears_oauth2_credentials(self):
        # OAuth 2.0 takes precedence in the backend, so leftovers would win.
        config.ATLASSIAN_OAUTH2_CLIENT_ID = "client-id"
        config.ATLASSIAN_OAUTH2_CLIENT_SECRET = "client-secret"

        response = self._post_with_valid_credentials(
            self.get_save_settings_url(),
            {
                "api_url": "https://test.atlassian.net",
                "auth_method": "api_token",
                "email": "test@example.com",
                "token": "test-token",
                "project_id": "SD",
                "confirm_save": True,
            },
        )

        assert response.status_code == status.HTTP_200_OK
        assert config.ATLASSIAN_OAUTH2_CLIENT_ID == ""
        assert config.ATLASSIAN_OAUTH2_CLIENT_SECRET == ""

    @override_config(
        ATLASSIAN_API_URL="https://example.com/",
        ATLASSIAN_USERNAME="USERNAME",
        ATLASSIAN_PASSWORD="PASSWORD",
        ATLASSIAN_TOKEN="",
        ATLASSIAN_PERSONAL_ACCESS_TOKEN="",
        ATLASSIAN_OAUTH2_CLIENT_ID="",
        ATLASSIAN_OAUTH2_CLIENT_SECRET="",
    )
    def test_current_settings_ignores_placeholder_defaults(self):
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(self.get_current_settings_url())
        assert response.data["ATLASSIAN_API_URL"] == ""
        assert response.data["ATLASSIAN_USERNAME"] == ""
        assert response.data["auth_method"] is None
        assert response.data["auth_configured"] is False

    @override_config(
        ATLASSIAN_OAUTH2_CLIENT_ID="client-id",
        ATLASSIAN_OAUTH2_CLIENT_SECRET="client-secret",
    )
    def test_current_settings_detects_oauth2_client_credentials(self):
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(self.get_current_settings_url())
        assert response.data["auth_method"] == "oauth2_client_credentials"
        assert response.data["ATLASSIAN_OAUTH2_CLIENT_ID"] == "client-id"
        assert "ATLASSIAN_OAUTH2_CLIENT_SECRET" not in response.data


@pytest.mark.django_db
class TestRequestTypeViewSet(test.APITestCase):
    """Tests for RequestTypeViewSet API endpoint."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.regular_user = structure_factories.UserFactory()

    def get_list_url(self):
        from django.urls import reverse

        return reverse("support-request-type-list")

    def get_detail_url(self, request_type):
        from django.urls import reverse

        return reverse(
            "support-request-type-detail", kwargs={"uuid": request_type.uuid.hex}
        )

    def test_list_returns_only_active_request_types(self):
        from waldur_mastermind.support.tests import factories

        # Create active and inactive request types
        active_type = factories.RequestTypeFactory(
            name="Active Type", is_active=True, order=1
        )
        factories.RequestTypeFactory(name="Inactive Type", is_active=False, order=2)

        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self.get_list_url())

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]["name"] == active_type.name

    def test_list_returns_request_types_ordered_by_order_field(self):
        from waldur_mastermind.support.tests import factories

        factories.RequestTypeFactory(name="Third", is_active=True, order=3)
        factories.RequestTypeFactory(name="First", is_active=True, order=1)
        factories.RequestTypeFactory(name="Second", is_active=True, order=2)

        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self.get_list_url())

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 3
        assert response.data[0]["name"] == "First"
        assert response.data[1]["name"] == "Second"
        assert response.data[2]["name"] == "Third"

    def test_detail_returns_request_type(self):
        from waldur_mastermind.support.tests import factories

        request_type = factories.RequestTypeFactory(name="Test Type", is_active=True)

        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self.get_detail_url(request_type))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["name"] == "Test Type"
        assert response.data["uuid"] == request_type.uuid.hex

    def test_serializer_url_field_resolves_correctly(self):
        """Verify that URL field in serializer resolves to correct view name."""
        from waldur_mastermind.support.tests import factories

        request_type = factories.RequestTypeFactory(name="URL Test", is_active=True)

        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self.get_list_url())

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        # URL should contain the request type UUID
        assert request_type.uuid.hex in response.data[0]["url"]
        assert "support-request-types" in response.data[0]["url"]

    def test_anonymous_user_cannot_access(self):
        response = self.client.get(self.get_list_url())
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
