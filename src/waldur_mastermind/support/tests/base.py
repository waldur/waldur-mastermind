import copy
from unittest import mock

import pkg_resources
from django.conf import settings
from django.test import override_settings
from rest_framework import test

from waldur_mastermind.support.backend import atlassian

from . import fixtures


class BaseTest(test.APITransactionTestCase):
    def setUp(self):
        support_backend = (
            'waldur_mastermind.support.backend.atlassian:ServiceDeskBackend'
        )
        settings.WALDUR_SUPPORT['ENABLED'] = True
        settings.WALDUR_SUPPORT['ACTIVE_BACKEND'] = support_backend
        settings.WALDUR_SUPPORT['ISSUE']['organisation_field'] = 'Reporter organization'
        settings.WALDUR_SUPPORT['ISSUE']['project_field'] = 'Waldur project'
        settings.WALDUR_SUPPORT['ISSUE'][
            'affected_resource_field'
        ] = 'Affected resource'
        settings.WALDUR_SUPPORT['ISSUE']['template_field'] = 'Waldur template'
        self.fixture = fixtures.SupportFixture()
        mock_patch = mock.patch('waldur_mastermind.support.backend.get_active_backend')
        self.mock_get_active_backend = mock_patch.start()
        self.mock_get_active_backend().update_is_available.return_value = True
        self.mock_get_active_backend().destroy_is_available.return_value = True
        self.mock_get_active_backend().comment_update_is_available.return_value = True
        self.mock_get_active_backend().comment_destroy_is_available.return_value = True
        self.mock_get_active_backend().backend_name = None
        self.mock_get_active_backend().pull_support_users = (
            atlassian.ServiceDeskBackend.pull_support_users
        )
        self.mock_get_active_backend().get_users.return_value = [1]

    def tearDown(self):
        mock.patch.stopall()


def override_support_settings(**kwargs):
    support_settings = copy.deepcopy(settings.WALDUR_SUPPORT)
    support_settings.update(kwargs)
    return override_settings(WALDUR_SUPPORT=support_settings)


def override_offerings():
    return override_support_settings(
        OFFERINGS={
            'custom_vpc': {
                'label': 'Custom VPC',
                'order': ['storage', 'ram', 'cpu_count'],
                'options': {
                    'storage': {
                        'type': 'integer',
                        'label': 'Max storage, GB',
                        'help_text': 'VPC storage limit in GB.',
                    },
                    'ram': {
                        'type': 'integer',
                        'label': 'Max RAM, GB',
                        'help_text': 'VPC RAM limit in GB.',
                    },
                    'cpu_count': {
                        'default': 93,
                        'type': 'integer',
                        'label': 'Max vCPU',
                        'help_text': 'VPC CPU count limit.',
                    },
                },
            },
        }
    )


def load_resource(path):
    return pkg_resources.resource_stream(__name__, path).read().decode()
