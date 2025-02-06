from ddt import data, ddt
from rest_framework import test

from waldur_core.media.utils import dummy_image
from waldur_mastermind.support.models import Template, TemplateAttachment
from waldur_mastermind.support.tests import factories, fixtures


class IssueTemplateGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SupportFixture()
        self.url = factories.TemplateFactory.get_list_url()
        self.template = factories.TemplateFactory()

    def test_user_can_get_template(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], self.template.name)


@ddt
class IssueTemplateCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SupportFixture()
        self.url = factories.TemplateFactory.get_list_url()

    def test_user_can_create_template(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, data=self._get_valid_payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["name"], self._get_valid_payload()["name"])

    @data("admin", "owner")
    def test_user_cannot_create_template(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, data=self._get_valid_payload())
        self.assertEqual(response.status_code, 403)

    def _get_valid_payload(self):
        return {
            "name": "test_template",
            "description": "test_description",
            "type": Template.IssueTypes.INCIDENT,
        }


class IssueTemplateAttachmentTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SupportFixture()
        self.template = factories.TemplateFactory()
        self.attachment = factories.AttachmentFactory()
        self.attach_url = factories.TemplateFactory.get_url(
            self.template, "create_attachments"
        )
        self.detach_url = factories.TemplateFactory.get_url(
            self.template, "delete_attachments"
        )

    def test_user_can_add_attachments(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.attach_url, data=self._get_valid_payload(), format="multipart"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.template.attachments.count(), 2)

    def test_user_can_remove_attachment(self):
        self.client.force_authenticate(self.fixture.staff)
        attachment = TemplateAttachment.objects.create(
            template=self.template, file=self.attachment.file
        )
        self.template.attachments.add(attachment)
        self.assertEqual(self.template.attachments.count(), 1)
        response = self.client.post(
            self.detach_url, data={"attachment_ids": [attachment.uuid.hex]}
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.template.attachments.count(), 0)

    def _get_valid_payload(self):
        return {
            "attachments": [dummy_image(), self.attachment.file],
        }

    def test_create_attachment_with_invalid_payload(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.attach_url, data={}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "No attachments provided.")
        response = self.client.post(
            self.attach_url, data={"attachments": ["invalid"]}, format="multipart"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.template.attachments.count(), 0)
