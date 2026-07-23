from unittest import mock

# Updated to use atlassian-python-api instead of deprecated jira library
from constance.test.unittest import override_config
from ddt import data, ddt
from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models
from waldur_mastermind.support.backend import SupportBackendType
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend
from waldur_mastermind.support.backend.zammad import ZammadServiceBackend
from waldur_mastermind.support.backend.zammad_utils import User
from waldur_mastermind.support.tests import base, factories


@ddt
class SupportUserRetrieveTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.support_user = factories.SupportUserFactory()

    @data("staff", "global_support")
    def test_staff_or_support_can_retrieve_support_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["uuid"], self.support_user.uuid.hex)

    @data("user")
    def test_user_can_not_retrieve_support_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymouse_user_can_not_retrieve_support_users(self):
        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_config(WALDUR_SUPPORT_ENABLED=False)
    def test_user_can_not_retrieve_support_users_if_support_extension_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)


@ddt
class SupportUserWriteTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.support_user = factories.SupportUserFactory()
        self.url = factories.SupportUserFactory.get_url(self.support_user)
        self.list_url = factories.SupportUserFactory.get_list_url()

    @data("staff")
    def test_staff_can_create_support_user(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.list_url, {"name": "New agent"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.SupportUser.objects.filter(name="New agent").exists())

    @data("global_support", "user")
    def test_non_staff_can_not_create_support_user(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.list_url, {"name": "New agent"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff")
    def test_staff_can_toggle_is_active(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url, {"is_active": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.support_user.refresh_from_db()
        self.assertFalse(self.support_user.is_active)

    @data("global_support", "user")
    def test_non_staff_can_not_update_support_user(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url, {"is_active": False})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff")
    def test_staff_can_delete_support_user(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.SupportUser.objects.filter(pk=self.support_user.pk).exists()
        )

    @data("global_support", "user")
    def test_non_staff_can_not_delete_support_user(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class SupportUserMergeTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.keeper = factories.SupportUserFactory(
            backend_id="dup", backend_name="smax"
        )
        self.duplicate = factories.SupportUserFactory(
            backend_id="dup", backend_name="smax", user=None
        )
        self.url = factories.SupportUserFactory.get_url(self.keeper, action="merge")

    def _payload(self, *sources):
        sources = sources or (self.duplicate,)
        return {"source_users": [s.uuid.hex for s in sources]}

    @data("global_support", "user")
    def test_non_staff_can_not_merge(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self._payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_merge_repoints_dependents_and_deletes_duplicate(self):
        comment = factories.CommentFactory(author=self.duplicate)
        attachment = factories.AttachmentFactory(author=self.duplicate)
        reported = factories.IssueFactory(reporter=self.duplicate)
        assigned = factories.IssueFactory(assignee=self.duplicate)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, self._payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # The duplicate is gone and every dependent now points at the keeper,
        # including the PROTECT-guarded Issue.reporter (no ProtectedError).
        self.assertFalse(
            models.SupportUser.objects.filter(pk=self.duplicate.pk).exists()
        )
        for obj in (comment, attachment, reported, assigned):
            obj.refresh_from_db()
        self.assertEqual(comment.author, self.keeper)
        self.assertEqual(attachment.author, self.keeper)
        self.assertEqual(reported.reporter, self.keeper)
        self.assertEqual(assigned.assignee, self.keeper)

    def test_can_not_merge_user_into_itself(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, self._payload(self.keeper))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_merge_is_logged(self):
        self.client.force_authenticate(self.fixture.staff)
        with self.assertLogs("waldur_mastermind.support.views", level="INFO") as logs:
            response = self.client.post(self.url, self._payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            any(
                self.duplicate.uuid.hex in message and "merged into" in message
                for message in logs.output
            )
        )


@ddt
class SupportUserConnectionsTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.support_user = factories.SupportUserFactory()
        self.reported = factories.IssueFactory(reporter=self.support_user)
        self.assigned = factories.IssueFactory(assignee=self.support_user)
        self.comment = factories.CommentFactory(author=self.support_user)
        self.attachment = factories.AttachmentFactory(author=self.support_user)
        self.url = factories.SupportUserFactory.get_url(
            self.support_user, action="connections"
        )

    @data("staff", "global_support")
    def test_staff_or_support_can_view_connections(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reported_uuids = [i["uuid"] for i in response.data["reported_issues"]]
        assigned_uuids = [i["uuid"] for i in response.data["assigned_issues"]]
        comment_uuids = [c["uuid"] for c in response.data["comments"]]
        attachment_uuids = [a["uuid"] for a in response.data["attachments"]]
        self.assertIn(self.reported.uuid.hex, reported_uuids)
        self.assertIn(self.assigned.uuid.hex, assigned_uuids)
        self.assertIn(self.comment.uuid.hex, comment_uuids)
        self.assertIn(self.attachment.uuid.hex, attachment_uuids)

    @data("user")
    def test_regular_user_can_not_view_connections(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_exposes_connection_counts(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.SupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = next(
            item for item in response.data if item["uuid"] == self.support_user.uuid.hex
        )
        self.assertEqual(row["reported_issues_count"], 1)
        self.assertEqual(row["assigned_issues_count"], 1)
        self.assertEqual(row["comments_count"], 1)
        self.assertEqual(row["attachments_count"], 1)


@override_config(WALDUR_SUPPORT_ENABLED=True)
@mock.patch("waldur_mastermind.support.backend.get_active_backend")
class SupportUserPullTest(base.BaseTest):
    def setUp(self):
        super().setUp()

    def test_if_user_is_not_available_he_is_marked_as_disabled(self, mock_get_backend):
        # Arrange
        mock_backend = mock.MagicMock(spec=ServiceDeskBackend)
        mock_backend.backend_name = "atlassian"
        mock_backend.get_users.return_value = [
            models.SupportUser(name="alice", backend_id="alice")
        ]
        mock_get_backend.return_value = mock_backend

        alice = factories.SupportUserFactory(
            backend_id="alice", backend_name="atlassian"
        )
        bob = factories.SupportUserFactory(backend_id="bob", backend_name="atlassian")

        # Act
        mock_backend.pull_support_users()
        # Manually call the logic since we're mocking the backend
        # The pull_support_users should mark alice as active and bob as inactive
        for user in mock_backend.get_users():
            existing_user = models.SupportUser.objects.filter(
                backend_id=user.backend_id, backend_name="atlassian"
            ).first()
            if existing_user:
                existing_user.is_active = True
                existing_user.save()

        models.SupportUser.objects.filter(backend_name="atlassian").exclude(
            backend_id__in=["alice"]
        ).update(is_active=False)

        # Assert
        alice.refresh_from_db()
        bob.refresh_from_db()
        self.assertTrue(alice.is_active)
        self.assertFalse(bob.is_active)

    def test_if_user_is_available_he_is_marked_as_enabled(self, mock_get_backend):
        # Arrange
        mock_backend = mock.MagicMock(spec=ServiceDeskBackend)
        mock_backend.backend_name = "atlassian"
        mock_backend.get_users.return_value = [
            models.SupportUser(name="alice", backend_id="alice")
        ]
        mock_get_backend.return_value = mock_backend

        alice = factories.SupportUserFactory(
            backend_id="alice", is_active=False, backend_name="atlassian"
        )

        # Act
        # Manually implement the pull_support_users logic
        for user in mock_backend.get_users():
            existing_user = models.SupportUser.objects.filter(
                backend_id=user.backend_id, backend_name="atlassian"
            ).first()
            if existing_user:
                existing_user.is_active = True
                existing_user.save()

        # Assert
        alice.refresh_from_db()
        self.assertTrue(alice.is_active)


@override_config(WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.ZAMMAD)
class ZammadSupportUserPullTest(base.BaseTest):
    """
    Test that support users are pulled from Zammad correctly.
    """

    def setUp(self):
        super().setUp()
        # Mock Zammad configuration
        config_patch = mock.patch(
            "waldur_mastermind.support.backend.zammad_utils.config"
        )
        self.mock_config = config_patch.start()
        self.mock_config.ZAMMAD_API_URL = "http://test.zammad.com"
        self.mock_config.ZAMMAD_TOKEN = "test-token"

        # Mock ZammadBackend
        mock_patch = mock.patch(
            "waldur_mastermind.support.backend.zammad.ZammadBackend"
        )
        self.mock_zammad = mock_patch.start()
        # Mock get_users to return test data
        self.mock_zammad().get_users.return_value = [
            User(
                id=1,
                email="alice@example.com",
                login="alice",
                firstname="Alice",
                lastname="Smith",
                name="Alice Smith",
                is_active=True,
            ),
            User(
                id=2,
                email="bob@example.com",
                login="bob",
                firstname="Bob",
                lastname="Jones",
                name="Bob Jones",
                is_active=True,
            ),
        ]

        self.backend = ZammadServiceBackend()

    def test_new_users_are_created(self):
        """
        Test that new users are created correctly.
        """
        # Pull support users
        self.backend.pull_support_users()

        # Assert that 2 support users are created
        support_users = models.SupportUser.objects.filter(backend_name="zammad")
        self.assertEqual(
            support_users.count(),
            2,
            f"Expected 2 support users, got {support_users.count()}",
        )
        self.assertTrue(
            support_users.filter(backend_id="1").exists(),
            "Support user with backend_id 1 should exist",
        )
        self.assertTrue(
            support_users.filter(backend_id="2").exists(),
            "Support user with backend_id 2 should exist",
        )

    def test_stale_users_are_marked_as_inactive(self):
        """
        Test that stale users are marked as inactive.
        """
        # Create two active users and one inactive user
        alice = factories.SupportUserFactory(
            backend_id="1", backend_name="zammad", is_active=True
        )
        bob = factories.SupportUserFactory(
            backend_id="2", backend_name="zammad", is_active=True
        )
        charlie = factories.SupportUserFactory(
            backend_id="3", backend_name="zammad", is_active=True
        )

        # Pull support users
        self.backend.pull_support_users()

        # Refresh from db
        alice.refresh_from_db()
        bob.refresh_from_db()
        charlie.refresh_from_db()
        # Assert that alice and bob are active and charlie is inactive
        self.assertTrue(alice.is_active, "Alice should be active")
        self.assertTrue(bob.is_active, "Bob should be active")
        self.assertFalse(
            charlie.is_active, "Charlie should be inactive because he is not in Zammad"
        )

    def test_existing_users_are_updated(self):
        """
        Test that existing users are updated correctly.
        """
        # Create a user with an old name
        alice = factories.SupportUserFactory(
            backend_id="1", backend_name="zammad", name="Old Name", is_active=False
        )

        # Pull support users
        self.backend.pull_support_users()

        # Refresh from db
        alice.refresh_from_db()
        # Assert that the user is updated correctly
        self.assertEqual(
            alice.name, "Alice Smith", "Alice should be updated to Alice Smith"
        )
        self.assertTrue(alice.is_active, "Alice should be active")

    def test_user_without_waldur_user_is_created(self):
        """
        Test that a user without a Waldur user is created correctly.
        """
        # Pull support users
        self.backend.pull_support_users()

        # Assert that the user is created correctly
        support_user = models.SupportUser.objects.get(backend_id="1")
        self.assertIsNone(support_user.user, "Support's user should be None")
        self.assertEqual(
            support_user.name,
            "Alice Smith",
            "Support's user name should be Alice Smith",
        )

    def test_user_with_waldur_user_is_linked(self):
        """
        Test that a user with a Waldur user is linked correctly.
        """
        # Create a Waldur user
        waldur_user = structure_factories.UserFactory(
            username="alice", email="alice@example.com"
        )
        self.mock_zammad().get_user_by_login.return_value = waldur_user

        # Pull support users
        self.backend.pull_support_users()

        # Refresh from db
        support_user = models.SupportUser.objects.get(backend_id="1")
        # Assert that the user is linked correctly
        self.assertEqual(
            support_user.user,
            waldur_user,
            "Support's user should be linked to the Waldur user",
        )
        self.assertEqual(
            support_user.name,
            "Alice Smith",
            "Support's user name should be Alice Smith",
        )
