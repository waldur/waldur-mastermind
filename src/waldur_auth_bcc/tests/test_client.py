# coding: utf-8
from __future__ import unicode_literals

import json

from django.test import TestCase
from django.test import override_settings
import responses
from rest_framework import status

from waldur_core.structure.tests.factories import UserFactory


class ClientTest(TestCase):
    URL = '/api-auth/bcc/user-details/'

    @override_settings(WALDUR_AUTH_BCC={'ENABLED': False})
    def test_feature_is_disabled(self):
        self.client.force_login(UserFactory())
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(WALDUR_AUTH_BCC={'ENABLED': True})
    def test_required_params_are_validated(self):
        self.client.force_login(UserFactory())
        response = self.client.get(self.URL, {'nid': 'nid'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @responses.activate
    @override_settings(WALDUR_AUTH_BCC={
        'ENABLED': True,
        'BASE_API_URL': 'http://example.com/',
        'USERNAME': 'admin',
        'PASSWORD': 'secret',
    })
    def test_error_is_handled(self):
        self.client.force_login(UserFactory())
        responses.add(responses.GET, 'http://example.com/', json={'error': 'Invalid request'})
        response = self.client.get(self.URL, {'nid': 'nid', 'vno': 'vno'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()['details'], 'Invalid request')

    @responses.activate
    @override_settings(WALDUR_AUTH_BCC={
        'ENABLED': True,
        'BASE_API_URL': 'http://example.com/',
        'USERNAME': 'admin',
        'PASSWORD': 'secret',
    })
    def test_empty_response_is_rendered_as_error(self):
        self.client.force_login(UserFactory())
        responses.add(responses.GET, 'http://example.com/', json={
            'nameen': '',
            'namebn': '',
            'desig': '',
            'office': '',
        })
        response = self.client.get(self.URL, {'nid': 'nid', 'vno': 'vno'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @responses.activate
    @override_settings(WALDUR_AUTH_BCC={
        'ENABLED': True,
        'BASE_API_URL': 'http://example.com/',
        'USERNAME': 'admin',
        'PASSWORD': 'secret',
    })
    def test_user_details_are_rendered(self):
        self.client.force_login(UserFactory())
        responses.add(responses.GET, 'http://example.com/', json={
            'nameen': 'User',
            'namebn': 'User',
            'desig': 'সহকারী সচিব',
            'office': 'Secretariat',
        })
        response = self.client.get(self.URL, {'nid': 'nid', 'vno': 'vno'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(json.loads(response.content), {
            'name': 'User',
            'native_name': 'User',
            'job_title': 'সহকারী সচিব',
            'organization': 'Secretariat',
        })
