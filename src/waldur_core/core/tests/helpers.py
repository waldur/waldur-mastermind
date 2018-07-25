from __future__ import unicode_literals

import copy

from django.conf import settings
from django.test.utils import override_settings
from rest_framework import test, status
import six


class PermissionsTest(test.APITransactionTestCase):
    """
    Abstract class for permissions tests.

    Methods `get_urls_configs`, `get_users_with_permission`,
    `get_users_without_permissions` have to be overridden.

    Logical example:

    class ExamplePermissionsTest(PermissionsTest):

        def get_users_with_permission(self, url, method):
            if is_unreachable(url):
                # no one can has access to unreachable url
                return []
            return [user_with_permission]

        def get_users_without_permissions(self, url, method):
            if is_unreachable(url):
                # everybody does not have access to to unreachable url
                return [user_with_permission, user_without_permission]
            return [user_without_permission]

        def get_urls_configs(self):
            yield {'url': 'http://testserver/some/url, 'method': 'GET'}
            yield {'url': 'http://testserver/some/unreachable/url', 'method': 'POST'}
            ...
    """

    def get_urls_configs(self):
        """
        Return list or generator of url configs.

        Each url config is dictionary with such keys:
         - url: url itself
         - method: request method
         - data: data which will be sent in request
        url config example:
        {
            'url': 'http://testserver/api/backup/',
            'method': 'POST',
            'data': {'backup_source': 'backup/source/url'}
        }
        """
        raise NotImplementedError()

    def get_users_with_permission(self, url, method):
        """
        Return list of users which can access given url with given method
        """
        raise NotImplementedError()

    def get_users_without_permissions(self, url, method):
        """
        Return list of users which can not access given url with given method
        """
        raise NotImplementedError()

    def test_permissions(self):
        """
        Go through all url configs ands checks that user with permissions
        can request them and users without - can't
        """
        for conf in self.get_urls_configs():
            url, method = conf['url'], conf['method']
            data = conf['data'] if 'data' in conf else {}

            for user in self.get_users_with_permission(url, method):
                self.client.force_authenticate(user=user)
                response = getattr(self.client, method.lower())(url, data=data)
                self.assertFalse(
                    response.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                    'Error. User %s can not reach url: %s (method:%s). (Response status code %s, data %s)'
                    % (user, url, method, response.status_code, response.data))

            for user in self.get_users_without_permissions(url, method):
                self.client.force_authenticate(user=user)
                response = getattr(self.client, method.lower())(url, data=data)
                unreachable_statuses = (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT)
                self.assertTrue(
                    response.status_code in unreachable_statuses,
                    'Error. User %s can reach url: %s (method:%s). (Response status code %s, data %s)'
                    % (user, url, method, response.status_code, response.data))


class ListPermissionsTest(test.APITransactionTestCase):
    """
    Abstract class that tests what objects user receive in list.

    Method `get_users_and_expected_results` has to be overridden.
    Method `get_url` have to be defined.
    """

    def get_url(self):
        return None

    def get_users_and_expected_results(self):
        """
        Return list or generator of dictionaries with such keys:
         - user - user which we want to test
         - expected_results - list of dictionaries with fields which user has
                              to receive as answer from server
        """
        pass

    def test_list_permissions(self):
        for user_and_expected_result in self.get_users_and_expected_results():
            user = user_and_expected_result['user']
            expected_results = user_and_expected_result['expected_results']

            self.client.force_authenticate(user=user)
            response = self.client.get(self.get_url())
            self.assertEqual(
                len(expected_results), len(response.data),
                'User %s receive wrong number of objects. Expected: %s, received %s'
                % (user, len(expected_results), len(response.data)))
            for actual, expected in zip(response.data, expected_results):
                for key, value in six.iteritems(expected):
                    self.assertEqual(actual[key], value)


def override_waldur_core_settings(**kwargs):
    waldur_settings = copy.deepcopy(settings.WALDUR_CORE)
    waldur_settings.update(kwargs)
    return override_settings(WALDUR_CORE=waldur_settings)
