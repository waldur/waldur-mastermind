"""Tests for staff/support historical lookup of user roles (WAL-10031).

Covers exposing revoked (inactive) role grants, the is_active filter,
revoke/restore actions on a specific grant and the scope_is_removed flag
for soft-deleted scopes.
"""

from rest_framework import status, test

from waldur_core.permissions import utils
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures


class HistoricalRolesTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.owner = self.fixture.owner

        self.staff = factories.UserFactory(is_staff=True)
        self.support = factories.UserFactory(is_support=True)
        self.target = factories.UserFactory()

        # One active grant and one revoked grant for the target user.
        self.active_role = utils.add_user(
            self.project, self.target, ProjectRole.ADMIN, created_by=self.owner
        )
        self.revoked_role = utils.add_user(
            self.customer, self.target, CustomerRole.SUPPORT, created_by=self.owner
        )
        self.revoked_role.revoke(current_user=self.owner, reason="test reason")

        self.url = "/api/user-permissions/"

    def _detail_url(self, user_role, action):
        return f"{self.url}{user_role.uuid.hex}/{action}/"

    # --- visibility -----------------------------------------------------

    def test_staff_sees_both_active_and_revoked_roles(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            self.url, {"user": self.target.uuid.hex, "show_inactive": "true"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_support_sees_both_active_and_revoked_roles(self):
        self.client.force_authenticate(self.support)
        response = self.client.get(
            self.url, {"user": self.target.uuid.hex, "show_inactive": "true"}
        )
        self.assertEqual(len(response.data), 2)

    def test_staff_default_hides_revoked_roles(self):
        # Without show_inactive the endpoint keeps its historical behaviour:
        # only active grants are returned, even for staff.
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {"user": self.target.uuid.hex})
        self.assertEqual(len(response.data), 1)
        self.assertTrue(all(item["is_active"] for item in response.data))

    def test_is_active_filter_returns_only_revoked_roles(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            self.url,
            {
                "user": self.target.uuid.hex,
                "show_inactive": "true",
                "is_active": "false",
            },
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["role_name"], CustomerRole.SUPPORT.name)
        self.assertFalse(response.data[0]["is_active"])

    def test_is_active_filter_returns_only_active_roles(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            self.url, {"user": self.target.uuid.hex, "is_active": "true"}
        )
        self.assertEqual(len(response.data), 1)
        self.assertTrue(response.data[0]["is_active"])

    def test_regular_user_does_not_see_revoked_roles(self):
        self.client.force_authenticate(self.target)
        response = self.client.get(self.url)
        # Only the single active grant of the user itself is visible.
        self.assertEqual(len(response.data), 1)
        self.assertTrue(all(item["is_active"] for item in response.data))

    # --- serializer fields ---------------------------------------------

    def test_revoked_role_exposes_revoke_metadata(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            self.url,
            {
                "user": self.target.uuid.hex,
                "show_inactive": "true",
                "is_active": "false",
            },
        )
        item = response.data[0]
        self.assertEqual(item["uuid"], self.revoked_role.uuid.hex)
        self.assertEqual(item["revoked_by_username"], self.owner.username)
        self.assertEqual(item["revoke_reason"], "test reason")
        self.assertFalse(item["scope_is_removed"])

    def test_scope_is_removed_flag_for_soft_deleted_project(self):
        role = utils.add_user(
            self.project, self.target, ProjectRole.MANAGER, created_by=self.owner
        )
        role.revoke(current_user=self.owner, reason="project gone")
        self.project.is_removed = True
        self.project.save()

        self.client.force_authenticate(self.staff)
        response = self.client.get(
            self.url,
            {
                "user": self.target.uuid.hex,
                "scope_uuid": self.project.uuid.hex,
                "show_inactive": "true",
                "is_active": "false",
            },
        )
        self.assertTrue(any(item["scope_is_removed"] for item in response.data))

    # --- revoke action --------------------------------------------------

    def test_staff_can_revoke_active_role(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self._detail_url(self.active_role, "revoke"), {"reason": "no longer needed"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.active_role.refresh_from_db()
        self.assertFalse(self.active_role.is_active)
        self.assertEqual(self.active_role.revoked_by, self.staff)
        self.assertEqual(self.active_role.revoke_reason, "no longer needed")

    def test_revoking_already_revoked_role_fails(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self._detail_url(self.revoked_role, "revoke"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_support_cannot_revoke_without_permission(self):
        self.client.force_authenticate(self.support)
        response = self.client.post(self._detail_url(self.active_role, "revoke"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.active_role.refresh_from_db()
        self.assertTrue(self.active_role.is_active)

    # --- restore action -------------------------------------------------

    def test_staff_can_restore_revoked_role(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self._detail_url(self.revoked_role, "restore"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.revoked_role.refresh_from_db()
        self.assertTrue(self.revoked_role.is_active)
        self.assertIsNone(self.revoked_role.revoked_by)
        self.assertEqual(self.revoked_role.revoke_reason, "")

    def test_restoring_active_role_fails(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self._detail_url(self.active_role, "restore"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_restore_role_when_equivalent_active_grant_exists(self):
        # Re-grant the same role on the same scope after revocation: restoring
        # the old grant must be refused to avoid two active grants.
        utils.add_user(
            self.customer, self.target, CustomerRole.SUPPORT, created_by=self.owner
        )
        self.client.force_authenticate(self.staff)
        response = self.client.post(self._detail_url(self.revoked_role, "restore"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.revoked_role.refresh_from_db()
        self.assertFalse(self.revoked_role.is_active)

    def test_cannot_restore_role_on_soft_deleted_project(self):
        role = utils.add_user(
            self.project, self.target, ProjectRole.MANAGER, created_by=self.owner
        )
        role.revoke(current_user=self.owner)
        self.project.is_removed = True
        self.project.save()

        self.client.force_authenticate(self.staff)
        response = self.client.post(self._detail_url(role, "restore"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        role.refresh_from_db()
        self.assertFalse(role.is_active)

    def test_regular_user_cannot_access_other_users_role_action(self):
        other = factories.UserFactory()
        self.client.force_authenticate(other)
        response = self.client.post(self._detail_url(self.active_role, "revoke"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
