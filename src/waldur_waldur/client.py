import logging

import requests

from .exceptions import NotFound, WaldurClientException

logger = logging.getLogger(__name__)


class WaldurClient:
    def __init__(self, url, token):
        self.url = url
        self.request_headers = {'Authorization': 'Token %s' % token}

    def _request(self, method, endpoint=None, url=None, json=None, **kwargs):
        if not url:
            if endpoint:
                url = '%s/%s' % (self.url, endpoint)
            else:
                url = self.url

        try:
            response = requests.request(
                method, url, json=json, headers=self.request_headers, **kwargs
            )
        except requests.RequestException as e:
            raise WaldurClientException(e)

        status_code = response.status_code

        if status_code in (
            requests.codes.ok,
            requests.codes.created,
            requests.codes.accepted,
            requests.codes.no_content,
        ):
            return response
        elif status_code == requests.codes.not_found:
            raise NotFound(response.content.decode('utf-8'))
        else:
            raise WaldurClientException(response.content.decode('utf-8'))

    def _get(self, endpoint, **kwargs):
        return self._request('get', endpoint, **kwargs)

    def _get_paginated_data(self, endpoint, **kwargs):
        response = self._request('get', endpoint=endpoint, **kwargs)

        data = response.json()
        while 'next' in response.headers['Link']:
            if 'prev' in response.headers['Link']:
                next_url = response.headers['Link'].split(', ')[2].split('; ')[0][1:-1]
            else:  # First page case
                next_url = response.headers['Link'].split(', ')[1].split('; ')[0][1:-1]
            response = self._request('get', url=next_url, **kwargs)
            data += response.json()

        return data

    def ping(self):
        self._get('api')

    def list_public_offerings(self, customer_uuid):
        return self._get_paginated_data(
            'marketplace-offerings/?shared=true&customer_uuid=%s' % customer_uuid
        )

    def list_remote_customers(self):
        return self._get_paginated_data('customers')

    def get_public_offering(self, offering_uuid):
        return self._get('marketplace-offerings/%s' % offering_uuid)

    def get_customer(self, customer_uuid):
        return self._get('customers/%s' % customer_uuid)
