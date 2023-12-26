from unittest import mock

from django.conf import settings
from rest_framework import test

from waldur_mastermind.support.backend import SupportBackendType

from . import fixtures


class BaseTest(test.APITransactionTestCase):
    def setUp(self):
        settings.WALDUR_SUPPORT['ENABLED'] = True
        settings.WALDUR_SUPPORT['ACTIVE_BACKEND_TYPE'] = SupportBackendType.SMAX

        settings.WALDUR_SMAX['SMAX_API_URL'] = 'http://localhost:8080'
        settings.WALDUR_SMAX['SMAX_TENANT_ID'] = '123456789'
        settings.WALDUR_SMAX['SMAX_LOGIN'] = 'user@example.com'
        settings.WALDUR_SMAX['SMAX_PASSWORD'] = 'password'

        self.fixture = fixtures.SupportFixture()

        mock_patch = mock.patch('waldur_mastermind.support.backend.smax.SmaxBackend')
        self.mock_smax = mock_patch.start()

    def tearDown(self):
        mock.patch.stopall()
