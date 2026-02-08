from unittest import mock

from constance.test.unittest import override_config
from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models
from waldur_mastermind.support.backend import SupportBackendType
from waldur_mastermind.support.backend.atlassian import (
    CommentSynchronizer,
    ServiceDeskBackend,
)
from waldur_mastermind.support.tests import factories


@ddt
class IssueSyncAPITest(test.APITestCase):
    """Tests for the issue sync API endpoint."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.owner = structure_factories.UserFactory()
        self.issue = factories.IssueFactory(
            backend_name=SupportBackendType.ATLASSIAN,
        )
        self.url = factories.IssueFactory.get_url(self.issue, action="sync")

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    @data("staff", "support")
    def test_staff_or_support_can_sync_issue(self, user_type):
        user = self.staff if user_type == "staff" else self.support
        self.client.force_authenticate(user)

        with mock.patch(
            "waldur_mastermind.support.views.backend.get_active_backend"
        ) as mock_backend:
            mock_backend.return_value.sync_issues = mock.MagicMock()
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_backend.return_value.sync_issues.assert_called_once_with(self.issue.id)

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_regular_user_cannot_sync_issue(self):
        self.client.force_authenticate(self.owner)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_anonymous_user_cannot_sync_issue(self):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_sync_error_raises_exception(self):
        """When sync fails, exception is raised (will be 500 in production)."""
        self.client.force_authenticate(self.staff)

        with mock.patch(
            "waldur_mastermind.support.views.backend.get_active_backend"
        ) as mock_backend:
            mock_backend.return_value.sync_issues.side_effect = Exception("Sync failed")
            with self.assertRaises(Exception) as ctx:
                self.client.post(self.url)

        self.assertEqual(str(ctx.exception), "Sync failed")


class SyncSingleIssueTest(test.APITestCase):
    """Tests for the sync_single_issue method."""

    def setUp(self):
        self.issue = factories.IssueFactory(
            backend_name=SupportBackendType.ATLASSIAN,
            backend_id="TEST-123",
            key="TEST-123",
        )

    @mock.patch.object(ServiceDeskBackend, "update_issue_from_jira")
    @mock.patch.object(ServiceDeskBackend, "update_attachment_from_jira")
    @mock.patch.object(ServiceDeskBackend, "sync_comments_from_jira")
    def test_sync_single_issue_calls_all_sync_methods(
        self, mock_sync_comments, mock_sync_attachments, mock_sync_issue
    ):
        backend = ServiceDeskBackend()

        backend.sync_single_issue(self.issue)

        mock_sync_issue.assert_called_once_with(self.issue)
        mock_sync_attachments.assert_called_once_with(self.issue)
        mock_sync_comments.assert_called_once_with(self.issue)


class SyncIssuesTest(test.APITestCase):
    """Tests for the sync_issues method."""

    def setUp(self):
        self.issue1 = factories.IssueFactory(
            backend_name=SupportBackendType.ATLASSIAN,
            backend_id="TEST-1",
            key="TEST-1",
        )
        self.issue2 = factories.IssueFactory(
            backend_name=SupportBackendType.ATLASSIAN,
            backend_id="TEST-2",
            key="TEST-2",
        )
        # Issue with different backend - should not be synced
        self.issue_other = factories.IssueFactory(
            backend_name="other_backend",
            backend_id="OTHER-1",
            key="OTHER-1",
        )

    @mock.patch.object(ServiceDeskBackend, "sync_single_issue")
    def test_sync_issues_syncs_only_matching_backend(self, mock_sync_single):
        backend = ServiceDeskBackend()

        backend.sync_issues()

        # Should sync both atlassian issues
        self.assertEqual(mock_sync_single.call_count, 2)
        synced_issues = [call[0][0] for call in mock_sync_single.call_args_list]
        self.assertIn(self.issue1, synced_issues)
        self.assertIn(self.issue2, synced_issues)
        self.assertNotIn(self.issue_other, synced_issues)

    @mock.patch.object(ServiceDeskBackend, "sync_single_issue")
    def test_sync_issues_with_issue_id_syncs_only_that_issue(self, mock_sync_single):
        backend = ServiceDeskBackend()

        backend.sync_issues(issue_id=self.issue1.id)

        mock_sync_single.assert_called_once_with(self.issue1)

    @mock.patch.object(ServiceDeskBackend, "sync_single_issue")
    def test_sync_issues_continues_on_error_for_batch(self, mock_sync_single):
        """For batch sync, errors are logged but sync continues."""
        mock_sync_single.side_effect = [Exception("Error"), None]
        backend = ServiceDeskBackend()

        # Should not raise - continues processing
        backend.sync_issues()

        self.assertEqual(mock_sync_single.call_count, 2)

    @mock.patch.object(ServiceDeskBackend, "sync_single_issue")
    def test_sync_issues_raises_error_for_single_issue(self, mock_sync_single):
        """For single issue sync, errors are re-raised."""
        mock_sync_single.side_effect = Exception("Sync error")
        backend = ServiceDeskBackend()

        with self.assertRaises(Exception) as ctx:
            backend.sync_issues(issue_id=self.issue1.id)

        self.assertEqual(str(ctx.exception), "Sync error")


class CommentSynchronizerTest(test.APITestCase):
    """Tests for the CommentSynchronizer class."""

    def setUp(self):
        self.issue = factories.IssueFactory(
            backend_name=SupportBackendType.ATLASSIAN,
            backend_id="TEST-123",
            key="TEST-123",
        )
        self.support_user = factories.SupportUserFactory(
            backend_name=SupportBackendType.ATLASSIAN,
        )

    def test_perform_update_creates_new_comments(self):
        """Comments in backend but not in Waldur are created."""
        backend_issue = {}
        backend = mock.MagicMock()
        backend.get.return_value = {
            "comments": [
                {
                    "id": "new-comment-1",
                    "body": "New comment",
                    "author": {"accountId": self.support_user.backend_id},
                }
            ]
        }

        def populate_comment(backend_comment, comment):
            comment.description = backend_comment.get("body", "")
            comment.backend_name = SupportBackendType.ATLASSIAN
            comment.author = self.support_user

        backend._backend_comment_to_comment = mock.MagicMock(
            side_effect=populate_comment
        )

        synchronizer = CommentSynchronizer(backend, self.issue, backend_issue)
        synchronizer.perform_update()

        self.assertEqual(models.Comment.objects.filter(issue=self.issue).count(), 1)
        comment = models.Comment.objects.get(issue=self.issue)
        self.assertEqual(comment.backend_id, "new-comment-1")

    def test_perform_update_deletes_stale_comments(self):
        """Comments in Waldur but not in backend are deleted."""
        # Create a local comment that doesn't exist in backend
        stale_comment = factories.CommentFactory(
            issue=self.issue,
            backend_id="stale-comment-1",
        )

        backend_issue = {}
        backend = mock.MagicMock()
        backend.get.return_value = {"comments": []}  # No comments in backend

        synchronizer = CommentSynchronizer(backend, self.issue, backend_issue)
        synchronizer.perform_update()

        # Stale comment should be deleted
        self.assertFalse(models.Comment.objects.filter(id=stale_comment.id).exists())

    def test_perform_update_updates_existing_comments(self):
        """Comments that exist in both are updated."""
        existing_comment = factories.CommentFactory(
            issue=self.issue,
            backend_id="existing-comment-1",
            description="Old description",
        )

        backend_issue = {}
        backend = mock.MagicMock()
        backend.get.return_value = {
            "comments": [
                {
                    "id": "existing-comment-1",
                    "body": "Updated description",
                    "author": {"accountId": self.support_user.backend_id},
                }
            ]
        }
        backend._backend_comment_to_comment = mock.MagicMock(
            side_effect=lambda bc, c: setattr(c, "description", bc["body"])
        )

        synchronizer = CommentSynchronizer(backend, self.issue, backend_issue)
        synchronizer.perform_update()

        existing_comment.refresh_from_db()
        self.assertEqual(existing_comment.description, "Updated description")


class GetOrCreateSupportUserTest(test.APITestCase):
    """Tests for get_or_create_support_user handling duplicates."""

    def test_handles_duplicate_support_users(self):
        """When duplicates exist, returns first one without error."""
        # Create duplicate support users
        user_id = "duplicate-user-id"
        factories.SupportUserFactory(
            backend_id=user_id,
            backend_name=SupportBackendType.ATLASSIAN,
            name="First",
        )
        factories.SupportUserFactory(
            backend_id=user_id,
            backend_name=SupportBackendType.ATLASSIAN,
            name="Second",
        )
        factories.SupportUserFactory(
            backend_id=user_id,
            backend_name=SupportBackendType.ATLASSIAN,
            name="Third",
        )

        backend = ServiceDeskBackend()

        # Should not raise MultipleObjectsReturned
        user = backend.get_or_create_support_user(user_id)

        self.assertIsNotNone(user)
        self.assertEqual(user.backend_id, user_id)

    def test_creates_user_if_not_exists(self):
        """Creates new support user if none exists."""
        user_id = "new-user-id"
        backend = ServiceDeskBackend()

        user = backend.get_or_create_support_user(user_id)

        self.assertIsNotNone(user)
        self.assertEqual(user.backend_id, user_id)
        self.assertEqual(user.backend_name, SupportBackendType.ATLASSIAN)

    def test_returns_existing_user(self):
        """Returns existing user without creating duplicate."""
        user_id = "existing-user-id"
        existing = factories.SupportUserFactory(
            backend_id=user_id,
            backend_name=SupportBackendType.ATLASSIAN,
        )
        backend = ServiceDeskBackend()

        user = backend.get_or_create_support_user(user_id)

        self.assertEqual(user.id, existing.id)
        # Verify no duplicate was created
        self.assertEqual(
            models.SupportUser.objects.filter(
                backend_id=user_id,
                backend_name=SupportBackendType.ATLASSIAN,
            ).count(),
            1,
        )
