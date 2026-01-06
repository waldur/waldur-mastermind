"""
Atlassian Settings Discovery Service

This module provides stateless discovery functionality for Atlassian settings.
It creates temporary clients using provided credentials without saving them.
"""

import logging
from dataclasses import dataclass

import requests
from atlassian import ServiceDesk
from atlassian.errors import ApiError, ApiNotFoundError, ApiPermissionError

from waldur_core.structure.exceptions import ServiceBackendError

logger = logging.getLogger(__name__)


class AtlassianDiscoveryError(ServiceBackendError):
    """Exception for discovery-related errors."""

    pass


@dataclass
class TemporaryCredentials:
    """Container for temporary Atlassian credentials."""

    api_url: str
    auth_method: str  # 'api_token', 'personal_access_token', 'basic'
    email: str | None = None
    token: str | None = None
    personal_access_token: str | None = None
    username: str | None = None
    password: str | None = None
    verify_ssl: bool = True


class AtlassianDiscoveryService:
    """
    Stateless service for discovering Atlassian configuration.

    All methods accept temporary credentials and do not persist anything
    until explicitly requested.
    """

    def __init__(self, credentials: TemporaryCredentials):
        self.credentials = credentials
        self._client = None

    @property
    def client(self) -> ServiceDesk:
        """Lazily create ServiceDesk client with temporary credentials."""
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def _create_client(self) -> ServiceDesk:
        """Create ServiceDesk client based on auth method."""
        url = self.credentials.api_url
        if not url.endswith("/"):
            url += "/"

        base_kwargs = {
            "url": url,
            "verify_ssl": self.credentials.verify_ssl,
        }

        if self.credentials.auth_method == "personal_access_token":
            logger.info(
                "Creating discovery client with Personal Access Token authentication"
            )
            return ServiceDesk(
                token=self.credentials.personal_access_token, **base_kwargs
            )
        elif self.credentials.auth_method == "api_token":
            logger.info("Creating discovery client with API Token authentication")
            return ServiceDesk(
                username=self.credentials.email,
                password=self.credentials.token,
                cloud=True,
                **base_kwargs,
            )
        else:  # basic
            logger.info("Creating discovery client with Basic Authentication")
            is_cloud = ".atlassian.net" in url.lower()
            return ServiceDesk(
                username=self.credentials.username,
                password=self.credentials.password,
                cloud=is_cloud,
                **base_kwargs,
            )

    def validate_credentials(self) -> dict:
        """
        Validate credentials by attempting to connect.

        Returns:
            dict with 'valid' boolean and 'message' or 'error'
        """
        try:
            info = self.client.get_info()
            return {
                "valid": True,
                "message": "Credentials validated successfully",
                "server_info": {
                    "version": info.get("version", "unknown"),
                    "deployment_type": info.get("deploymentType", "unknown"),
                },
            }
        except (
            ApiError,
            ApiPermissionError,
            ApiNotFoundError,
            requests.exceptions.RequestException,
        ) as e:
            logger.warning(f"Atlassian credential validation failed: {e}")
            return {
                "valid": False,
                "error": str(e),
            }

    def discover_projects(self) -> list[dict]:
        """
        Discover available Service Desk projects.

        Returns:
            List of project dictionaries with id, key, name, description
        """
        try:
            service_desks = self.client.get_service_desks()
            # Handle both list and paginated response formats
            if isinstance(service_desks, list):
                values = service_desks
            else:
                values = service_desks.get("values", [])
            projects = []
            for sd in values:
                projects.append(
                    {
                        "id": str(sd.get("id", "")),
                        "key": sd.get("projectKey", ""),
                        "name": sd.get("projectName", ""),
                        "description": sd.get("description", ""),
                    }
                )
            return projects
        except (ApiError, ApiPermissionError, ApiNotFoundError) as e:
            raise AtlassianDiscoveryError(f"Failed to discover projects: {e}")

    def discover_request_types(self, project_id: str) -> list[dict]:
        """
        Discover request types for a given project.

        Args:
            project_id: Service Desk project ID or key

        Returns:
            List of request type dictionaries
        """
        try:
            request_types = self.client.get_request_types(project_id)
            # Handle both list and paginated response formats
            if isinstance(request_types, list):
                values = request_types
            else:
                values = request_types.get("values", [])
            result = []
            for rt in values:
                result.append(
                    {
                        "id": str(rt.get("id", "")),
                        "name": rt.get("name", ""),
                        "description": rt.get("description", ""),
                        "issue_type_id": rt.get("issueTypeId", ""),
                    }
                )
            return result
        except (ApiError, ApiPermissionError, ApiNotFoundError) as e:
            raise AtlassianDiscoveryError(f"Failed to discover request types: {e}")

    def discover_custom_fields(
        self, project_id: str | None = None, request_type_id: str | None = None
    ) -> list[dict]:
        """
        Discover available custom fields.

        Args:
            project_id: Optional project ID for project-specific fields
            request_type_id: Optional request type ID for request-specific fields

        Returns:
            List of field dictionaries
        """
        try:
            # Get all fields via Jira API
            fields = self._get_all_fields()
            result = []
            for field in fields:
                if not field.get("custom", False):
                    continue  # Skip standard fields
                result.append(
                    {
                        "id": field.get("id", ""),
                        "name": field.get("name", ""),
                        "clause_names": field.get("clauseNames", []),
                        "field_type": field.get("schema", {}).get("type", "unknown"),
                        "required": False,  # Would need request type context
                    }
                )

            # If request type specified, get required fields
            if project_id and request_type_id:
                self._enrich_fields_with_request_type_info(
                    result, project_id, request_type_id
                )

            return result
        except (ApiError, ApiPermissionError, ApiNotFoundError) as e:
            raise AtlassianDiscoveryError(f"Failed to discover custom fields: {e}")

    def _get_all_fields(self) -> list:
        """Get all fields from Jira REST API."""
        return self.client.get("rest/api/2/field")

    def _enrich_fields_with_request_type_info(
        self, fields: list[dict], project_id: str, request_type_id: str
    ):
        """Enrich fields with required flag from request type."""
        try:
            rt_fields = self.client.get(
                f"rest/servicedeskapi/servicedesk/{project_id}/requesttype/{request_type_id}/field"
            )
            required_ids = {
                f["fieldId"]
                for f in rt_fields.get("requestTypeFields", [])
                if f.get("required", False)
            }
            for field in fields:
                if field["id"] in required_ids:
                    field["required"] = True
        except Exception as e:
            logger.debug(f"Could not get request type field details: {e}")

    def discover_priorities(self) -> list[dict]:
        """
        Discover available priorities.

        Returns:
            List of priority dictionaries
        """
        try:
            priorities = self.client.get("rest/api/2/priority/")
            result = []
            for priority in priorities:
                result.append(
                    {
                        "id": priority.get("id", ""),
                        "name": priority.get("name", ""),
                        "description": priority.get("description", ""),
                        "icon_url": priority.get("iconUrl", ""),
                    }
                )
            return result
        except (ApiError, ApiPermissionError, ApiNotFoundError) as e:
            raise AtlassianDiscoveryError(f"Failed to discover priorities: {e}")

    def discover_issue_types(self, project_id: str) -> list[dict]:
        """
        Discover available issue types for a project.

        Args:
            project_id: Project ID or key

        Returns:
            List of issue type dictionaries
        """
        try:
            # Get project details with issue types
            project = self.client.get(f"rest/api/2/project/{project_id}")
            issue_types = project.get("issueTypes", [])
            result = []
            for it in issue_types:
                result.append(
                    {
                        "id": it.get("id", ""),
                        "name": it.get("name", ""),
                        "description": it.get("description", ""),
                        "subtask": it.get("subtask", False),
                    }
                )
            return result
        except (ApiError, ApiPermissionError, ApiNotFoundError) as e:
            raise AtlassianDiscoveryError(f"Failed to discover issue types: {e}")
