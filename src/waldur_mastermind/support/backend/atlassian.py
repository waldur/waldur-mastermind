import collections
import functools
import logging
import os
import re
import unicodedata
from io import BytesIO

import dateutil.parser
import requests
from atlassian import ServiceDesk
from atlassian.errors import ApiError, ApiNotFoundError, ApiPermissionError
from constance import config
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.template import Context, Template
from django.utils import timezone
from django.utils.functional import cached_property
from requests.auth import HTTPBasicAuth

from waldur_core.core.enums import CoreStates
from waldur_core.core.models import User
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_mastermind.support import models

from . import SupportBackend, SupportBackendType

PADDING = 3
CHARS_LIMIT = 255

logger = logging.getLogger(__name__)

Settings = collections.namedtuple(
    "Settings",
    [
        "backend_url",
        "username",
        "password",
        "email",
        "token",
        "personal_access_token",
        "oauth2_client_id",
        "oauth2_access_token",
        "oauth2_token_type",
    ],
)

logger = logging.getLogger(__name__)


class JiraBackendError(ServiceBackendError):
    pass


def check_captcha(e):
    if e.response is None:
        return False
    if not hasattr(e.response, "headers"):
        return False
    if "X-Seraph-LoginReason" not in e.response.headers:
        return False
    return e.response.headers["X-Seraph-LoginReason"] == "AUTHENTICATED_FAILED"


def reraise_exceptions(func):
    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except (
            ApiError,
            ApiPermissionError,
            ApiNotFoundError,
            requests.exceptions.RequestException,
        ) as e:
            raise JiraBackendError(e)

    return wrapped


def adf_from_text(text: str) -> dict:
    parts = []
    for i, line in enumerate((text or "").split("\n")):
        if i:
            parts.append({"type": "hardBreak"})
        if line:
            parts.append({"type": "text", "text": line})
    if not parts:
        parts = [{"type": "text", "text": ""}]
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": parts}],
    }


class AttachmentSynchronizer:
    def __init__(self, backend, current_issue, backend_issue):
        self.backend = backend
        self.current_issue = current_issue
        self.backend_issue = backend_issue

    def perform_update(self):
        if self.stale_attachment_ids:
            models.Attachment.objects.filter(
                backend_id__in=self.stale_attachment_ids
            ).delete()

        for attachment_id in self.new_attachment_ids:
            self._add_attachment(
                self.current_issue, self.get_backend_attachment(attachment_id)
            )

    def get_current_attachment(self, attachment_id):
        return self.current_attachments_map[attachment_id]

    def get_backend_attachment(self, attachment_id):
        return self.backend_attachments_map[attachment_id]

    @cached_property
    def current_attachments_map(self):
        return {
            str(attachment.backend_id): attachment
            for attachment in self.current_issue.attachments.all()
        }

    @cached_property
    def current_attachments_ids(self):
        return set(self.current_attachments_map.keys())

    @cached_property
    def backend_attachments_map(self):
        if self.backend.api_version != 2:
            attachments = self.backend.get(
                f"rest/servicedeskapi/request/{self.current_issue.key}/attachment/"
            ).get("values", [])
            return {
                str(attachment["_links"]["jiraRest"].split("/")[-1]): attachment
                for attachment in attachments
            }
        else:
            attachments = (
                self.backend.get(
                    f"rest/api/2/issue/{self.current_issue.key}?fields=attachment"
                )
                .get("fields", {})
                .get("attachment", [])
            )
            return {attachment["id"]: attachment for attachment in attachments}

    @cached_property
    def backend_attachments_ids(self):
        return set(self.backend_attachments_map.keys())

    @cached_property
    def stale_attachment_ids(self):
        return self.current_attachments_ids - self.backend_attachments_ids

    @cached_property
    def new_attachment_ids(self):
        return self.backend_attachments_ids - self.current_attachments_ids

    @cached_property
    def updated_attachments_ids(self):
        return filter(self._is_attachment_updated, self.backend_attachments_ids)

    def _is_attachment_updated(self, attachment_id):
        if attachment_id not in self.current_attachments_ids:
            return False

        return True

    def _download_file(self, url):
        """
        Download file from URL using secure JIRA session.
        :return: byte stream
        :raises: requests.RequestException
        """
        session = self.backend.manager._session
        response = session.get(url)
        response.raise_for_status()
        return BytesIO(response.content)

    def _add_attachment(self, issue, backend_attachment):
        attachment = models.Attachment(
            issue=issue,
            backend_id=backend_attachment.get("id")
            or backend_attachment["_links"]["jiraRest"].split("/")[-1],
            state=CoreStates.OK,
        )
        try:
            content = self._download_file(
                backend_attachment.get("content")
                or backend_attachment["_links"]["content"]
            )
        except ApiError as error:
            logger.error(
                f"Unable to load attachment for issue with backend id {issue.backend_id}. Error: {error})."
            )
            return

        self.backend._backend_attachment_to_attachment(backend_attachment, attachment)

        try:
            attachment.save()
        except IntegrityError:
            logger.debug(
                "Unable to create attachment issue_id=%s, backend_id=%s, "
                "because it already exists in Waldur.",
                issue.id,
                backend_attachment.get("id"),
            )

        attachment.file.save(backend_attachment["filename"], content, save=True)


