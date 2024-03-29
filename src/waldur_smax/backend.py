import functools
import json
import logging
import os
import time
from dataclasses import dataclass, field
from html import unescape

import requests
from constance import config
from requests import exceptions as requests_exceptions
from rest_framework import status

from waldur_core.structure.exceptions import ServiceBackendError

logger = logging.getLogger(__name__)


class SmaxBackendError(ServiceBackendError):
    pass


def reraise_exceptions(func):
    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except requests_exceptions.RequestException as e:
            raise SmaxBackendError(e)

    return wrapped


@dataclass
class User:
    email: str
    name: str
    id: int = None
    upn: str = None
    external_id: str = None


@dataclass
class Issue:
    summary: str
    description: str
    id: str = None
    status: str = ""
    attachments: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    organisation_name: str = None
    project_name: str = None
    resource_name: str = None
    category_id: str = None


@dataclass
class Attachment:
    filename: str
    size: str
    content_type: str
    id: str = None
    backend_issue_id: str = None
    backend_user_id: str = None


@dataclass
class Comment:
    description: str
    backend_user_id: str
    is_public: bool = False
    id: str = None
    backend_issue_id: str = None
    is_system: bool = False


@dataclass
class Category:
    name: str
    id: str = None


class SmaxBackend:
    def __init__(self):
        if not config.SMAX_API_URL.endswith("/"):
            self.api_url = f"{config.SMAX_API_URL}/"
        else:
            self.api_url = f"{config.SMAX_API_URL}"

        self.rest_api = f"{self.api_url}rest/{config.SMAX_TENANT_ID}/"
        self.lwsso_cookie_key = None

    def _smax_response_to_user(self, response):
        entities = response.json()["entities"]
        result = []

        for e in entities:
            result.append(
                User(
                    id=e["properties"]["Id"],
                    email=e["properties"]["Email"],
                    name=e["properties"]["Name"],
                    upn=e["properties"]["Upn"],
                    external_id=e["properties"].get("ExternalId"),
                )
            )

        return result

    def _smax_response_to_categories(self, response):
        entities = response.json()["entities"]
        result = []

        for e in entities:
            result.append(
                Category(
                    id=e["properties"]["Id"],
                    name=e["properties"]["DisplayLabel"],
                )
            )

        return result

    def _smax_response_to_issue(self, response):
        entities = response.json()["entities"]
        result = []

        for e in entities:
            result.append(
                Issue(
                    id=e["properties"]["Id"],
                    summary=e["properties"]["DisplayLabel"],
                    description=e["properties"]["Description"],
                    status=e["properties"]["PhaseId"],
                    attachments=self._smax_entities_to_attachments(
                        json.loads(e["properties"].get("RequestAttachments", "{}")).get(
                            "complexTypeProperties", []
                        ),
                        e["properties"]["Id"],
                    ),
                    comments=self._smax_entities_to_comments(
                        json.loads(e["properties"].get("Comments", "{}")).get(
                            "Comment", []
                        ),
                        e["properties"]["Id"],
                    ),
                    organisation_name=e["properties"].get(
                        config.SMAX_ORGANISATION_FIELD
                    )
                    if config.SMAX_ORGANISATION_FIELD
                    else None,
                    project_name=e["properties"].get(config.SMAX_PROJECT_FIELD)
                    if config.SMAX_PROJECT_FIELD
                    else None,
                    resource_name=e["properties"].get(
                        config.SMAX_AFFECTED_RESOURCE_FIELD
                    )
                    if config.SMAX_AFFECTED_RESOURCE_FIELD
                    else None,
                )
            )

        return result

    def _smax_response_to_comment(self, response):
        entities = response.json()
        result = []

        for e in entities:
            result.append(
                Comment(
                    id=e["Id"],
                    is_public=False if e["PrivacyType"] == "INTERNAL" else True,
                    description=unescape(e["Body"]),
                    backend_user_id=e["Submitter"]["UserId"],
                    is_system=False
                    if e.get("FunctionalPurpose") == "EndUserComment"
                    else True,
                )
            )

        return result

    def _smax_entities_to_comments(self, entities, backend_issue_id):
        result = []

        for e in entities:
            result.append(
                Comment(
                    id=e["CommentId"],
                    is_public=False if e["PrivacyType"] == "INTERNAL" else True,
                    description=unescape(e["CommentBody"]),
                    backend_user_id=e["Submitter"].replace("Person/", ""),
                    backend_issue_id=backend_issue_id,
                )
            )

        return result

    def _smax_entities_to_attachments(self, entities, backend_issue_id):
        result = []

        for e in entities:
            data = e["properties"]
            attachment = Attachment(
                filename=data.get("file_name") or data.get("name", "Unknown"),
                size=data["size"],
                content_type=data["mime_type"],
                id=data["id"],
                backend_issue_id=backend_issue_id,
            )
            creator = data.get("Creator")
            user = (
                self.get_user_by_external_id(creator) or self.get_user(creator)
                if creator
                else None
            )

            if user:
                attachment.backend_user_id = user.id

            result.append(attachment)

        return result

    @reraise_exceptions
    def auth(self):
        response = requests.post(
            f"{self.api_url}auth/authentication-endpoint/"
            f"authenticate/login?TENANTID={config.SMAX_TENANT_ID}",
            json={"login": config.SMAX_LOGIN, "password": config.SMAX_PASSWORD},
        )

        if response.status_code != status.HTTP_200_OK:
            logger.error("Unable to receive session token.")
            raise SmaxBackendError(
                f"Status code {response.status_code}, body {response.text}"
            )
        self.lwsso_cookie_key = response.text
        return self.lwsso_cookie_key

    @reraise_exceptions
    def _get(self, path, params=None):
        params = params or {}
        self.lwsso_cookie_key or self.auth()

        params["TENANTID"] = config.SMAX_TENANT_ID
        headers = {
            "Cookie": f"LWSSO_COOKIE_KEY={self.lwsso_cookie_key}",
            "Content-Type": "application/json",
        }

        url = self.rest_api + path
        return requests.get(
            url=url,
            headers=headers,
        )

    def get(
        self,
        path,
        params=None,
    ):
        response = self._get(path, params)

        if response.status_code == status.HTTP_401_UNAUTHORIZED:
            self.auth()
            response = self._get(path, params)

        if response.status_code >= 400:
            raise SmaxBackendError(
                f"Status code {response.status_code}, body {response.text}, path {path}, params {params}"
            )

        return response

    @reraise_exceptions
    def _request(self, path, method="post", data=None, json=None, **kwargs):
        self.lwsso_cookie_key or self.auth()
        user_headers = kwargs.pop("headers", {})
        headers = {"Cookie": f"LWSSO_COOKIE_KEY={self.lwsso_cookie_key}"}

        if "files" not in kwargs.keys():
            headers["Content-Type"] = "application/json"

        headers.update(user_headers)

        url = self.rest_api + path + f"?TENANTID={config.SMAX_TENANT_ID}"
        response = getattr(requests, method)(
            url=url,
            headers=headers,
            data=data,
            json=json,
            verify=config.SMAX_VERIFY_SSL,
            **kwargs,
        )

        if response.status_code > 299:
            raise SmaxBackendError(
                f"Status code {response.status_code}, body {response.text}. For request to {url}."
            )

        return response

    def post(self, path, data=None, json=None, **kwargs):
        return self._request(path, method="post", data=data, json=json, **kwargs)

    def put(self, path, data=None, json=None, **kwargs):
        return self._request(path, method="put", data=data, json=json, **kwargs)

    def patch(self, path, data=None, json=None, **kwargs):
        return self._request(path, method="patch", data=data, json=json, **kwargs)

    def delete(self, path, **kwargs):
        return self._request(path, method="delete", **kwargs)

    def get_user(self, user_id):
        response = self.get(f"ems/Person/{user_id}?layout=Name,Email,Upn,ExternalId")
        user = self._smax_response_to_user(response)

        if not user:
            return
        else:
            return user[0]

    def get_user_by_external_id(self, external_id):
        response = self.get(
            f"ems/Person?filter=ExternalId+%3D+%27{external_id}%27&layout=Id,Name,Email,Upn"
        )
        user = self._smax_response_to_user(response)

        if not user:
            return
        else:
            return user[0]

    def get_user_by_upn(self, upn):
        response = self.get(f"ems/Person/?layout=FULL_LAYOUT&filter=Upn='{upn}'")
        users = self._smax_response_to_user(response)
        return users[0] if users else None

    def get_all_users(self):
        response = self.get("ems/Person/?layout=FULL_LAYOUT")
        return self._smax_response_to_user(response)

    def get_user_by_email(self, email):
        response = self.get(f"ems/Person/?layout=FULL_LAYOUT&filter=Email='{email}'")
        users = self._smax_response_to_user(response)
        return users[0] if users else None

    def search_user(self, keyword):
        return self.get_user_by_upn(keyword) or self.get_user_by_email(keyword)

    def add_user(self, user: User):
        name = user.name.split()
        first_name = name[0] if len(name) else ""
        last_name = name[1] if len(name) > 1 else ""

        if not first_name or not last_name:
            raise SmaxBackendError(
                "User creation has failed because first or last names have not been passed."
            )

        response = self.post(
            "ems/bulk",
            json={
                "operation": "CREATE",
                "entities": [
                    {
                        "entity_type": "Person",
                        "properties": {
                            "FirstName": first_name,
                            "LastName": last_name,
                            "Email": user.email,
                        },
                    }
                ],
            },
        )
        backend_user = self.wait_result(self.search_user, user.email)

        if not backend_user:
            raise SmaxBackendError(
                f"User creation has failed. Creation response: {response.text}"
            )

        return backend_user

    def get_issue(self, issue_id):
        response = self.get(f"ems/Request?layout=FULL_LAYOUT&filter=Id={issue_id}")
        issues = self._smax_response_to_issue(response)
        return issues[0] if issues else None

    def add_issue(self, user: User, issue: Issue, entity_type="Request"):
        user = self.search_user(user.email) or self.add_user(user)

        properties = {
            "Description": issue.description,
            "DisplayLabel": issue.summary[:140],  # maximal length in SMAX
            "RequestedByPerson": user.id,
            "CreationSource": "CreationSourceExternal",  # to avoid any internal SMAX notification logic
        }

        if config.SMAX_ORGANISATION_FIELD and issue.organisation_name:
            properties[config.SMAX_ORGANISATION_FIELD] = issue.organisation_name

        if config.SMAX_PROJECT_FIELD and issue.project_name:
            properties[config.SMAX_PROJECT_FIELD] = issue.project_name

        if config.SMAX_AFFECTED_RESOURCE_FIELD and issue.resource_name:
            properties[config.SMAX_AFFECTED_RESOURCE_FIELD] = issue.resource_name

        if config.SMAX_CREATION_SOURCE_NAME:
            properties["CreationSourceName_c"] = config.SMAX_CREATION_SOURCE_NAME

        if config.SMAX_REQUESTS_OFFERING:
            properties["RequestsOffering"] = config.SMAX_REQUESTS_OFFERING

        if issue.category_id:
            properties["Category"] = issue.category_id

        response = self.post(
            "ems/bulk",
            json={
                "entities": [
                    {
                        "entity_type": entity_type,
                        "properties": properties,
                    }
                ],
                "operation": "CREATE",
            },
        )
        result = response.json()["entity_result_list"][0]
        if result["completion_status"] == "FAILED":
            raise SmaxBackendError(
                f"Could not create issue. Creation response: {response.json()}"
            )
        issue_id = result["entity"]["properties"]["Id"]
        return self.get_issue(issue_id)

    def get_comments(self, issue_id):
        response = self.get(f"collaboration/comments/Request/{issue_id}")
        return self._smax_response_to_comment(response)

    def add_comment(self, issue_id, comment: Comment):
        payload = {
            "IsSystem": False,
            "Body": comment.description,
            "PrivacyType": "PUBLIC" if comment.is_public else "INTERNAL",
            "Submitter": {"UserId": comment.backend_user_id},
            "ActualInterface": "API",
            "CommentFrom": "User",
        }

        if not comment.is_system:
            payload["FunctionalPurpose"] = "EndUserComment"

        response = self.post(
            f"/collaboration/comments/Request/{issue_id}/",
            json=payload,
        )

        comment.id = response.json()["Id"]
        return comment

    def update_comment(self, issue_id, comment: Comment):
        self.put(
            f"/collaboration/comments/Request/{issue_id}/{comment.id}",
            json={
                "IsSystem": False,
                "Body": comment.description,
                "PrivacyType": "PUBLIC" if comment.is_public else "INTERNAL",
                "Submitter": {"UserId": comment.backend_user_id},
                "ActualInterface": "API",
                "CommentFrom": "User",
            },
        )

        return comment

    def delete_comment(self, issue_id, comment_id):
        self.delete(f"/collaboration/comments/Request/{issue_id}/{comment_id}")
        return

    def attachment_download(self, attachment):
        return self.get(f"/frs/file-list/{attachment.id}").content

    def file_upload(self, file_name, mime_type, file):
        url = "ces/attachment"
        files = [("files[]", (file_name, file, mime_type))]
        response = self.post(url, files=files)
        return response.json()

    def create_attachment(self, issue_id, user_id, file_name, mime_type, file):
        url = "ems/bulk"
        response = self.file_upload(file_name, mime_type, file)
        file_extension = ""

        if len(os.path.splitext(response["name"])) > 1:
            file_extension = os.path.splitext(response["name"])[1].replace(".", "")

        backend_issue = self.get(f"ems/Request?layout=FULL_LAYOUT&filter=Id={issue_id}")
        attachments = json.loads(
            backend_issue.json()["entities"][0]["properties"].get(
                "RequestAttachments", '{"complexTypeProperties":[]}'
            )
        ).get("complexTypeProperties", [])
        backend_user = self.get_user(user_id)
        attachments.append(
            {
                "properties": {
                    "id": response["guid"],
                    "file_name": response["name"],
                    "file_extension": file_extension,
                    "size": response["contentLength"],
                    "mime_type": response["contentType"],
                    "Creator": backend_user.external_id,
                    "LastUpdateTime": response["lastModified"],
                }
            }
        )

        payload = {
            "entities": [
                {
                    "entity_type": "Request",
                    "properties": {
                        "Id": issue_id,
                        "RequestAttachments": json.dumps(
                            {"complexTypeProperties": attachments}
                        ),
                    },
                }
            ],
            "operation": "UPDATE",
        }

        self.post(url, json=payload)
        return Attachment(
            filename=response["name"],
            size=response["contentLength"],
            content_type=response["contentType"],
            id=response["guid"],
            backend_issue_id=issue_id,
            backend_user_id=user_id,
        )

    def delete_attachment(self, issue_id, attachment_id):
        url = "ems/bulk"
        backend_issue = self.get(f"ems/Request?layout=FULL_LAYOUT&filter=Id={issue_id}")
        attachments = json.loads(
            backend_issue.json()["entities"][0]["properties"].get(
                "RequestAttachments", '{"complexTypeProperties":[]}'
            )
        ).get("complexTypeProperties", [])
        attachments = list(
            filter(lambda x: x["properties"]["id"] != attachment_id, attachments)
        )
        payload = {
            "entities": [
                {
                    "entity_type": "Request",
                    "properties": {
                        "Id": issue_id,
                        "RequestAttachments": json.dumps(
                            {"complexTypeProperties": attachments}
                        ),
                    },
                }
            ],
            "operation": "UPDATE",
        }

        self.post(url, json=payload)

    def create_issue_link(self, issue_id, linked_issue_id):
        url = "ems/bulk"
        payload = {
            "relationships": [
                {
                    "name": "RequestCausedByRequest",
                    "firstEndpoint": {"Request": issue_id},
                    "secondEndpoint": {"Request": linked_issue_id},
                }
            ],
            "operation": "CREATE",
        }
        self.post(url, json=payload)

    def add_category_to_issue(self, issue_id, category_id):
        url = "ems/bulk"
        payload = {
            "entities": [
                {
                    "entity_type": "Request",
                    "properties": {
                        "Id": issue_id,
                        "Category": category_id,
                    },
                }
            ],
            "operation": "UPDATE",
        }

        self.post(url, json=payload)

    def get_all_categories(self):
        url = "ems/ITProcessRecordCategory?layout=DisplayLabel"
        response = self.get(url)
        return self._smax_response_to_categories(response)

    def get_category_by_name(self, name):
        url = f"ems/ITProcessRecordCategory?filter=DisplayLabel+%3D+%27{name}%27&layout=DisplayLabel"
        response = self.get(url)
        categories = self._smax_response_to_categories(response)
        return categories[0] if categories else None

    def wait_result(self, func, *args, **kwargs):
        result = None

        for i in range(config.SMAX_TIMES_TO_PULL):
            result = func(*args, **kwargs)
            if result:
                break
            else:
                time.sleep(config.SMAX_SECONDS_TO_WAIT)

        return result
