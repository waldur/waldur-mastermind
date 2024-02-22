from unittest import mock

import pkg_resources
import pytest
from rest_framework import test

from waldur_mastermind.support import models
from waldur_mastermind.support.backend import SupportBackendType, atlassian

from . import fixtures


@pytest.mark.override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.ATLASSIAN,
    ATLASSIAN_ORGANISATION_FIELD="Reporter organization",
    ATLASSIAN_PROJECT_FIELD="Waldur project",
    ATLASSIAN_AFFECTED_RESOURCE_FIELD="Affected resource",
    ATLASSIAN_TEMPLATE_FIELD="Waldur template",
)
class BaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SupportFixture()
        mock_patch = mock.patch("waldur_mastermind.support.backend.get_active_backend")
        self.mock_get_active_backend = mock_patch.start()
        self.mock_get_active_backend().update_is_available.return_value = True
        self.mock_get_active_backend().destroy_is_available.return_value = True
        self.mock_get_active_backend().comment_update_is_available.return_value = True
        self.mock_get_active_backend().comment_destroy_is_available.return_value = True
        self.mock_get_active_backend().attachment_destroy_is_available.return_value = (
            True
        )
        self.mock_get_active_backend().comment_create_is_available.return_value = True
        self.mock_get_active_backend().attachment_create_is_available.return_value = (
            True
        )
        self.mock_get_active_backend().backend_name = None
        self.mock_get_active_backend().pull_support_users = (
            atlassian.ServiceDeskBackend.pull_support_users
        )
        self.mock_get_active_backend().get_users.return_value = [1]
        self.mock_get_active_backend().get_issue_details.return_value = {}
        self.mock_get_active_backend().summary_max_length = 255

        models.IssueStatus.objects.create(
            name="done", type=models.IssueStatus.Types.RESOLVED
        )
        models.IssueStatus.objects.create(
            name="rejected", type=models.IssueStatus.Types.CANCELED
        )

    def tearDown(self):
        mock.patch.stopall()


def load_resource(path):
    return pkg_resources.resource_stream(__name__, path).read().decode()