class CommentSynchronizer:
    def __init__(self, backend, current_issue, backend_issue):
        self.backend = backend
        self.current_issue = current_issue
        self.backend_issue = backend_issue

    def delete_old_comments(self):
        if self.stale_comments_ids:
            models.Comment.objects.filter(
                backend_id__in=self.stale_comments_ids
            ).delete()

    def get_current_comment(self, comment_id):
        return self.current_comments_map[comment_id]

    def get_backend_comment(self, comment_id):
        return self.backend_comments_map[comment_id]

    @cached_property
    def current_comments_map(self):
        return {
            str(comment.backend_id): comment
            for comment in self.current_issue.comments.all()
        }

    @cached_property
    def current_comments_ids(self):
        return set(self.current_comments_map.keys())

    @cached_property
    def backend_comments_map(self):
        comments = self.backend.get(
            f"/rest/api/2/issue/{self.current_issue.key}/comment"
        ).get("comments", [])
        return {comment["id"]: comment for comment in comments}

    @cached_property
    def backend_comments_ids(self):
        return set(self.backend_comments_map.keys())

    @cached_property
    def stale_comments_ids(self):
        return self.current_comments_ids - self.backend_comments_ids

    @cached_property
    def new_comments_ids(self):
        return self.backend_comments_ids - self.current_comments_ids

    @cached_property
    def existing_comments_ids(self):
        return self.current_comments_ids & self.backend_comments_ids

    def perform_update(self):
        """
        Synchronize comments from backend to Waldur.
        - Delete comments that exist in Waldur but not in backend
        - Create comments that exist in backend but not in Waldur
        - Update existing comments
        """
        # Delete stale comments
        self.delete_old_comments()

        # Create new comments
        for comment_id in self.new_comments_ids:
            backend_comment = self.get_backend_comment(comment_id)
            comment = models.Comment(
                issue=self.current_issue,
                backend_id=comment_id,
                state=CoreStates.OK,
            )
            self.backend._backend_comment_to_comment(backend_comment, comment)
            try:
                comment.save()
            except IntegrityError:
                logger.debug(
                    "Unable to create comment issue_id=%s, backend_id=%s, "
                    "because it already exists in Waldur.",
                    self.current_issue.id,
                    comment_id,
                )

        # Update existing comments
        for comment_id in self.existing_comments_ids:
            current_comment = self.get_current_comment(comment_id)
            backend_comment = self.get_backend_comment(comment_id)
            self.backend._backend_comment_to_comment(backend_comment, current_comment)
            current_comment.save()


