from rest_framework import status

from waldur_mastermind.support import models
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

    def test_create_issue(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.mock_smax().add_comment.assert_called_once()
        comment = models.Comment.objects.get(uuid=response.data["uuid"])
        self.assertEqual(str(comment.backend_id), str(self.smax_comment.id))


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
