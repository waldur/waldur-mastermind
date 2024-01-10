from unittest import mock

import pytest
from rest_framework import test

from waldur_mastermind.support.backend import SupportBackendType
from waldur_zammad.backend import User

from . import fixtures


@pytest.mark.override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.ZAMMAD,
    ZAMMAD_API_URL="http://localhost:8080",
    ZAMMAD_TOKEN="token",
)
class BaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SupportFixture()

        mock_patch = mock.patch(
            "waldur_mastermind.support.backend.zammad.ZammadBackend"
        )
        self.mock_zammad = mock_patch.start()
        self.mock_zammad().get_user_by_login.return_value = User(
            1, "test@test.com", "test", "test", "test", "test", True
        )

    def tearDown(self):
        mock.patch.stopall()
