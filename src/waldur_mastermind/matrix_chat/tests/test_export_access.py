from datetime import timedelta
from unittest import mock

from constance.test import override_config
from ddt import data, ddt
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.models import Token
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.structure.models import Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import extension, models, tasks
from waldur_mastermind.matrix_chat.tests import fixtures


def _with_files(export):
    export.export_file.save("history.json", ContentFile(b'{"messages": []}'))
    export.media_file.save("media.zip", ContentFile(b"PK\x03\x04"))
    return export.export_file.name, export.media_file.name


class ExportAccessTest(test.APITestCase):
    # An export is the room's whole history, media included, so it goes to
    # whoever manages the room and no further: staff and support, who run every
    # room from the administration page, and those who may create the room.
    # Being in the room is not enough.

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.export = self.fixture.history_export
        _with_files(self.export)

    def _download(self, user):
        self.client.force_authenticate(user)
        return self.client.get(
            f"/api/matrix/exports/{self.export.uuid}/download/export/"
        ).status_code

    def _listed(self, user):
        self.client.force_authenticate(user)
        response = self.client.get("/api/matrix/exports/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [item["uuid"] for item in response.data] == [self.export.uuid.hex]

    def assertAllowed(self, user):
        self.assertEqual(self._download(user), status.HTTP_200_OK)
        self.assertTrue(self._listed(user))

    def assertDenied(self, user):
        self.assertEqual(self._download(user), status.HTTP_404_NOT_FOUND)
        self.assertFalse(self._listed(user))

    def test_customer_owner_can_download(self):
        self.assertAllowed(self.fixture.owner)

    def test_staff_can_download(self):
        self.assertAllowed(self.fixture.staff)

    def test_global_support_can_download(self):
        self.assertAllowed(self.fixture.global_support)

    def test_room_member_cannot_download(self):
        self.assertDenied(self.fixture.matrix_room_member.user)

    def test_project_manager_cannot_download(self):
        self.assertDenied(self.fixture.manager)

    def test_editing_the_project_is_not_enough(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROJECT)

        self.assertDenied(self.fixture.manager)

    def test_project_manager_granted_room_creation_can_download(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)

        self.assertAllowed(self.fixture.manager)

    def test_customer_role_other_than_owner_cannot_download(self):
        self.assertDenied(self.fixture.customer_support)

    def test_owner_who_lost_the_role_cannot_download(self):
        # Nothing takes a customer role holder out of the room when the role
        # goes, so access follows the role and not the membership row.
        owner = self.fixture.owner
        models.MatrixRoomMember.objects.create(
            room=self.fixture.matrix_room,
            user=owner,
            matrix_user_id=f"@{owner.username}:matrix.example.com",
            membership_state=models.MembershipStates.JOINED,
        )
        self.fixture.customer.remove_user(owner, CustomerRole.OWNER)

        self.assertDenied(owner)

    def test_customer_owner_can_download_a_customer_room_export(self):
        customer = self.fixture.customer
        room = models.MatrixRoom.objects.create(
            room_id="!customer:matrix.example.com",
            room_name=customer.name,
            state=models.RoomStates.ACTIVE,
            content_type=ContentType.objects.get_for_model(customer),
            object_id=customer.id,
        )
        self.export = models.MatrixHistoryExport.objects.create(
            room=room, state=models.ExportStates.COMPLETED
        )
        _with_files(self.export)

        self.assertEqual(self._download(self.fixture.owner), status.HTTP_200_OK)
        self.assertEqual(
            self._download(self.fixture.customer_support), status.HTTP_404_NOT_FOUND
        )

    def test_customer_owner_can_download_after_the_project_is_removed(self):
        # The export made when a project is terminated is the only copy left,
        # and it stays with whoever managed the room.
        Project.objects.filter(pk=self.fixture.project.pk).update(is_removed=True)

        self.assertAllowed(self.fixture.owner)


@override_config(PAT_ENABLED=True)
class ExportTokenAccessTest(test.APITestCase):
    # A personal access token is held to what it carries, as on the room's
    # write actions: its bindings, the support scope for support, and
    # MATRIX_ROOM.CREATE for those who manage the room through a role.

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.export = self.fixture.history_export
        _with_files(self.export)

    def _token(self, user, scopes, bindings=()):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        Token.objects.get_or_create(user=user)
        pat = _create_pat(user, scopes=scopes, bindings=list(bindings))
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))

    def assertAllowed(self):
        response = self.client.get(
            f"/api/matrix/exports/{self.export.uuid}/download/export/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.get("/api/matrix/exports/")
        self.assertEqual(
            [item["uuid"] for item in response.data], [self.export.uuid.hex]
        )

    def assertDenied(self):
        response = self.client.get(
            f"/api/matrix/exports/{self.export.uuid}/download/export/"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.get("/api/matrix/exports/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_support_token_without_support_scope_is_denied(self):
        self._token(self.fixture.global_support, [PermissionEnum.LIST_PROJECTS.value])

        self.assertDenied()

    def test_support_token_with_support_scope_is_allowed(self):
        self._token(self.fixture.global_support, [PermissionEnum.SUPPORT_ACCESS.value])

        self.assertAllowed()

    def test_staff_token_without_staff_scope_is_denied(self):
        self._token(self.fixture.staff, [PermissionEnum.LIST_PROJECTS.value])

        self.assertDenied()

    def test_owner_token_without_room_permission_is_denied(self):
        self._token(self.fixture.owner, [PermissionEnum.LIST_PROJECTS.value])

        self.assertDenied()

    def test_owner_token_with_room_permission_is_allowed(self):
        self._token(self.fixture.owner, [PermissionEnum.CREATE_MATRIX_ROOM.value])

        self.assertAllowed()

    def test_owner_token_bound_to_the_project_is_allowed(self):
        self._token(
            self.fixture.owner,
            [PermissionEnum.CREATE_MATRIX_ROOM.value],
            [self.fixture.project],
        )

        self.assertAllowed()

    def test_owner_token_bound_to_the_customer_is_allowed(self):
        self._token(
            self.fixture.owner,
            [PermissionEnum.CREATE_MATRIX_ROOM.value],
            [self.fixture.customer],
        )

        self.assertAllowed()

    def test_owner_token_bound_to_another_project_is_denied(self):
        other = structure_factories.ProjectFactory(customer=self.fixture.customer)
        self._token(
            self.fixture.owner, [PermissionEnum.CREATE_MATRIX_ROOM.value], [other]
        )

        self.assertDenied()

    def test_support_token_bound_elsewhere_is_denied(self):
        # Bindings narrow staff and support as well.
        self._token(
            self.fixture.global_support,
            [PermissionEnum.SUPPORT_ACCESS.value],
            [structure_factories.CustomerFactory()],
        )

        self.assertDenied()


class ExportFilesDeletedWithExportTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.export = self.fixture.history_export
        self.names = _with_files(self.export)

    def assertFilesGone(self):
        for name in self.names:
            self.assertFalse(default_storage.exists(name), name)

    def test_deleting_an_export_deletes_its_files(self):
        # Files are rows in the media table, reachable only through the export:
        # left behind, they are full copies of the history nobody can manage.
        self.export.delete()

        self.assertFilesGone()

    def test_deleting_the_room_deletes_its_export_files(self):
        self.fixture.matrix_room.delete()

        self.assertFilesGone()

    @mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
    def test_disabling_the_room_with_delete_history_deletes_its_export_files(
        self, mock_client
    ):
        room = self.fixture.matrix_room
        room.state = models.RoomStates.DISABLING
        room.save(update_fields=["state"])

        tasks.disable_room(str(room.uuid), delete_history=True)

        self.assertFalse(room.exports.exists())
        self.assertFilesGone()


class PeriodicExportTest(TestCase):
    def setUp(self):
        self.room = fixtures.MatrixChatFixture().matrix_room

    @override_config(MATRIX_HISTORY_EXPORT_ENABLED=True, MATRIX_ENABLED=False)
    def test_queues_nothing_while_chat_is_switched_off(self):
        # The export task returns at once without Matrix, which would leave a
        # pending row per room behind every night.
        tasks.periodic_history_export()

        self.assertFalse(self.room.exports.exists())

    @override_config(MATRIX_HISTORY_EXPORT_ENABLED=True)
    @mock.patch("waldur_mastermind.matrix_chat.tasks.export_room_history")
    @mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
    def test_queues_an_export_per_active_room(self, mock_client, mock_export):
        mock_client.is_enabled.return_value = True

        tasks.periodic_history_export()

        export = self.room.exports.get()
        mock_export.delay.assert_called_once_with(str(export.uuid))


@ddt
@override_config(MATRIX_HISTORY_EXPORT_RETENTION_DAYS=30)
class ExportRetentionTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room

    def _export(
        self,
        age_days,
        room=None,
        state=models.ExportStates.COMPLETED,
        export_type=models.ExportTypes.PERIODIC,
        files=True,
    ):
        with freeze_time(timezone.now() - timedelta(days=age_days)):
            export = models.MatrixHistoryExport.objects.create(
                room=room or self.room,
                export_type=export_type,
                state=state,
            )
        return export, _with_files(export) if files else ()

    def _archived_room(self):
        customer = self.fixture.customer
        return models.MatrixRoom.objects.create(
            room_id="!archived:matrix.example.com",
            room_name=customer.name,
            state=models.RoomStates.ARCHIVED,
            content_type=ContentType.objects.get_for_model(customer),
            object_id=customer.id,
        )

    def assertKept(self, *exports):
        for export in exports:
            self.assertTrue(
                models.MatrixHistoryExport.objects.filter(pk=export.pk).exists(),
                export.state,
            )

    def assertDeleted(self, *exports):
        for export in exports:
            self.assertFalse(
                models.MatrixHistoryExport.objects.filter(pk=export.pk).exists(),
                export.state,
            )

    def test_deletes_exports_older_than_the_retention_period(self):
        old, old_files = self._export(age_days=31)
        recent, _ = self._export(age_days=29)

        tasks.cleanup_old_history_exports()

        self.assertFalse(models.MatrixHistoryExport.objects.filter(pk=old.pk).exists())
        self.assertTrue(
            models.MatrixHistoryExport.objects.filter(pk=recent.pk).exists()
        )
        for name in old_files:
            self.assertFalse(default_storage.exists(name), name)

    def test_keeps_the_newest_completed_export_of_a_room_however_old(self):
        older, _ = self._export(age_days=40)
        newest, _ = self._export(age_days=31)

        tasks.cleanup_old_history_exports()

        self.assertDeleted(older)
        self.assertKept(newest)

    def test_keeps_the_final_export_of_an_archived_room(self):
        # An archived room gets one export on deletion and none after it, so
        # that export is the only copy of the history left.
        final, _ = self._export(
            age_days=365,
            room=self._archived_room(),
            export_type=models.ExportTypes.ON_DELETION,
        )

        tasks.cleanup_old_history_exports()

        self.assertKept(final)

    def test_deletes_unfinished_exports_past_the_retention_period(self):
        # Nothing is still exporting after this long: the task died, or never
        # ran, and the row would otherwise stay for good.
        self._export(age_days=1)
        pending, _ = self._export(
            age_days=40, state=models.ExportStates.PENDING, files=False
        )
        exporting, _ = self._export(age_days=40, state=models.ExportStates.EXPORTING)
        failed, _ = self._export(age_days=40, state=models.ExportStates.FAILED)

        tasks.cleanup_old_history_exports()

        self.assertDeleted(pending, exporting, failed)

    def test_keeps_unfinished_exports_within_the_retention_period(self):
        pending, _ = self._export(
            age_days=1, state=models.ExportStates.PENDING, files=False
        )
        exporting, _ = self._export(
            age_days=1, state=models.ExportStates.EXPORTING, files=False
        )

        tasks.cleanup_old_history_exports()

        self.assertKept(pending, exporting)

    def test_deletes_failed_exports_of_a_room_without_completed_ones(self):
        failed, _ = self._export(
            age_days=40,
            room=self._archived_room(),
            state=models.ExportStates.FAILED,
        )

        tasks.cleanup_old_history_exports()

        self.assertDeleted(failed)

    def test_deletes_more_exports_than_fit_in_one_chunk(self):
        expired = [self._export(age_days=40, files=False)[0] for _ in range(5)]
        newest, _ = self._export(age_days=1, files=False)

        with mock.patch.object(tasks, "EXPORT_CLEANUP_CHUNK_SIZE", 2):
            tasks.cleanup_old_history_exports()

        self.assertDeleted(*expired)
        self.assertKept(newest)

    @data(-1, 0)
    def test_retention_of_zero_or_less_keeps_everything(self, days):
        older, _ = self._export(age_days=3650)
        newest, _ = self._export(age_days=3000)

        with override_config(MATRIX_HISTORY_EXPORT_RETENTION_DAYS=days):
            tasks.cleanup_old_history_exports()

        self.assertKept(older, newest)

    def test_runs_daily(self):
        tasks_by_name = {
            entry["task"]
            for entry in extension.MatrixChatExtension.celery_tasks().values()
        }
        self.assertIn(
            "waldur_mastermind.matrix_chat.cleanup_old_history_exports", tasks_by_name
        )
