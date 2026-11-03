"""Media access rules for support attachments.

See :mod:`waldur_core.media.access`. An attachment is downloaded by whoever
sees its issue, by the same filter ``AttachmentViewSet`` narrows with, so these
tests pin that the two agree rather than restate the issue rule.
"""

from ddt import data, ddt
from django.urls import reverse
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support.tests import factories, fixtures


@ddt
class SupportAttachmentMediaAccessTest(test.APITestCase):
    def setUp(self):
        # The issue is raised against a project, by a caller holding no role there.
        self.fixture = fixtures.SupportFixture()
        self.attachment = self.fixture.attachment
        self.url = self.media_url(self.attachment)

    def media_url(self, attachment):
        media_file = media_models.File.objects.get(name=attachment.file.name)
        return reverse("media", kwargs={"uuid": media_file.uuid})

    def get_as(self, user, url=None):
        if user is None:
            self.client.logout()
        else:
            self.client.force_authenticate(user)
        return self.client.get(url or self.url).status_code

    def test_anonymous_user_cannot_download(self):
        self.assertEqual(self.get_as(None), status.HTTP_404_NOT_FOUND)

    def test_unrelated_user_cannot_download(self):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory()), status.HTTP_404_NOT_FOUND
        )

    @data("staff", "global_support", "owner", "admin", "manager", "member")
    def test_whoever_sees_the_issue_can_download(self, user):
        self.assertEqual(self.get_as(getattr(self.fixture, user)), status.HTTP_200_OK)

    def test_caller_without_a_role_in_the_issues_project_cannot_download(self):
        self.assertEqual(
            self.get_as(self.attachment.issue.caller), status.HTTP_404_NOT_FOUND
        )

    def test_caller_of_an_unscoped_issue_can_download(self):
        issue = factories.IssueFactory(project=None, customer=None)
        attachment = factories.AttachmentFactory(issue=issue)
        self.assertEqual(
            self.get_as(issue.caller, self.media_url(attachment)), status.HTTP_200_OK
        )
