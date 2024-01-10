from unittest import mock

import pytest
from rest_framework import test

from waldur_mastermind.support.backend import SupportBackendType

from . import fixtures


@pytest.mark.override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.SMAX,
    SMAX_API_URL="http://localhost:8080",
    SMAX_TENANT_ID="123456789",
    SMAX_LOGIN="user@example.com",
    SMAX_PASSWORD="password",
)
class BaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SupportFixture()

        mock_patch = mock.patch("waldur_mastermind.support.backend.smax.SmaxBackend")
        self.mock_smax = mock_patch.start()

    def tearDown(self):
        mock.patch.stopall()
