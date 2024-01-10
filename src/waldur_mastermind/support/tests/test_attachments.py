from ddt import data, ddt
from rest_framework import status

from waldur_core.media.utils import dummy_image
from waldur_mastermind.support import models
from waldur_mastermind.support.tests import base, factories


class AttachmentTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.issue = self.fixture.issue
        self.fixture.caller = self.fixture.issue.caller
        self.attachment = factories.AttachmentFactory(issue=self.issue)
        self.file_path = self.attachment.file.file.name
        self.url = factories.AttachmentFactory.get_url(self.attachment)


@ddt
class AttachmentGetTest(AttachmentTest):
    @data("staff", "owner", "caller")
    def test_can_get_attachment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("admin", "manager")
    def test_cannot_get_attachment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class AttachmentCreateTest(AttachmentTest):
    def setUp(self):
        super().setUp()
        self.url = factories.AttachmentFactory.get_list_url()
        self.file = dummy_image()

    @data("staff", "owner", "caller")
    def test_can_add_attachment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.url, data=self._get_valid_payload(), format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        uuid = response.data["uuid"]
        self.assertTrue(models.Attachment.objects.filter(uuid=uuid).exists())
        self.add_attachment = models.Attachment.objects.get(uuid=uuid)

    @data(
        "admin",
        "manager",
    )
    def test_cannot_add_attachment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.url, data=self._get_valid_payload(), format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def _get_valid_payload(self):
        return {
            "issue": factories.IssueFactory.get_url(self.fixture.issue),
            "file": self.file,
        }


@ddt
class AttachmentDeleteTest(AttachmentTest):
    @data("admin", "manager")
    def test_cannot_delete_attachment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("staff", "owner", "caller")
    def test_can_delete_attachment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
