"""Media access rules for support attachments.

See :mod:`waldur_core.media.access`. The rule delegates to
``Attachment.objects.filter_for_user``, the same manager ``AttachmentViewSet``
filters with, so these tests pin the delegation rather than restate the rule.
"""

from django.urls import reverse
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support.tests import fixtures


class SupportAttachmentMediaAccessTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.SupportFixture()
        self.attachment = self.fixture.attachment
        media_file = media_models.File.objects.get(name=self.attachment.file.name)
        self.url = reverse("media", kwargs={"uuid": media_file.uuid})

    def get_as(self, user):
        if user is None:
            self.client.logout()
        else:
            self.client.force_authenticate(user)
        return self.client.get(self.url).status_code

    def test_anonymous_user_cannot_download(self):
        self.assertEqual(self.get_as(None), status.HTTP_404_NOT_FOUND)

    def test_unrelated_user_cannot_download(self):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory()), status.HTTP_404_NOT_FOUND
        )

    def test_issue_caller_can_download(self):
        self.assertEqual(self.get_as(self.attachment.issue.caller), status.HTTP_200_OK)

    def test_customer_owner_can_download(self):
        self.assertEqual(self.get_as(self.fixture.owner), status.HTTP_200_OK)

    def test_staff_can_download(self):
        self.assertEqual(self.get_as(self.fixture.staff), status.HTTP_200_OK)

    def test_global_support_cannot_download(self):
        """filter_for_user checks is_staff but not is_support.

        Preserved deliberately: the rule mirrors AttachmentViewSet, and
        widening it here would let media downloads outrun the API.
        """
        self.assertEqual(
            self.get_as(structure_factories.UserFactory(is_support=True)),
            status.HTTP_404_NOT_FOUND,
        )
