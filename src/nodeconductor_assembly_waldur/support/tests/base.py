import copy

from django.conf import settings
from django.test import override_settings
from rest_framework import test

from . import fixtures


class BaseTest(test.APITransactionTestCase):

    def setUp(self):
        support_backend = 'nodeconductor_assembly_waldur.support.backend.atlassian:SupportBackend'
        settings.WALDUR_SUPPORT['ACTIVE_BACKEND'] = support_backend
        self.fixture = fixtures.SupportFixture()


def override_support_settings(**kwargs):
    support_settings = copy.deepcopy(settings.WALDUR_SUPPORT)
    support_settings.update(kwargs)
    return override_settings(WALDUR_SUPPORT=support_settings)
