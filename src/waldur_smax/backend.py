import functools
import logging
from dataclasses import dataclass
from html import unescape

import requests
from constance import config
from requests import exceptions as requests_exceptions
from rest_framework import status

from waldur_core.structure.exceptions import ServiceBackendError

logger = logging.getLogger(__name__)


class SmaxBackendError(ServiceBackendError):
    pass


def reraise_exceptions(msg=None):
    def wrap(func):
        @functools.wraps(func)
        def wrapped(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except requests_exceptions.RequestException as e:
                raise SmaxBackendError(f'{msg}. {e}')

        return wrapped

    return wrap


@dataclass
class User:
    email: str
    name: str
    id: int = None
    upn: str = None


@dataclass
class Issue:
    id: str
    summary: str
    description: str
    status: str


@dataclass
class Comment:
    description: str
    backend_user_id: str
    is_public: bool = False
    id: str = None


class SmaxBackend:
    def __init__(self):
        if not config.SMAX_API_URL.endswith('/'):
            self.api_url = f'{config.SMAX_API_URL}/'
        else:
            self.api_url = f'{config.SMAX_API_URL}'

        self.rest_api = f'{self.api_url}rest/{config.SMAX_TENANT_ID}/'
        self.lwsso_cookie_key = None

    def _smax_response_to_user(self, response):
        entities = response.json()['entities']
        result = []

        for e in entities:
            result.append(
                User(
                    id=e['properties']['Id'],
                    email=e['properties']['Email'],
                    name=e['properties']['Name'],
                    upn=e['properties']['Upn'],
                )
            )

        return result

    def _smax_response_to_issue(self, response):
        entities = response.json()['entities']
        result = []

        for e in entities:
            result.append(
                Issue(
                    id=e['properties']['Id'],
                    summary=e['properties']['DisplayLabel'],
                    description=e['properties']['Description'],
                    status=e['properties']['Status'],
                )
            )

        return result

    def _smax_response_to_comment(self, response):
        entities = response.json()
        result = []

        for e in entities:
            result.append(
                Comment(
                    id=e['Id'],
                    is_public=False if e['PrivacyType'] == 'INTERNAL' else True,
                    description=unescape(e['Body']),
                    backend_user_id=e['Submitter']['UserId'],
                )
            )

        return result

    def auth(self):
        response = requests.post(
            f'{self.api_url}auth/authentication-endpoint/'
            f'authenticate/login?TENANTID={config.SMAX_TENANT_ID}',
            json={"login": config.SMAX_LOGIN, "password": config.SMAX_PASSWORD},
        )

        if response.status_code != status.HTTP_200_OK:
            logger.error('Unable to receive session token.')
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )
        self.lwsso_cookie_key = response.text
        return self.lwsso_cookie_key

    def _get(self, path, params=None):
        params = params or {}
        self.lwsso_cookie_key or self.auth()

        params['TENANTID'] = config.SMAX_TENANT_ID
        headers = {
            'Cookie': f'LWSSO_COOKIE_KEY={self.lwsso_cookie_key}',
            'Content-Type': 'application/json',
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
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

        return response

    def _request(self, path, method='post', data=None, json=None, **kwargs):
        self.lwsso_cookie_key or self.auth()

        headers = {
            'Cookie': f'LWSSO_COOKIE_KEY={self.lwsso_cookie_key}',
            'Content-Type': 'application/json',
        }

        url = self.rest_api + path + f'?TENANTID={config.SMAX_TENANT_ID}'
        response = getattr(requests, method)(
            url=url, headers=headers, data=data, json=json, **kwargs
        )

        if response.status_code > 299:
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

        return response

    def post(self, path, data=None, json=None, **kwargs):
        return self._request(path, method='post', data=data, json=json, **kwargs)

    def put(self, path, data=None, json=None, **kwargs):
        return self._request(path, method='put', data=data, json=json, **kwargs)

    def delete(self, path, **kwargs):
        return self._request(path, method='delete', **kwargs)

    def get_user(self, user_id):
        response = self.get(f'ems/Person/{user_id}?layout=Name,Email,Upn')
        user = self._smax_response_to_user(response)

        if not user:
            return
        else:
            return user[0]

    def get_all_users(self):
        response = self.get('ems/Person/?layout=Name,Email,Upn')
        return self._smax_response_to_user(response)

    def search_user_by_email(self, email):
        response = self.get(f"ems/Person/?layout=Name,Email,Upn&filter=Email='{email}'")
        users = self._smax_response_to_user(response)
        return users[0] if users else None

    def add_user(self, user: User):
        name = user.name.split()
        first_name = name[0] if len(name) else ''
        last_name = name[1] if len(name) > 1 else ''

        if not first_name or not last_name:
            raise requests_exceptions.RequestException(
                "User creation has failed because first or last names have not been passed."
            )

        self.post(
            'ums/managePersons',
            json={
                "operation": "CREATE_OR_UPDATE",
                "users": [
                    {
                        "properties": {
                            "Upn": user.upn,
                            "FirstName": first_name,
                            "LastName": last_name,
                            "Email": user.email,
                        }
                    }
                ],
            },
        )
        backend_user = self.search_user_by_email(user.email)

        if not backend_user:
            raise requests_exceptions.RequestException("User creation is failed.")

        return backend_user

    def get_issue(self, issue_id):
        response = self.get(f'ems/Request?layout=FULL_LAYOUT&filter=Id={issue_id}')
        issues = self._smax_response_to_issue(response)
        return issues[0] if issues else None

    def add_issue(self, subject, user: User, description='', entity_type='Request'):
        user = self.search_user_by_email(user.email) or self.add_user(user)

        response = self.post(
            'ems/bulk',
            json={
                "entities": [
                    {
                        "entity_type": entity_type,
                        "properties": {
                            "Description": description,
                            "DisplayLabel": subject,
                            "RequestedByPerson": user.id,
                        },
                    }
                ],
                "operation": "CREATE",
            },
        )
        issue_id = response.json()['entity_result_list'][0]['entity']['properties'][
            'Id'
        ]
        return self.get_issue(issue_id)

    def get_comments(self, issue_id):
        response = self.get(f'collaboration/comments/Request/{issue_id}')
        return self._smax_response_to_comment(response)

    def add_comment(self, issue_id, comment: Comment):
        response = self.post(
            f'/collaboration/comments/Request/{issue_id}/',
            json={
                "IsSystem": False,
                "Body": comment.description,
                "PrivacyType": "PUBLIC" if comment.is_public else "INTERNAL",
                "Submitter": {"UserId": comment.backend_user_id},
                "ActualInterface": "API",
                "CommentFrom": "User",
            },
        )

        comment.id = response.json()['Id']
        return comment

    def update_comment(self, issue_id, comment: Comment):
        self.put(
            f'/collaboration/comments/Request/{issue_id}/{comment.id}',
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
        self.delete(f'/collaboration/comments/Request/{issue_id}/{comment_id}')
        return
