from unittest import mock

from django.test import TestCase

from waldur_keycloak import tasks
from waldur_keycloak.tests import fixtures


class SyncPendingMembershipsTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_pending_membership_activated_when_user_found(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.find_user_by_username.return_value = {
            "id": "kc-user-id",
            "firstName": "Test",
            "lastName": "User",
        }

        membership = self.fixture.keycloak_membership
        self.assertEqual(membership.state, "pending")

        tasks.sync_pending_memberships()

        membership.refresh_from_db()
        self.assertEqual(membership.state, "active")
        self.assertEqual(membership.first_name, "Test")
        self.assertEqual(membership.last_name, "User")
        self.assertEqual(membership.error_message, "")

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_pending_membership_stays_pending_when_user_not_found(
        self, mock_get_client
    ):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.find_user_by_username.return_value = None

        tasks.sync_pending_memberships()

        membership = self.fixture.keycloak_membership
        membership.refresh_from_db()
        self.assertEqual(membership.state, "pending")


class CleanupOrphanedGroupsTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_cleanup_verifies_local_groups_against_remote(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.get_group.return_value = {
            "id": self.fixture.keycloak_group.backend_id,
        }

        tasks.cleanup_orphaned_groups()
        mock_keycloak.get_group.assert_called_once_with(
            self.fixture.keycloak_group.backend_id
        )


class CleanupOrphanedGroupsDetailTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_missing_remote_group_clears_backend_id(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.get_group.return_value = None

        group = self.fixture.keycloak_group
        self.assertNotEqual(group.backend_id, "")

        tasks.cleanup_orphaned_groups()

        group.refresh_from_db()
        self.assertEqual(group.backend_id, "")


class CleanupOrphanedMembershipsTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_cleanup_runs_without_error(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.list_group_members.return_value = []

        tasks.cleanup_orphaned_memberships()
        mock_keycloak.list_group_members.assert_called_once()


class CleanupOrphanedMembershipsDetailTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_externally_removed_member_flagged_with_error(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak

        # Make the membership active so the cleanup task inspects it
        membership = self.fixture.keycloak_membership
        membership.activate()
        membership.save()

        # Remote group exists but our active user is no longer in it
        mock_keycloak.list_group_members.return_value = []

        tasks.cleanup_orphaned_memberships()

        membership.refresh_from_db()
        self.assertIn("removed", membership.error_message.lower())
