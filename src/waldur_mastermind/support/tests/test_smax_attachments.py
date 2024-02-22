from rest_framework import status

from waldur_core.media.utils import dummy_image
from waldur_mastermind.support import models
from waldur_mastermind.support.backend.smax import SmaxServiceBackend
from waldur_mastermind.support.tests import factories, fixtures, smax_base
from waldur_smax.backend import Attachment, Issue, User


class AttachmentCreateTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.SupportFixture()
        self.support_user = factories.SupportUserFactory(
            user=self.fixture.staff, backend_name=self.fixture.backend_name
        )
        self.url = factories.AttachmentFactory.get_list_url()
        self.file = dummy_image()

        self.smax_attachment = Attachment(
            filename="attach.png",
            size="1024",
            content_type="img/png",
            id="backend_id",
            backend_issue_id=self.fixture.issue.backend_id,
            backend_user_id=self.support_user.backend_id,
        )
        self.mock_smax().create_attachment.return_value = self.smax_attachment

    def _get_valid_payload(self):
        return {
            "issue": factories.IssueFactory.get_url(self.fixture.issue),
            "file": self.file,
        }

    def test_create_attachment(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        response = self.client.post(
            self.url, data=self._get_valid_payload(), format="multipart"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.mock_smax().create_attachment.assert_called_once()
        attachment = models.Attachment.objects.get(uuid=response.data["uuid"])
        self.assertEqual(str(attachment.backend_id), str(self.smax_attachment.id))

    def test_create_attachment_if_issue_is_resolved(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        self.fixture.issue.set_resolved()

        response = self.client.post(
            self.url, data=self._get_valid_payload(), format="multipart"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AttachmentDeleteTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.SupportFixture()
        self.attachment = self.fixture.attachment
        self.url = factories.AttachmentFactory.get_url(self.attachment)
        self.mock_smax().delete_attachment.return_value = None

    def test_delete_attachment(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.mock_smax().delete_attachment.assert_called_once()


class SyncFromSmaxTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.SupportFixture()
        self.issue = self.fixture.issue
        self.support_user = factories.SupportUserFactory(
            user=self.fixture.staff, backend_name=self.fixture.backend_name
        )
        self.smax_attachment = Attachment(
            filename="attach.png",
            size="1024",
            content_type="img/png",
            id="backend_id",
            backend_issue_id=self.fixture.issue.backend_id,
            backend_user_id=self.support_user.backend_id,
        )
        self.smax_issue = Issue(
            1,
            "test",
            "description",
            "RequestStatusReady",
            attachments=[self.smax_attachment],
        )
        self.smax_user = User(
            email=self.support_user.user.email,
            name=self.support_user.user.username,
            id=self.support_user.backend_id,
            upn=self.support_user.uuid.hex,
        )
        self.mock_smax().get_issue.return_value = self.smax_issue
        self.mock_smax().get_user.return_value = self.smax_user
        self.mock_smax().attachment_download.return_value = ""
        self.backend = SmaxServiceBackend()

    def test_create_attachment(self):
        self.assertEqual(self.issue.attachments.count(), 0)
        self.backend.sync_issues()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.attachments.count(), 1)

    def test_delete_attachment(self):
        self.attachment = self.fixture.attachment
        self.assertEqual(self.issue.attachments.count(), 1)
        self.smax_issue.attachments = []
        self.backend.sync_issues()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.attachments.count(), 0)
