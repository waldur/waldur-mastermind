import logging

import requests

logger = logging.getLogger(__name__)


class KeycloakException(Exception):
    pass


class KeycloakClient:
    def __init__(self, base_url, realm, client_id, client_secret, username, password):
        self.base_url = base_url
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self.username = username
        self.password = password

    def get_access_token(self):
        token_url = f'{self.base_url}/realms/{self.realm}/protocol/openid-connect/token'
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'username': self.username,
            'password': self.password,
            'grant_type': 'password',
        }
        try:
            response = requests.post(token_url, data=data)
        except requests.exceptions.RequestException as e:
            logger.warning('Unable to send authentication request. Error is %s', e)
            raise KeycloakException('Unable to send authentication request.')
        if response.ok:
            return response.json()['access_token']
        else:
            raise KeycloakException('Unable to parse access token.')

    def _request(self, method, endpoint, json=None):
        access_token = self.get_access_token()
        if not endpoint.startswith(self.base_url):
            url = f'{self.base_url}/admin/realms/{self.realm}/{endpoint}'
        headers = {'Authorization': 'Bearer ' + access_token}
        try:
            response = requests.request(method, url, json=json, headers=headers)
        except requests.RequestException as e:
            raise KeycloakException(e)
        else:
            return response

    def _parse_response(self, response):
        status_code = response.status_code
        if status_code in (
            requests.codes.ok,
            requests.codes.accepted,
            requests.codes.no_content,
        ):
            if response.content:
                return response.json()
        else:
            raise KeycloakException(response.content)

    def _get_all(self, endpoint):
        response = self._request('get', endpoint)

        if response.status_code != 200:
            raise KeycloakException(response.content)
        result = response.json()
        if 'Link' not in response.headers:
            return result
        while 'next' in response.headers['Link']:
            next_url = response.headers['Link'].split('; ')[0][1:-1]
            response = self._request('get', next_url)

            if response.status_code != 200:
                raise KeycloakException(response.content)

            result += response.json()

        return result

    def _post(self, endpoint, **kwargs):
        response = self._request('post', endpoint, **kwargs)
        if response.status_code == requests.codes.created:
            # Parse created object UUID
            return response.headers['location'].split('/')[-1]
        else:
            return self._parse_response(response)

    def _get(self, endpoint, **kwargs):
        response = self._request('get', endpoint, **kwargs)
        return self._parse_response(response)

    def _put(self, endpoint, **kwargs):
        response = self._request('put', endpoint, **kwargs)
        return self._parse_response(response)

    def _delete(self, endpoint, **kwargs):
        response = self._request('delete', endpoint, **kwargs)
        return self._parse_response(response)

    def get_users(self):
        return self._get_all('users')

    def get_user_groups(self, user_id):
        return self._get_all(f'users/{user_id}/groups')

    def add_user_to_group(self, user_id, group_id):
        return self._put(f'users/{user_id}/groups/{group_id}')

    def delete_user_from_group(self, user_id, group_id):
        return self._delete(f'users/{user_id}/groups/{group_id}')

    def get_groups(self):
        return self._get_all('groups')

    def create_group(self, name):
        return self._post('groups', json={'name': name})

    def update_group(self, group_id, name):
        return self._put(f'groups/{group_id}', json={'name': name})

    def delete_group(self, group_id):
        return self._delete(f'groups/{group_id}')

    def create_child_group(self, group_id, name):
        return self._post(f'groups/{group_id}/children', json={'name': name})

    def get_group_users(self, group_id):
        return self._get_all(f'groups/{group_id}/members')
