from unittest import mock

from django.conf import settings
from rest_framework import test

from waldur_zammad.backend import User

from . import fixtures


class BaseTest(test.APITransactionTestCase):
    def setUp(self):
        settings.WALDUR_SUPPORT['ENABLED'] = True
        settings.WALDUR_SUPPORT[
            'ACTIVE_BACKEND'
        ] = 'waldur_mastermind.support.backend.zammad:ZammadServiceBackend'
        settings.WALDUR_ZAMMAD['ZAMMAD_API_URL'] = 'http://localhost:8080'
        settings.WALDUR_ZAMMAD['ZAMMAD_TOKEN'] = 'token'

        self.fixture = fixtures.SupportFixture()

        mock_patch = mock.patch(
            'waldur_mastermind.support.backend.zammad.ZammadBackend'
        )
        self.mock_zammad = mock_patch.start()
        self.mock_zammad().get_user_by_login.return_value = User(
            1, 'test@test.com', 'test', 'test', 'test', 'test', True
        )

    def tearDown(self):
        mock.patch.stopall()
