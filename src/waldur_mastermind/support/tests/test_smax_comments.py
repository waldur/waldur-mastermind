from unittest import mock

from dbtemplates.models import Template
from rest_framework import status

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models, tasks
from waldur_mastermind.support.backend.smax import SmaxServiceBackend
from waldur_mastermind.support.tests import factories, fixtures, smax_base
from waldur_smax.backend import Comment, Issue


class CommentCreateTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.SupportFixture()
        self.support_user = factories.SupportUserFactory(
            user=self.fixture.staff, backend_name=self.fixture.backend_name
        )
        self.url = factories.IssueFactory.get_url(self.fixture.issue, "comment")

        self.smax_comment = Comment(
            description="comment text",
            backend_user_id=self.support_user.backend_id,
            is_public=True,
            id="abc123",
        )
        self.mock_smax().add_comment.return_value = self.smax_comment

    def _get_valid_payload(self, **additional):
        payload = {
            "description": "comment text",
        }
        payload.update(additional)
        return payload

    def test_create_comment(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.mock_smax().add_comment.assert_called_once()
        comment = models.Comment.objects.get(uuid=response.data["uuid"])
        self.assertEqual(str(comment.backend_id), str(self.smax_comment.id))

    def test_create_comment_if_issue_is_resolved(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        self.fixture.issue.set_resolved()

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CommentUpdateTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.SupportFixture()
        self.fixture.comment.state = models.Comment.States.OK
        self.fixture.comment.save()
        self.support_user = factories.SupportUserFactory(
            user=self.fixture.staff, backend_name=self.fixture.backend_name
        )
        self.url = factories.CommentFactory.get_url(self.fixture.comment)

    def _get_valid_payload(self, **additional):
        payload = {
            "description": "new comment text",
        }
        payload.update(additional)
        return payload

    def test_update_issue(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        response = self.client.patch(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.mock_smax().update_comment.assert_called_once()


class CommentDeleteTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.SupportFixture()
        self.fixture.comment.state = models.Comment.States.OK
        self.fixture.comment.save()
        self.url = factories.CommentFactory.get_url(self.fixture.comment)

    def test_update_issue(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.mock_smax().delete_comment.assert_called_once()


class SyncFromSmaxTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.SupportFixture()
        self.issue = self.fixture.issue
        self.comment = self.fixture.comment
        self.smax_comment = Comment(
            description="new description",
            backend_user_id=self.comment.author,
            is_public=False,
            id=self.comment.backend_id,
        )
        self.smax_issue = Issue(
            1, "test", "description", "RequestStatusReady", comments=[self.smax_comment]
        )
        self.mock_smax().get_issue.return_value = self.smax_issue
        self.backend = SmaxServiceBackend()

    def test_sync_comment(self):
        self.backend.sync_issues()
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.description, self.smax_comment.description)
        self.assertEqual(self.comment.is_public, self.smax_comment.is_public)


class ConfirmationCommentTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.SupportFixture()
        self.issue = self.fixture.issue
        self.backend = SmaxServiceBackend()

    def test_create_confirmation_comment_if_template_exists(self):
        factories.TemplateConfirmationCommentFactory(template="template")
        self.backend.create_confirmation_comment(self.issue)
        self.mock_smax().add_comment.assert_called_once()

    def test_not_create_confirmation_comment_if_template_does_not_exist(self):
        self.backend.create_confirmation_comment(self.issue)
        self.mock_smax().add_comment.assert_not_called()


@mock.patch("waldur_mastermind.support.tasks.core_utils.send_mail")
class CommentNotificationTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.SupportFixture()
        self.comment = self.fixture.comment
        self.comment.description = "<p>message</p>"
        self.comment.save()

    def test_add_comment_notification(self, mock_send_mail):
        structure_factories.NotificationFactory(
            key="support.notification_comment_added", enabled=True
        )
        Template.objects.create(
            name="support/notification_comment_added.html",
            content="{{ description|safe }}",
        )
        Template.objects.create(
            name="support/notification_comment_added.txt", content="{{ description }}"
        )
        Template.objects.create(
            name="support/notification_comment_added_subject.txt",
            content="New comment.",
        )
        serialized_comment = core_utils.serialize_instance(self.comment)
        tasks.send_comment_added_notification(serialized_comment)
        mock_send_mail.assert_called_once_with(
            "New comment.",
            "message\n\n",
            [self.fixture.issue.caller.email],
            html_message="<p>message</p>",
        )

    def test_update_comment_notification(self, mock_send_mail):
        structure_factories.NotificationFactory(
            key="support.notification_comment_updated", enabled=True
        )
        Template.objects.create(
            name="support/notification_comment_updated.html",
            content="New: {{ description|safe }}, old: {{ old_description|safe }}",
        )
        Template.objects.create(
            name="support/notification_comment_updated.txt",
            content="New: {{ description }}, old: {{ old_description|safe }}",
        )
        Template.objects.create(
            name="support/notification_comment_updated_subject.txt",
            content="Update comment.",
        )
        serialized_comment = core_utils.serialize_instance(self.comment)
        old_description = "<p>old message</p>"
        tasks.send_comment_updated_notification(serialized_comment, old_description)
        mock_send_mail.assert_called_once_with(
            "Update comment.",
            "New: message\n\n, old: old message\n\n",
            [self.fixture.issue.caller.email],
            html_message="New: <p>message</p>, old: <p>old message</p>",
        )
