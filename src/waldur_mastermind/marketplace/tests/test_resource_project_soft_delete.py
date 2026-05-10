"""Soft delete + recovery on ResourceProject.

DELETE marks the row is_removed=True, captures active UserRoles into
termination_metadata, and revokes them. The default queryset hides removed
rows, but the unique-name constraint allows recreation with the same name.
Staff can hard-delete via ?force=true. Resource state transitioning into
TERMINATED cascades a soft-delete to children. POST /recover/ flips
is_removed back and optionally restores team members.
"""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions.models import Role, UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


def _list_url():
    return "http://testserver" + reverse("marketplace-resource-project-list")


def _detail_url(rp):
    return "http://testserver" + reverse(
        "marketplace-resource-project-detail", kwargs={"uuid": rp.uuid.hex}
    )


class _Base(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.plugin_options = {"enable_resource_projects": True}
        self.offering.save(update_fields=["plugin_options"])
        self.resource = self.fixture.resource
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(self.staff)

    def _create_rp(self, name="rp-1") -> models.ResourceProject:
        response = self.client.post(
            _list_url(),
            {"resource": self.resource.uuid.hex, "name": name},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return models.ResourceProject.available_objects.get(uuid=response.data["uuid"])


class SoftDeleteTest(_Base):
    def test_delete_marks_is_removed_and_records_audit_fields(self):
        rp = self._create_rp()
        response = self.client.delete(_detail_url(rp))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Row still exists, just flagged
        rp.refresh_from_db()
        self.assertTrue(rp.is_removed)
        self.assertIsNotNone(rp.removed_date)
        self.assertEqual(rp.removed_by, self.staff)

    def test_removed_project_hidden_from_list_and_detail(self):
        rp = self._create_rp()
        self.client.delete(_detail_url(rp))

        # 404 on detail
        response = self.client.get(_detail_url(rp))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Excluded from list
        response = self.client.get(
            _list_url(), {"resource_uuid": self.resource.uuid.hex}
        )
        uuids = [item["uuid"] for item in response.data]
        self.assertNotIn(rp.uuid.hex, uuids)

    def test_list_with_include_removed_surfaces_soft_delete_fields(self):
        rp = self._create_rp()
        self.client.delete(_detail_url(rp))

        response = self.client.get(
            _list_url(),
            {"resource_uuid": self.resource.uuid.hex, "include_removed": "true"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = [item for item in response.data if item["uuid"] == rp.uuid.hex]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["is_removed"])
        self.assertIsNotNone(row["removed_date"])
        self.assertEqual(row["removed_by_username"], self.staff.username)

    def test_managers_partition_removed_vs_active(self):
        rp = self._create_rp()
        self.client.delete(_detail_url(rp))

        self.assertEqual(models.ResourceProject.objects.filter(pk=rp.pk).count(), 1)
        self.assertEqual(
            models.ResourceProject.available_objects.filter(pk=rp.pk).count(), 0
        )

    def test_recreate_with_same_name_after_soft_delete(self):
        rp = self._create_rp("repeated-name")
        self.client.delete(_detail_url(rp))

        # Partial unique constraint allows the same name on a new active row
        response = self.client.post(
            _list_url(),
            {"resource": self.resource.uuid.hex, "name": "repeated-name"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        new_rp = models.ResourceProject.available_objects.get(
            uuid=response.data["uuid"]
        )
        self.assertNotEqual(new_rp.pk, rp.pk)


class ForceDeleteTest(_Base):
    def test_staff_can_hard_delete_with_force_true(self):
        rp = self._create_rp()
        response = self.client.delete(_detail_url(rp) + "?force=true")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.ResourceProject.objects.filter(pk=rp.pk).exists())

    def test_non_staff_cannot_force_delete(self):
        # The fixture's owner has UPDATE_RESOURCE on the project's customer,
        # so plain DELETE is allowed; ?force=true must be rejected with 403.
        rp = self._create_rp()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(_detail_url(rp) + "?force=true")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        rp.refresh_from_db()
        self.assertFalse(rp.is_removed)

    def test_force_false_falls_back_to_soft_delete(self):
        rp = self._create_rp()
        response = self.client.delete(_detail_url(rp) + "?force=false")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        rp.refresh_from_db()
        self.assertTrue(rp.is_removed)


class CascadeOnResourceTerminatedTest(_Base):
    def test_resource_terminated_soft_deletes_children(self):
        rp = self._create_rp("child-1")
        rp2 = self._create_rp("child-2")

        # Move resource through OK → TERMINATING → TERMINATED to fire the post_save handler
        self.resource.set_state_ok()
        self.resource.save(update_fields=["state"])
        self.resource.set_state_terminating()
        self.resource.save(update_fields=["state"])
        self.resource.set_state_terminated()
        self.resource.save(update_fields=["state"])

        for r in (rp, rp2):
            r.refresh_from_db()
            self.assertTrue(r.is_removed)
            self.assertIsNotNone(r.removed_date)
            # System action — no user attached
            self.assertIsNone(r.removed_by)

    def test_already_removed_children_are_not_re_touched(self):
        rp = self._create_rp("child-1")
        # Soft-delete user-initiated first
        self.client.delete(_detail_url(rp))
        rp.refresh_from_db()
        original_removed_date = rp.removed_date
        self.assertEqual(rp.removed_by, self.staff)

        # Now terminate the parent — the cascade filters is_removed=False so
        # it should NOT touch this row again
        self.resource.set_state_ok()
        self.resource.save(update_fields=["state"])
        self.resource.set_state_terminating()
        self.resource.save(update_fields=["state"])
        self.resource.set_state_terminated()
        self.resource.save(update_fields=["state"])

        rp.refresh_from_db()
        # Audit metadata preserved from the original user-initiated delete
        self.assertEqual(rp.removed_by, self.staff)
        self.assertEqual(rp.removed_date, original_removed_date)


def _recover_url(rp):
    return (
        "http://testserver"
        + reverse("marketplace-resource-project-detail", kwargs={"uuid": rp.uuid.hex})
        + "recover/"
    )


class _RecoverBase(_Base):
    def _grant_member(self, rp: models.ResourceProject):
        """Add the fixture's regular user as a custom RP role."""
        rp_ct = ContentType.objects.get_for_model(rp)
        role, _ = Role.objects.get_or_create(
            name="rp-member",
            content_type=rp_ct,
            defaults={"is_system_role": False},
        )
        rp.add_user(self.user, role)
        return role


class SoftDeleteRevokesRolesTest(_RecoverBase):
    def test_soft_delete_captures_metadata_and_revokes_active_roles(self):
        rp = self._create_rp()
        role = self._grant_member(rp)
        rp_ct = ContentType.objects.get_for_model(rp)

        # Sanity: role is active before delete
        self.assertTrue(
            UserRole.objects.filter(
                user=self.user,
                role=role,
                content_type=rp_ct,
                object_id=rp.id,
                is_active=True,
            ).exists()
        )

        self.client.delete(_detail_url(rp))
        rp.refresh_from_db()

        # Active role flipped to inactive
        self.assertFalse(
            UserRole.objects.filter(
                user=self.user,
                role=role,
                content_type=rp_ct,
                object_id=rp.id,
                is_active=True,
            ).exists()
        )
        # Metadata captured
        self.assertIsNotNone(rp.termination_metadata)
        snapshot = rp.termination_metadata["user_roles"]
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["user_username"], self.user.username)
        self.assertEqual(snapshot[0]["role_name"], "rp-member")
        self.assertFalse(snapshot[0]["is_restored"])


class RecoverTest(_RecoverBase):
    def test_recover_flips_is_removed_without_restoring_members(self):
        rp = self._create_rp()
        self._grant_member(rp)
        self.client.delete(_detail_url(rp))

        response = self.client.post(_recover_url(rp), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rp.refresh_from_db()
        self.assertFalse(rp.is_removed)
        self.assertIsNone(rp.removed_date)
        self.assertIsNone(rp.removed_by)
        # Without restore_team_members, the active UserRole stays revoked
        rp_ct = ContentType.objects.get_for_model(rp)
        self.assertFalse(
            UserRole.objects.filter(
                user=self.user, content_type=rp_ct, object_id=rp.id, is_active=True
            ).exists()
        )

    def test_recover_with_restore_team_members_recreates_user_roles(self):
        rp = self._create_rp()
        self._grant_member(rp)
        self.client.delete(_detail_url(rp))

        response = self.client.post(
            _recover_url(rp), {"restore_team_members": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["recovery_info"]["restored_users_count"], 1)

        rp.refresh_from_db()
        rp_ct = ContentType.objects.get_for_model(rp)
        self.assertTrue(
            UserRole.objects.filter(
                user=self.user,
                content_type=rp_ct,
                object_id=rp.id,
                is_active=True,
            ).exists()
        )
        # Snapshot is_restored marker is set
        snapshot = rp.termination_metadata["user_roles"][0]
        self.assertTrue(snapshot["is_restored"])
        self.assertEqual(snapshot["restored_by"], self.staff.username)

    def test_recover_blocks_when_resource_terminated(self):
        rp = self._create_rp()
        self.client.delete(_detail_url(rp))

        # Force-terminate the resource via FSM transitions
        self.resource.set_state_ok()
        self.resource.save(update_fields=["state"])
        self.resource.set_state_terminating()
        self.resource.save(update_fields=["state"])
        self.resource.set_state_terminated()
        self.resource.save(update_fields=["state"])

        response = self.client.post(_recover_url(rp), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_recover_blocks_on_active_name_conflict(self):
        rp = self._create_rp("dup-name")
        self.client.delete(_detail_url(rp))
        # Create a new active RP with the same name (allowed by partial unique constraint)
        self._create_rp("dup-name")

        response = self.client.post(_recover_url(rp), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_recover_rejects_options_when_metadata_missing(self):
        rp = self._create_rp()
        self.client.delete(_detail_url(rp))
        # Simulate a legacy soft-delete: clear the captured metadata
        rp.refresh_from_db()
        rp.termination_metadata = None
        rp.save(update_fields=["termination_metadata"])

        response = self.client.post(
            _recover_url(rp), {"restore_team_members": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Bare recover still works
        response = self.client.post(_recover_url(rp), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rp.refresh_from_db()
        self.assertFalse(rp.is_removed)

    def test_recover_rejects_when_not_removed(self):
        rp = self._create_rp()
        response = self.client.post(_recover_url(rp), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_recover_rejects_mutually_exclusive_options(self):
        rp = self._create_rp()
        self._grant_member(rp)
        self.client.delete(_detail_url(rp))

        response = self.client.post(
            _recover_url(rp),
            {
                "restore_team_members": True,
                "send_invitations_to_previous_members": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
