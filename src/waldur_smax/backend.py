import functools
import logging
from dataclasses import dataclass

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

    def post(self, path, data=None, json=None, **kwargs):
        self.lwsso_cookie_key or self.auth()

        headers = {
            'Cookie': f'LWSSO_COOKIE_KEY={self.lwsso_cookie_key}',
            'Content-Type': 'application/json',
        }

        url = self.rest_api + path + f'?TENANTID={config.SMAX_TENANT_ID}'
        response = requests.post(
            url=url, headers=headers, data=data, json=json, **kwargs
        )

        if response.status_code > 299:
            raise requests_exceptions.RequestException(
                f"Status code {response.status_code}, body {response.text}"
            )

        return response

    def get_user(self, user_id):
        response = self.get(f'ems/Person/{user_id}?layout=Name,Email,Upn')
        return self._smax_response_to_user(response)

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
        user = self.search_user_by_email(user.email)

        if not user:
            raise requests_exceptions.RequestException("User creation is failed.")

        return user

    def get_issue(self, issue_id):
        response = self.get(
            f'ems/Request?layout=Id,Description,DisplayLabel&filter=Id={issue_id}'
        )
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
