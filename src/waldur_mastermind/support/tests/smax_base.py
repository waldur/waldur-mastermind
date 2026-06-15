from unittest import mock

import pytest
from rest_framework import test

from waldur_mastermind.support import models
from waldur_mastermind.support.backend import SupportBackendType
from waldur_mastermind.support.backend.smax import SmaxServiceBackend

from . import fixtures

SMAX_WEBHOOK_TEST_SECRET = "smax-test-secret"  # noqa: S105


@pytest.mark.override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.SMAX,
    SMAX_API_URL="http://localhost:8080",
    SMAX_TENANT_ID="123456789",
    SMAX_LOGIN="user@example.com",
    SMAX_PASSWORD="password",
    SMAX_WEBHOOK_SHARED_SECRET=SMAX_WEBHOOK_TEST_SECRET,
)
class BaseTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.SupportFixture()

        mock_patch = mock.patch("waldur_mastermind.support.backend.smax.SmaxBackend")
        self.mock_smax = mock_patch.start()

        # Mock get_active_backend to return a real SmaxServiceBackend instance.
        # This prevents failures from mock leaks when other tests that mock
        # get_active_backend run before SMAX tests in the same CI shard.
        # The SmaxServiceBackend uses the already-mocked SmaxBackend as its
        # internal manager, so all self.mock_smax() assertions still work.
        backend_mock_patch = mock.patch(
            "waldur_mastermind.support.backend.get_active_backend",
            return_value=SmaxServiceBackend(),
        )
        backend_mock_patch.start()

        models.IssueStatus.objects.create(
            name="done", type=models.IssueStatus.Types.RESOLVED
        )
        models.IssueStatus.objects.create(
            name="rejected", type=models.IssueStatus.Types.CANCELED
        )

    def tearDown(self):
        mock.patch.stopall()
