from unittest import mock

from constance.test import override_config
from rest_framework import test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests.factories import SshPublicKeyFactory, UserFactory
from waldur_mastermind.marketplace.enums import SUPPORT_OFFERING, ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models as support_models


@override_config(WALDUR_SUPPORT_ENABLED=True)
@mock.patch("waldur_mastermind.support.backend.get_active_backend")
class TestSshKeyChangeIssues(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.user = UserFactory()

    @override_config(ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES=True)
    def test_issue_created_when_ssh_key_added(self, mock_get_active_backend):
        mock_get_active_backend().get_issue_details.return_value = {}
        ssh_key = SshPublicKeyFactory(user=self.user)
        self.assertTrue(
            support_models.Issue.objects.filter(
                summary__icontains="added",
            ).exists()
        )
        issue = support_models.Issue.objects.filter(
            summary__icontains="added",
        ).get()
        self.assertIn(ssh_key.name, issue.summary)

    @override_config(ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES=True)
    def test_issue_created_when_ssh_key_removed(self, mock_get_active_backend):
        mock_get_active_backend().get_issue_details.return_value = {}
        ssh_key = SshPublicKeyFactory(user=self.user)
        support_models.Issue.objects.all().delete()
        ssh_key.delete()
        self.assertTrue(
            support_models.Issue.objects.filter(
                summary__icontains="removed",
            ).exists()
        )

    @override_config(ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES=False)
    def test_issue_not_created_when_setting_disabled(self, mock_get_active_backend):
        mock_get_active_backend().get_issue_details.return_value = {}
        SshPublicKeyFactory(user=self.user)
        self.assertFalse(support_models.Issue.objects.exists())

    @override_config(ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES=True)
    def test_issue_description_contains_ssh_key_details(self, mock_get_active_backend):
        mock_get_active_backend().get_issue_details.return_value = {}
        ssh_key = SshPublicKeyFactory(user=self.user)
        issue = support_models.Issue.objects.filter(
            summary__icontains="added",
        ).get()
        self.assertIn(self.user.username, issue.description)
        self.assertIn(ssh_key.name, issue.description)
        self.assertIn(ssh_key.public_key, issue.description)
        self.assertIn(ssh_key.fingerprint_md5, issue.description)
        self.assertIn(ssh_key.fingerprint_sha256, issue.description)
        self.assertIn(ssh_key.fingerprint_sha512, issue.description)

    @override_config(ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES=True)
    def test_issue_description_contains_support_resources(
        self, mock_get_active_backend
    ):
        mock_get_active_backend().get_issue_details.return_value = {}
        resource = marketplace_factories.ResourceFactory(
            offering=marketplace_factories.OfferingFactory(type=SUPPORT_OFFERING),
            state=ResourceStates.OK,
        )
        resource.project.add_user(self.user, ProjectRole.MEMBER)
        SshPublicKeyFactory(user=self.user)
        issue = support_models.Issue.objects.filter(
            summary__icontains="added",
        ).get()
        self.assertIn(resource.name, issue.description)
        self.assertIn(resource.project.name, issue.description)
        self.assertIn(resource.project.customer.name, issue.description)

    @override_config(ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES=True)
    def test_terminated_resources_not_included(self, mock_get_active_backend):
        mock_get_active_backend().get_issue_details.return_value = {}
        resource = marketplace_factories.ResourceFactory(
            offering=marketplace_factories.OfferingFactory(type=SUPPORT_OFFERING),
            state=ResourceStates.TERMINATED,
        )
        resource.project.add_user(self.user, ProjectRole.MEMBER)
        SshPublicKeyFactory(user=self.user)
        issue = support_models.Issue.objects.filter(
            summary__icontains="added",
        ).get()
        self.assertNotIn(resource.name, issue.description)
        self.assertIn("No active support resources", issue.description)


@override_config(WALDUR_SUPPORT_ENABLED=False)
@mock.patch("waldur_mastermind.support.backend.get_active_backend")
class TestSshKeyChangeIssuesWhenSupportDisabled(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.user = UserFactory()

    @override_config(ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES=True)
    def test_issue_not_created_when_support_disabled(self, mock_get_active_backend):
        mock_get_active_backend().get_issue_details.return_value = {}
        SshPublicKeyFactory(user=self.user)
        self.assertFalse(support_models.Issue.objects.exists())