class ServiceDeskBackend(SupportBackend):
    backend_name = SupportBackendType.ATLASSIAN

    def __init__(self, settings_override=None):
        self._settings_override = settings_override or {}
        self.settings = Settings(
            backend_url=self._get_config("ATLASSIAN_API_URL")
            + ("/" if not self._get_config("ATLASSIAN_API_URL").endswith("/") else ""),
            username=self._get_config("ATLASSIAN_USERNAME"),
            password=self._get_config("ATLASSIAN_PASSWORD"),
            email=self._get_config("ATLASSIAN_EMAIL"),
            token=self._get_config("ATLASSIAN_TOKEN"),
            personal_access_token=self._get_config("ATLASSIAN_PERSONAL_ACCESS_TOKEN"),
            oauth2_client_id=self._get_config("ATLASSIAN_OAUTH2_CLIENT_ID"),
            oauth2_access_token=self._get_config("ATLASSIAN_OAUTH2_ACCESS_TOKEN"),
            oauth2_token_type=self._get_config("ATLASSIAN_OAUTH2_TOKEN_TYPE"),
        )
        self.verify = self._get_config("ATLASSIAN_VERIFY_SSL")
        self.api_version = 2 if self._get_config("ATLASSIAN_USE_OLD_API") else 3

    def _get_config(self, key, default=None):
        """Get config value from provider settings override or Constance."""
        if key in self._settings_override:
            return self._settings_override[key]
        return getattr(config, key, default)

    @classmethod
    def from_settings(cls, settings_dict):
        """Create a ServiceDeskBackend with provider-specific settings."""
        return cls(settings_override=settings_dict)

    @cached_property
    def manager(self):
        return self._create_service_desk_client()

    def attachment_destroy_is_available(self, attachment=None):
        return True

    def _create_service_desk_client(self):
        """Create ServiceDesk client with appropriate authentication method."""
        base_kwargs = {
            "url": self.settings.backend_url,
            "verify_ssl": self.verify,
        }

        # Priority order: OAuth 2.0 > Personal Access Token > API Token > Basic Auth
        if self._has_oauth2_config():
            return self._create_oauth2_client(base_kwargs)
        elif self.settings.personal_access_token:
            return self._create_pat_client(base_kwargs)
        elif self.settings.token:
            return self._create_api_token_client(base_kwargs)
        else:
            return self._create_basic_auth_client(base_kwargs)

    def _has_oauth2_config(self):
        """Check if OAuth 2.0 configuration is available."""
        return self.settings.oauth2_client_id and self.settings.oauth2_access_token

    def _create_oauth2_client(self, base_kwargs):
        """Create ServiceDesk client with OAuth 2.0 authentication."""
        oauth2_dict = {
            "client_id": self.settings.oauth2_client_id,
            "token": {
                "access_token": self.settings.oauth2_access_token,
                "token_type": self.settings.oauth2_token_type,
            },
        }
        logger.info("Using OAuth 2.0 authentication for Atlassian ServiceDesk")
        return ServiceDesk(oauth2=oauth2_dict, **base_kwargs)

    def _create_pat_client(self, base_kwargs):
        """Create ServiceDesk client with Personal Access Token."""
        logger.info(
            "Using Personal Access Token authentication for Atlassian ServiceDesk"
        )
        return ServiceDesk(token=self.settings.personal_access_token, **base_kwargs)

    def _create_api_token_client(self, base_kwargs):
        """Create ServiceDesk client with API Token (Cloud)."""
        # For Atlassian Cloud, use email as username if username is not set
        username = self.settings.username or self.settings.email
        if not username:
            logger.error(
                "API Token authentication requires username or email to be set"
            )
        logger.info("Using API Token authentication for Atlassian Cloud ServiceDesk")
        return ServiceDesk(
            username=username,
            password=self.settings.token,
            cloud=True,
            **base_kwargs,
        )

    def _create_basic_auth_client(self, base_kwargs):
        """Create ServiceDesk client with Basic Authentication."""
        logger.info("Using Basic Authentication for Atlassian ServiceDesk")
        # Determine if this is a cloud instance based on URL
        is_cloud = ".atlassian.net" in self.settings.backend_url.lower()
        return ServiceDesk(
            username=self.settings.username,
            password=self.settings.password,
            cloud=is_cloud,
            **base_kwargs,
        )

    def get_authentication_method(self):
        """Get the current authentication method being used."""
        if self._has_oauth2_config():
            return "OAuth 2.0"
        elif self.settings.personal_access_token:
            return "Personal Access Token"
        elif self.settings.token:
            return "API Token (Cloud)"
        else:
            return "Basic Authentication"

    def validate_authentication_config(self):
        """Validate authentication configuration and log warnings."""
        auth_method = self.get_authentication_method()

        if auth_method == "Basic Authentication":
            if ".atlassian.net" in self.settings.backend_url.lower():
                logger.warning(
                    "Using Basic Authentication with Atlassian Cloud. "
                    "Consider using API Tokens for better security."
                )
        elif auth_method == "OAuth 2.0":
            if not all(
                [
                    self.settings.oauth2_client_id,
                    self.settings.oauth2_access_token,
                ]
            ):
                logger.error("Incomplete OAuth 2.0 configuration detected")
                return False

        logger.info(f"Atlassian ServiceDesk authentication method: {auth_method}")
        return True

    def get(self, path, **kwargs):
        headers = kwargs.get("headers", {})
        headers["X-ExperimentalApi"] = "opt-in"
        kwargs["headers"] = headers
        return self.manager.get(path, **kwargs)

    def _get_jira_auth(self):
        """Get authentication for direct Jira REST API calls"""
        if self.settings.email and self.settings.token:
            return HTTPBasicAuth(self.settings.email, self.settings.token)
        elif self.settings.username and self.settings.password:
            return HTTPBasicAuth(self.settings.username, self.settings.password)
        else:
            raise ServiceBackendError(
                "No valid authentication credentials for Jira REST API"
            )

    def _get_jira_headers(self):
        """Get headers for Jira REST API calls"""
        return {"Accept": "application/json", "Content-Type": "application/json"}

    def _make_jira_request(self, endpoint, method="GET", **kwargs):
        """Make a direct Jira REST API request as fallback"""
        base_url = self.settings.backend_url.rstrip("/")
        url = f"{base_url}{endpoint}"
        auth = self._get_jira_auth()
        headers = self._get_jira_headers()

        try:
            response = requests.request(
                method=method,
                url=url,
                auth=auth,
                headers=headers,
                verify=self.verify,
                timeout=30,
                **kwargs,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Jira REST API fallback failed for {endpoint}: {e}")
            raise ServiceBackendError(f"Jira REST API request failed: {e}")

    def _get_service_desk_by_id_fallback(self, project_id):
        """Fallback to get service desk info via Jira REST API"""
        endpoint = f"/rest/servicedeskapi/servicedesk/{project_id}"
        return self._make_jira_request(endpoint)

    def _get_request_types_fallback(self, project_id):
        """Fallback to get request types via Jira REST API"""
        endpoint = f"/rest/servicedeskapi/servicedesk/{project_id}/requesttype"
        return self._make_jira_request(endpoint)

    def _get_request_type_fields_fallback(self, project_id, request_type_id):
        """Fallback to get request type fields via Jira REST API"""
        endpoint = f"/rest/servicedeskapi/servicedesk/{project_id}/requesttype/{request_type_id}/field"
        return self._make_jira_request(endpoint)

    def _search_users_fallback(self, query, max_results=50):
        """Fallback to search users via Jira REST API"""
        endpoint = f"/rest/api/2/user/search?query={requests.utils.quote(query)}&maxResults={max_results}"
        return self._make_jira_request(endpoint)

    def _get_issue_metadata_fallback(self, project_key=None):
        """Get issue creation metadata via Jira REST API"""
        endpoint = "/rest/api/2/issue/createmeta"
        if project_key:
            endpoint += f"?projectKeys={project_key}"
        return self._make_jira_request(endpoint)

    def _search_customers_hybrid(self, project_id, user_email, start=0, limit=50):
        """Search customers with hybrid Service Desk API + Jira REST API fallback"""
        try:
            # Try Service Desk API first
            return self.manager.get_customers(
                project_id, query=user_email, start=start, limit=limit
            )
        except (ApiPermissionError, ApiError, requests.exceptions.HTTPError) as e:
            # Fallback to Jira user search
            if "401" in str(e) or "403" in str(e):
                logger.info(
                    "Service Desk customer search denied, trying Jira user search fallback"
                )
                try:
                    users = self._search_users_fallback(user_email, max_results=limit)
                    # Convert Jira user format to Service Desk customer format
                    customers = []
                    for user in users:
                        if user.get("emailAddress", "").lower() == user_email.lower():
                            customers.append(
                                {
                                    "emailAddress": user.get("emailAddress"),
                                    "displayName": user.get("displayName"),
                                    "accountId": user.get("accountId"),
                                    "name": user.get("name"),
                                }
                            )
                    return {
                        "values": customers,
                        "size": len(customers),
                        "isLastPage": True,  # Jira user search doesn't support pagination the same way
                    }
                except Exception as fe:
                    logger.warning(f"Jira user search fallback also failed: {fe}")
                    # Return empty result to allow customer creation
                    return {"values": [], "size": 0, "isLastPage": True}
            else:
                raise

    @reraise_exceptions
    def get_service_desk_id(self):
        try:
            return int(self._get_config("ATLASSIAN_PROJECT_ID"))
        except ValueError:
            try:
                # Try Service Desk API first
                return int(
                    self.manager.get_service_desk_by_id(
                        self._get_config("ATLASSIAN_PROJECT_ID")
                    ).get("id")
                )
            except (ApiPermissionError, ApiError, requests.exceptions.HTTPError) as e:
                # Fallback to Jira REST API
                if "401" in str(e) or "403" in str(e):
                    logger.info(
                        "Service Desk API access denied, trying Jira REST API fallback"
                    )
                    try:
                        sd_info = self._get_service_desk_by_id_fallback(
                            self._get_config("ATLASSIAN_PROJECT_ID")
                        )
                        return int(sd_info.get("id"))
                    except Exception as fe:
                        logger.warning(f"Jira REST API fallback also failed: {fe}")
                        project_id = self._get_config("ATLASSIAN_PROJECT_ID")
                        raise ServiceBackendError(
                            f"Service desk ID not found for key {project_id}. "
                            f"Both Service Desk API and Jira REST API failed."
                        )
                raise
            except ValueError:
                project_id = self._get_config("ATLASSIAN_PROJECT_ID")
                raise ServiceBackendError(
                    f"Service desk ID not found for key {project_id}."
                )

    @reraise_exceptions
    def ping(self, raise_exception=False):
        try:
            # Validate authentication configuration first
            if not self.validate_authentication_config():
                if raise_exception:
                    raise JiraBackendError("Invalid authentication configuration")
                return False

            # Test the connection
            self.manager.get_info()
        except Exception as e:
            if raise_exception:
                raise JiraBackendError(e)
            return False
        else:
            return True

    @reraise_exceptions
    def create_issue(self, issue: models.Issue):
        logger.info(
            "Creating JIRA issue for caller %s (email: %s)",
            issue.caller.username,
            issue.caller.email,
        )

        if not issue.caller:
            logger.error("Cannot create issue - no caller specified")
            raise ServiceBackendError(
                "Issue is not created because no caller is specified."
            )

        if not issue.caller.email:
            logger.error("Cannot create issue - caller user does not have email")
            raise ServiceBackendError(
                "Issue is not created because caller user does not have email."
            )

        # Get request type directly by name (no mapping)
        request_type = models.RequestType.objects.filter(
            name=issue.type, is_active=True
        ).first()

        if not request_type:
            # Try to pull request types and retry
            self.pull_request_types()
            request_type = models.RequestType.objects.filter(
                name=issue.type, is_active=True
            ).first()

        if not request_type:
            raise ServiceBackendError(
                f"Issue is not created because request type '{issue.type}' is not found or not active."
            )

        logger.info("Creating customer request in JIRA")

        values_dict = {"summary": issue.summary, "description": issue.description}

        if self._get_config("ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED"):
            logger.debug("Custom field mapping is enabled, setting custom fields")
            custom_fields = self._get_custom_fields(issue)
            values_dict.update(custom_fields)

        # TODO: Consider adding validation to check if all fields in values_dict
        # are allowed by the request_type. If not, user should get 400 error
        # and admin should be notified that request types are misconfigured

        request = self.manager.create_customer_request(
            self.get_service_desk_id(),
            request_type.backend_id,
            values_dict=values_dict,
            raise_on_behalf_of=issue.caller.email,
        )

        self._backend_issue_to_issue(request, issue)
        issue.state = CoreStates.OK
        issue.save()
        logger.info(
            "Issue creation completed. Local issue ID: %s, JIRA key: %s",
            getattr(issue, "id", "unknown"),
            issue.key,
        )

    def delete_issue(self, issue):
        try:
            self.manager.delete(
                f"/rest/api/{self.api_version}/issue/{issue.backend_id}"
            )
        except requests.exceptions.HTTPError:
            logger.debug(
                "Unable to delete issue with key=%s, "
                "because it has already been deleted on backend.",
                issue.backend_id,
            )

    def _get_custom_fields(self, issue):
        args = {}

        if issue.reporter:
            args[
                self.get_field_id_by_name(self._get_config("ATLASSIAN_REPORTER_FIELD"))
            ] = issue.reporter.name
        if issue.impact:
            args[
                self.get_field_id_by_name(self._get_config("ATLASSIAN_IMPACT_FIELD"))
            ] = issue.impact
        if issue.priority:
            args["priority"] = {"name": issue.priority}

        def set_custom_field(field_name, value):
            if value and self._get_config(field_name):
                field_id = self.get_field_id_by_name(self._get_config(field_name))
                if field_id:
                    args[field_id] = value

        if issue.customer:
            set_custom_field("ATLASSIAN_ORGANISATION_FIELD", issue.customer.name)

        if issue.project:
            set_custom_field("ATLASSIAN_PROJECT_FIELD", issue.project.name)

        if issue.resource:
            set_custom_field("ATLASSIAN_AFFECTED_RESOURCE_FIELD", issue.resource)

        if issue.template:
            set_custom_field("ATLASSIAN_TEMPLATE_FIELD", issue.template.name)

        return args

    @reraise_exceptions
    def get_field_id_by_name(self, field_name):
        if not field_name:
            return None
        try:
            fields = getattr(self, "_fields")
        except AttributeError:
            fields = self._fields = self.get("/rest/api/2/field")
        try:
            return next(f["id"] for f in fields if field_name in f["clauseNames"])
        except StopIteration:
            logger.warning(
                "Can't find custom field '%s' in JIRA. Skipping field.", field_name
            )
            return None

    @reraise_exceptions
    def create_user(self, user: User):
        logger.info(
            "Creating user in JIRA. Username: %s, Email: %s, Shared username mode: %s",
            user.username,
            user.email,
            self._get_config("ATLASSIAN_SHARED_USERNAME"),
        )
        # in case usernames are shared, skip lookups and create SupportCustomer if it is missing
        if self._get_config("ATLASSIAN_SHARED_USERNAME"):
            try:
                user.supportcustomer
                logger.info(
                    "Support customer already exists for user %s", user.username
                )
            except ObjectDoesNotExist:
                logger.info("Creating new support customer for user %s", user.username)
                support_customer = models.SupportCustomer(
                    user=user, backend_id=user.username
                )
                support_customer.save()
            return

        logger.info("Creating JSM customer for %s", user.email)

        # Handle pagination to search all customers, not just first page
        start = 0
        limit = 50
        customers = []

        while True:
            response = self._search_customers_hybrid(
                self._get_config("ATLASSIAN_PROJECT_ID"),
                user.email,
                start=start,
                limit=limit,
            )
            batch = response.get("values", [])
            if not batch:
                break

            customers.extend(batch)

            # Check if we found the exact customer in this batch
            existing_customer = next(
                (c for c in batch if c.get("emailAddress") == user.email), None
            )
            if existing_customer:
                customers = [existing_customer]
                break

            # Check if we've reached the end
            if len(batch) < limit or response.get("isLastPage", True):
                break
            start += limit

        if not customers:
            logger.info("User not found, creating new JSM customer: %s", user.username)
            backend_customer = self.manager.create_customer(user.full_name, user.email)
            logger.info(
                "Successfully created JSM customer: %s", backend_customer["accountId"]
            )

    @reraise_exceptions
    def pull_request_types(self):
        """Pull request types from Atlassian Service Desk via direct API call.

        Uses direct HTTP request instead of the atlassian library's
        get_request_types method to avoid TypeError in the library's
        raise_for_status when the API returns a non-dict JSON error body.
        See Sentry CSCS-PY.
        """
        try:
            response = self._get_request_types_fallback(
                self._get_config("ATLASSIAN_PROJECT_ID")
            )
            request_types = response.get("values", [])
        except Exception as e:
            raise ServiceBackendError(f"Failed to retrieve request types: {e}")

        try:
            with transaction.atomic():
                # Collect current backend IDs from the API response
                current_backend_ids = set()

                for request_type in request_types:
                    backend_id = str(request_type.get("id", ""))
                    name = request_type.get("name", "")
                    current_backend_ids.add(backend_id)

                    # Simplified approach: Only fetch request type fields if absolutely necessary
                    # This optimization removes the extra API call per request type for better performance
                    # Fields can be fetched later when actually needed for issue creation
                    request_type_fields = []

                    # Check for existing request types with the same name but different backend_id
                    # Remove stale duplicates before creating/updating
                    existing_with_same_name = models.RequestType.objects.filter(
                        name=name
                    ).exclude(backend_id=backend_id)

                    if existing_with_same_name.exists():
                        stale_count = existing_with_same_name.count()
                        logger.info(
                            f"Removing {stale_count} stale request type(s) with name '{name}' "
                            f"(keeping backend_id={backend_id})"
                        )
                        existing_with_same_name.delete()

                    models.RequestType.objects.update_or_create(
                        backend_id=backend_id,
                        defaults={
                            "name": name,
                            "backend_name": self._get_config(
                                "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE"
                            ),
                            "fields": request_type_fields,
                            "issue_type_name": request_type.get("name", "Task"),
                        },
                    )

                # Remove orphaned request types that are no longer in JIRA
                # (only for this backend to avoid cross-backend conflicts)
                orphaned_types = models.RequestType.objects.exclude(
                    backend_id__in=current_backend_ids
                )
                if orphaned_types.exists():
                    orphaned_count = orphaned_types.count()
                    logger.info(
                        f"Removing {orphaned_count} orphaned request type(s) no longer in JIRA"
                    )
                    orphaned_types.delete()

                logger.info(
                    "Successfully pulled %d request types from JIRA", len(request_types)
                )
        except Exception as e:
            logger.exception("Failed to pull request types from JIRA: %s", str(e))
            raise

    def get_issue_details(self):
        return {"type": self._get_config("ATLASSIAN_DEFAULT_OFFERING_ISSUE_TYPE")}

    @reraise_exceptions
    def _add_comment(self, issue_key, body, is_internal):
        """Add a comment to an issue using Service Desk API.

        Args:
            issue_key: The issue key/ID
            body: Comment body text
            is_internal: True for internal comments, False for public comments
        """
        # Convert is_internal to public flag (inverted)
        public = not is_internal
        return self.manager.create_request_comment(issue_key, body, public=public)

    @reraise_exceptions
    def _get_property(self, object_name, object_id, property_name):
        """Get a property from a JIRA object using hybrid fallback.

        This method provides compatibility with the old JIRA API for getting
        properties like comment visibility settings.
        """
        try:
            # Use hybrid Jira REST API fallback for properties
            url = f"/rest/api/2/{object_name}/{object_id}/properties/{property_name}"
            response = self._hybrid_jira_get(url)
            return response
        except Exception:
            # Fallback to default behavior for Service Desk API
            return {"value": {"internal": False}}

    @reraise_exceptions
    def get_backend_comment(self, issue_backend_id, comment_backend_id):
        """Get a comment from the backend using Service Desk API."""
        try:
            # Use Service Desk API to get comment
            return self.manager.get_request_comment_by_id(
                issue_backend_id, comment_backend_id
            )
        except Exception:
            # Fallback to empty structure if comment not found
            return {"id": comment_backend_id, "body": "", "author": {"accountId": ""}}

    @reraise_exceptions
    def create_confirmation_comment(self, issue, comment_tmpl=""):
        if not comment_tmpl:
            comment_tmpl = self.get_confirmation_comment_template(issue.type)

        if not comment_tmpl:
            return

        body = (
            Template(comment_tmpl)
            .render(Context({"issue": issue}, autoescape=False))
            .strip()
        )
        return self._add_comment(issue.backend_id, body, is_internal=False)

    def _get_filename(self, path):
        # JIRA does not support composite symbols from Latin-1 charset.
        # Hence we need to use NFD normalization which translates
        # each character into its decomposed form.
        path = unicodedata.normalize("NFD", path)
        limit = CHARS_LIMIT - PADDING
        fname = os.path.basename(path)
        filename = fname.split(".")[0]
        filename_extension = fname.split(".")[1:]
        count = (
            len(".".join(filename_extension).encode("utf-8")) + 1
            if filename_extension
            else 0
        )
        char_limit = 0

        for char in filename:
            count += len(char.encode("utf-8"))
            if count > limit:
                break
            else:
                char_limit += 1

        if not char_limit:
            raise ApiError("Attachment filename is very long.")

        tmp = [filename[:char_limit]]
        tmp.extend(filename_extension)
        filename = ".".join(tmp)
        return filename

    def _upload_file(self, issue, upload_file, filename):
        url = self.manager.url_joiner(
            self.manager.url, f"/rest/api/2/issue/{issue.key}/attachments"
        )
        files = {
            "file": (filename, upload_file),
        }
        headers = {
            "X-Atlassian-Token": "no-check",
        }
        req = requests.Request(
            "POST", url, headers=headers, files=files, auth=self.manager._session.auth
        )
        prepped = req.prepare()
        prepped.body = re.sub(
            b"filename=.*", b'filename="%s"\r' % filename.encode("utf-8"), prepped.body
        )
        r = self.manager._session.send(prepped)

        return r.json()

    @reraise_exceptions
    def create_attachment(self, attachment: models.Attachment):
        file = attachment.file
        filename = self._get_filename(file.name)
        backend_attachment = self._upload_file(
            attachment.issue, file.file.read(), filename
        )

        # Check if file upload was successful
        if not backend_attachment or len(backend_attachment) == 0:
            raise JiraBackendError(f"Failed to upload attachment {filename}")

        attachment.backend_id = backend_attachment[0]["id"]
        attachment.save(update_fields=["backend_id"])

    @reraise_exceptions
    def create_comment(self, comment: models.Comment):
        backend_comment = self.manager.create_request_comment(
            comment.issue.backend_id, comment.prepare_message(), comment.is_public
        )
        comment.backend_id = backend_comment["id"]
        comment.save(update_fields=["backend_id"])

    def _backend_issue_to_issue(self, backend_issue, issue):
        def _get_field_value(field_name):
            return next(
                (
                    f
                    for f in backend_issue["requestFieldValues"]
                    if f["fieldId"] == field_name
                ),
                {},
            ).get("value", "")

        issue.key = backend_issue["issueKey"]
        issue.backend_id = backend_issue["issueKey"]

        # Get the current status from the Service Desk response
        current_status = backend_issue.get("currentStatus", {}).get("status", "")
        issue.status = current_status

        # Check for resolution (only present when issue is resolved)
        resolution = (
            self.manager.get(
                f"/rest/api/{self.api_version}/issue/{issue.key}?fields=resolution"
            )
            .get("fields", {})
            .get("resolution", {})
        )

        if resolution:
            issue.resolution = resolution.get("name", "")
        else:
            issue.resolution = ""
        issue.link = backend_issue["_links"].get("agent") or backend_issue[
            "_links"
        ].get("web")
        issue.summary = (
            backend_issue.get("summary")
            or (
                backend_issue["requestFieldValues"]
                and next(
                    f["value"]
                    for f in backend_issue["requestFieldValues"]
                    if f["fieldId"] == "summary"
                )
            )
            or ""
        )
        issue.description = _get_field_value("description")
        # issue.type = backend_issue.fields.issuetype.name
        # issue.resolution_date = backend_issue.fields.resolutiondate or None
        issue.feedback_request = (
            self.get_request_feedback_field(backend_issue)
            if self._get_config("ATLASSIAN_REQUEST_FEEDBACK_FIELD")
            else True
        )

        def get_support_user_by_field(field_name):
            backend_user = backend_issue.get(field_name, {}).get("accountId", None)

            if backend_user:
                return self.get_or_create_support_user(backend_user)

        impact_field_id = self.get_field_id_by_name(
            self._get_config("ATLASSIAN_IMPACT_FIELD")
        )
        impact = _get_field_value(impact_field_id)
        if impact:
            issue.impact = impact

        assignee = get_support_user_by_field("assignee")
        if assignee:
            issue.assignee = assignee

        reporter = get_support_user_by_field("reporter")
        if reporter:
            issue.reporter = reporter

        # Update resource backend_id if issue is connected to a resource and custom field mapping is enabled
        self._update_resource_backend_id_from_custom_fields(issue)

    def get_or_create_support_user(self, user_id):
        # Use filter().first() to handle potential duplicates gracefully
        author = models.SupportUser.objects.filter(
            backend_id=user_id,
            backend_name=self.backend_name,
        ).first()
        if not author:
            author = models.SupportUser.objects.create(
                backend_id=user_id,
                backend_name=self.backend_name,
            )
        return author

    def _backend_comment_to_comment(self, backend_comment, comment: models.Comment):
        comment.update_message(backend_comment["body"])
        # Always update the author from backend data
        comment.author = self.get_or_create_support_user(
            backend_comment["author"].get("accountId")
            or backend_comment["author"].get("key")
        )
        # Service Desk API uses "public", REST API v2/v3 uses "jsdPublic"
        if "public" in backend_comment:
            comment.is_public = backend_comment["public"]
        elif "jsdPublic" in backend_comment:
            comment.is_public = backend_comment["jsdPublic"]
        else:
            comment.is_public = True

    def _backend_attachment_to_attachment(self, backend_attachment, attachment):
        attachment.created = dateutil.parser.parse(
            backend_attachment["created"].get("iso8601")
            if not self._get_config("ATLASSIAN_USE_OLD_API")
            else backend_attachment["created"]
        )
        attachment.author = self.get_or_create_support_user(
            backend_attachment["author"].get("accountId")
            or backend_attachment["author"].get("key")
        )

    @reraise_exceptions
    def update_issue_from_jira(self, issue):
        start_time = timezone.now()
        customer_request = self.manager.get_customer_request(issue.backend_id)
        issue.refresh_from_db()

        if issue.modified > start_time:
            logger.debug(
                "Skipping issue update with key=%s, "
                "because it has been updated from other thread.",
                issue.backend_id,
            )
            return

        self._backend_issue_to_issue(customer_request, issue)
        issue.save()

    @reraise_exceptions
    def delete_issue_from_jira(self, issue):
        try:
            self.manager.get_customer_request(issue.backend_id)
            logger.debug(
                "Skipping issue deletion with key=%s, "
                "because it still exists on backend.",
                issue.backend_id,
            )
        except requests.exceptions.HTTPError:
            issue.delete()

    @reraise_exceptions
    def create_comment_from_jira(self, issue, comment_backend_id):
        backend_comment = self.manager.get_request_comment_by_id(
            issue.backend_id, comment_backend_id
        )
        comment = models.Comment(
            issue=issue, backend_id=comment_backend_id, state=CoreStates.OK
        )
        self._backend_comment_to_comment(backend_comment, comment)

        try:
            comment.save()
        except IntegrityError:
            logger.debug(
                "Unable to create comment issue_id=%s, backend_id=%s, "
                "because it already exists  n Waldur.",
                issue.id,
                comment_backend_id,
            )

    @reraise_exceptions
    def update_comment(self, comment):
        try:
            if self._get_config("ATLASSIAN_USE_OLD_API"):
                payload = {"body": comment.prepare_message()}
            else:
                payload = {"body": adf_from_text(comment.prepare_message())}

            self.manager.put(
                f"/rest/api/{self.api_version}/issue/{comment.issue.key}/comment/{comment.backend_id}",
                data=payload,
            )
        except requests.exceptions.HTTPError:
            logger.debug(
                "Unable to update comment with backend_id=%s, "
                "because it has already been deleted on backend.",
                comment.backend_id,
            )

    @reraise_exceptions
    def update_comment_from_jira(self, comment: models.Comment):
        backend_comment = self.get_backend_comment(
            comment.issue.backend_id, comment.backend_id
        )
        comment.state = CoreStates.OK
        self._backend_comment_to_comment(backend_comment, comment)
        comment.save()

    @reraise_exceptions
    def delete_comment(self, comment):
        try:
            self.manager.delete(
                f"/rest/api/{self.api_version}/issue/{comment.issue.key}/comment/{comment.backend_id}"
            )
        except requests.exceptions.HTTPError:
            logger.debug(
                "Unable to delete comment with backend_id=%s, "
                "because it has already been deleted on backend.",
                comment.backend_id,
            )

    @reraise_exceptions
    def delete_comment_from_jira(self, comment: models.Comment):
        try:
            self.manager.get_request_comment_by_id(
                comment.issue.backend_id, comment.backend_id
            )
            logger.debug(
                "Skipping comment deletion with UUID=%s, "
                "because it still exists on backend.",
                comment.uuid.hex,
            )
        except requests.exceptions.HTTPError:
            comment.delete()

    @reraise_exceptions
    def update_attachment_from_jira(self, issue):
        customer_request = self.manager.get_customer_request(issue.backend_id)
        AttachmentSynchronizer(self, issue, customer_request).perform_update()

    @reraise_exceptions
    def get_users(self):
        start_at = 0
        max_results = 1000
        all_users = []

        while True:
            batch = self.manager.get(
                f"/rest/api/{self.api_version}/user/assignable/search",
                params={
                    "project": self._get_config("ATLASSIAN_PROJECT_ID"),
                    "maxResults": max_results,
                    "startAt": start_at,
                },
            )
            if not batch:
                break
            all_users.extend(batch)
            if len(batch) < max_results:
                break
            start_at += max_results

        return [
            models.SupportUser(
                name=user.get("displayName", ""), backend_id=user.get("accountId", "")
            )
            for user in all_users
            if user.get("accountId")  # Only include users with accountId
        ]

    def pull_support_users(self):
        """
        Pull support users from backend.
        Note that support users are not deleted in JIRA.
        Instead, they are marked as disabled.
        Therefore, Waldur replicates the same behaviour.
        """

        backend_users = self.get_users()

        for backend_user in backend_users:
            user, created = models.SupportUser.objects.get_or_create(
                backend_id=backend_user.backend_id,
                backend_name=self.backend_name,
                defaults={"name": backend_user.name},
            )
            if not created and user.name != backend_user.name:
                user.name = backend_user.name
                user.save()
            if not user.is_active:
                user.is_active = True
                user.save()

        models.SupportUser.objects.filter(backend_name=self.backend_name).exclude(
            backend_id__in=[u.backend_id for u in backend_users]
        ).update(is_active=False)

    @reraise_exceptions
    def pull_priorities(self):
        backend_priorities = self.manager.get(f"/rest/api/{self.api_version}/priority/")

        with transaction.atomic():
            backend_priorities_map = {
                priority["id"]: priority for priority in backend_priorities
            }

            waldur_priorities = {
                priority.backend_id: priority
                for priority in models.Priority.objects.all()
            }

            stale_priorities = set(waldur_priorities.keys()) - set(
                backend_priorities_map.keys()
            )
            models.Priority.objects.filter(backend_id__in=stale_priorities).delete()

            for priority in backend_priorities:
                models.Priority.objects.update_or_create(
                    backend_id=priority["id"],
                    defaults={
                        "name": priority["name"],
                        "description": priority["description"],
                        "icon_url": priority["iconUrl"],
                    },
                )

    @reraise_exceptions
    def create_issue_links(self, issue, linked_issues):
        for linked_issue in linked_issues:
            link_type = self._get_config("ATLASSIAN_LINKED_ISSUE_TYPE")

            payload = {
                "type": {"name": link_type},
                "inwardIssue": {"key": issue.key},
                "outwardIssue": {"key": linked_issue.key},
            }
            self.manager.post(f"/rest/api/{self.api_version}/issueLink", data=payload)

    def create_feedback(self, feedback):
        if feedback.comment:
            support_user, _ = models.SupportUser.objects.get_or_create_from_user(
                feedback.issue.caller
            )
            comment = models.Comment.objects.create(
                issue=feedback.issue,
                description=feedback.comment,
                is_public=False,
                author=support_user,
            )
            self.create_comment(comment)

        if feedback.evaluation:
            field_name = self.get_field_id_by_name(
                self._get_config("ATLASSIAN_SATISFACTION_FIELD")
            )
            kwargs = {field_name: feedback.get_evaluation_display()}
            self.manager.post(
                f"/rest/api/{self.api_version}/issue/{feedback.issue.backend_id}",
                data=kwargs,
            )

    def get_request_feedback_field(self, backend_issue):
        try:
            field_name = self.get_field_id_by_name(
                self._get_config("ATLASSIAN_REQUEST_FEEDBACK_FIELD")
            )
        except JiraBackendError:
            logger.warning("Field request_feedback is not defined in Jira support.")
            return True
        value = next(
            (
                f
                for f in backend_issue["requestFieldValues"]
                if f["fieldId"] == field_name
            ),
            {},
        ).get("value")
        return bool(value)

    @reraise_exceptions
    def delete_attachment(self, attachment):
        try:
            self.manager.delete(
                f"/rest/api/{self.api_version}/attachment/{attachment.backend_id}"
            )
        except requests.exceptions.HTTPError:
            pass

    @reraise_exceptions
    def delete_old_comments(self, issue):
        customer_request = self.manager.get_customer_request(issue.backend_id)
        CommentSynchronizer(self, issue, customer_request).delete_old_comments()

    @reraise_exceptions
    def sync_comments_from_jira(self, issue):
        """
        Synchronize all comments for an issue from Jira.
        Creates new comments, updates existing ones, and deletes stale ones.
        """
        customer_request = self.manager.get_customer_request(issue.backend_id)
        CommentSynchronizer(self, issue, customer_request).perform_update()

    def sync_single_issue(self, issue):
        """
        Synchronize a single issue's data, comments, and attachments from Jira.

        This method is used by both webhooks and manual sync to ensure
        consistent behavior across all sync triggers.

        Args:
            issue: The Issue model instance to sync.
        """
        logger.info(f"Syncing issue {issue.key} (id={issue.id})")

        # Update issue data from Jira
        self.update_issue_from_jira(issue)

        # Sync attachments
        self.update_attachment_from_jira(issue)

        # Sync comments
        self.sync_comments_from_jira(issue)

        logger.info(f"Successfully synced issue {issue.key}")

    def sync_issues(self, issue_id=None):
        """
        Synchronize issue data, comments, and attachments from Jira.

        Args:
            issue_id: Optional issue ID to sync a single issue.
                      If None, syncs all issues with this backend.
        """
        issues = models.Issue.objects.filter(backend_name=self.backend_name)

        if issue_id:
            issues = issues.filter(id=issue_id)

        for issue in issues:
            try:
                self.sync_single_issue(issue)
            except Exception as e:
                logger.exception(f"Failed to sync issue {issue.key}: {e}")
                # Re-raise for single issue sync so caller knows it failed
                if issue_id:
                    raise

    def _update_resource_backend_id_from_custom_fields(self, issue):
        """
        Update connected resource's backend_id from custom fields if custom field mapping is enabled
        and the issue is connected to a resource.

        If the issue is connected to an Order, updates the Order's marketplace Resource backend_id.
        Otherwise, updates the directly connected resource's backend_id.
        """
        from waldur_mastermind.marketplace import models as marketplace_models

        # Only proceed if custom field mapping is enabled
        if not self._get_config("ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED"):
            return

        # Check if issue is connected to a resource via generic foreign key
        if not (issue.resource_content_type and issue.resource_object_id):
            return

        try:
            # Get the connected object (could be Order or Resource)
            connected_object = issue.resource_content_type.get_object_for_this_type(
                pk=issue.resource_object_id
            )

            # If connected to an Order, get the Order's marketplace Resource
            if isinstance(connected_object, marketplace_models.Order):
                resource = connected_object.resource
                if not resource:
                    logger.debug(
                        f"Order {connected_object} does not have a connected marketplace Resource, skipping update"
                    )
                    return
            else:
                resource = connected_object

            # Check if resource has a backend_id field (most Waldur resources do)
            if not hasattr(resource, "backend_id"):
                logger.debug(
                    f"Resource {resource} does not have backend_id field, skipping update"
                )
                return

            # Get the full Jira issue with all custom fields
            jira_issue = self.get(f"/rest/api/2/issue/{issue.key}")
            fields = jira_issue.get("fields", {})

            # Look for waldur_backend_id custom field
            waldur_backend_id_field = None
            try:
                waldur_backend_id_field = self.get_field_id_by_name("waldur_backend_id")
            except JiraBackendError:
                # Field doesn't exist, try configured fallback field ID
                waldur_backend_id_field = self._get_config(
                    "ATLASSIAN_WALDUR_BACKEND_ID_FIELD"
                )

            if waldur_backend_id_field and waldur_backend_id_field in fields:
                waldur_backend_id_value = fields[waldur_backend_id_field]

                if waldur_backend_id_value and str(waldur_backend_id_value).strip():
                    # Update resource's backend_id if it differs
                    current_backend_id = getattr(resource, "backend_id", "")
                    new_backend_id = str(waldur_backend_id_value).strip()

                    if current_backend_id != new_backend_id:
                        logger.info(
                            f"Updating resource {resource} backend_id from '{current_backend_id}' "
                            f"to '{new_backend_id}' based on Service Desk custom field"
                        )
                        resource.backend_id = new_backend_id
                        resource.save(update_fields=["backend_id"])

                        logger.debug(
                            f"Successfully updated resource backend_id for issue {issue.key}"
                        )
                    else:
                        logger.debug(
                            f"Resource backend_id already matches custom field value: {new_backend_id}"
                        )
                else:
                    logger.debug(
                        f"waldur_backend_id custom field is empty for issue {issue.key}"
                    )
            else:
                logger.debug(
                    f"waldur_backend_id custom field ({waldur_backend_id_field}) not found in issue {issue.key}"
                )

        except Exception as e:
            logger.warning(
                f"Error updating resource backend_id from custom fields for issue {issue.key}: {e}"
            )
