import copy

from django.conf import settings
from django.test import override_settings
from rest_framework import test

from . import fixtures


class BaseTest(test.APITransactionTestCase):

    def setUp(self):
        support_backend = 'nodeconductor_assembly_waldur.support.backend.atlassian:SupportBackend'
        settings.WALDUR_SUPPORT['ENABLED'] = True
        settings.WALDUR_SUPPORT['ACTIVE_BACKEND'] = support_backend
        self.fixture = fixtures.SupportFixture()


def override_support_settings(**kwargs):
    support_settings = copy.deepcopy(settings.WALDUR_SUPPORT)
    support_settings.update(kwargs)
    return override_settings(WALDUR_SUPPORT=support_settings)


def override_offerings():
    return override_support_settings(OFFERINGS={
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
    })
