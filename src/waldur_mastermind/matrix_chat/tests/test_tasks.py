import json
import zipfile
from io import BytesIO
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import models, tasks
from waldur_mastermind.matrix_chat.tests import fixtures


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class CreateRoomTaskTest(TestCase):
    def test_creates_room_and_updates_state(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.create_room.return_value = ("!new_room:matrix.example.com", True)

        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_name="Test Project",
            content_type=ct,
            object_id=project.id,
        )

        with mock.patch("waldur_mastermind.matrix_chat.tasks.config") as mock_config:
            mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
            tasks.create_room(str(room.uuid))

        room.refresh_from_db()
        self.assertEqual(room.room_id, "!new_room:matrix.example.com")
        self.assertEqual(room.state, models.RoomStates.ACTIVE)

    def test_sets_error_state_on_failure(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.create_room.side_effect = Exception("Matrix error")

        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_name="Test Project",
            content_type=ct,
            object_id=project.id,
        )

        tasks.create_room(str(room.uuid))

        room.refresh_from_db()
        self.assertEqual(room.state, models.RoomStates.ERROR)
        self.assertIn("Matrix error", room.error_message)

    def test_projects_sharing_a_uuid_prefix_get_distinct_aliases(self, mock_client):
        # Sequential UUIDs (demo presets) share their leading characters; an
        # alias built from a prefix collided and the second room got none.
        mock_client.is_enabled.return_value = True

        aliases = []
        for uuid in (
            "c3000000000000000000000000000001",
            "c3000000000000000000000000000002",
        ):
            project = structure_factories.ProjectFactory(uuid=uuid)
            room = models.MatrixRoom.objects.create(
                room_name=project.name,
                content_type=ContentType.objects.get_for_model(project),
                object_id=project.id,
            )
            mock_client.create_room.return_value = (f"!{uuid}:example.com", True)
            tasks.create_room(str(room.uuid))
            aliases.append(mock_client.create_room.call_args.kwargs["alias_localpart"])

        self.assertEqual(
            aliases,
            [
                "waldur-c3000000000000000000000000000001",
                "waldur-c3000000000000000000000000000002",
            ],
        )

    def test_second_dispatch_leaves_the_created_room_alone(self, mock_client):
        # Retry is offered on a room that has sat in `creating` for a few
        # minutes, which a backfill queue makes common, so the first task may
        # still be pending when a second one is queued.
        mock_client.is_enabled.return_value = True
        mock_client.create_room.return_value = ("!first:matrix.example.com", True)

        project = structure_factories.ProjectFactory()
        room = models.MatrixRoom.objects.create(
            room_name="Test Project",
            content_type=ContentType.objects.get_for_model(project),
            object_id=project.id,
        )

        tasks.create_room(str(room.uuid))
        tasks.create_room(str(room.uuid))

        room.refresh_from_db()
        self.assertEqual(room.state, models.RoomStates.ACTIVE)
        self.assertEqual(room.room_id, "!first:matrix.example.com")
        mock_client.create_room.assert_called_once()

    def test_room_of_a_removed_project_is_not_provisioned(self, mock_client):
        # A project removed while its room waited in the queue has already
        # passed pre_delete, so nothing would ever archive a room made now.
        mock_client.is_enabled.return_value = True

        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_name="Test Project",
            content_type=ct,
            object_id=project.id,
        )
        project.delete()

        tasks.create_room(str(room.uuid))

        room.refresh_from_db()
        self.assertEqual(room.state, models.RoomStates.ERROR)
        self.assertEqual(
            room.error_message,
            "The project was removed before its room was created, "
            "so retrying cannot succeed.",
        )
        mock_client.create_room.assert_not_called()

    def test_skips_when_disabled(self, mock_client):
        mock_client.is_enabled.return_value = False

        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_name="Test Project",
            content_type=ct,
            object_id=project.id,
        )

        tasks.create_room(str(room.uuid))

        room.refresh_from_db()
        self.assertEqual(room.state, models.RoomStates.CREATING)
        mock_client.create_room.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class InviteUserTaskTest(TestCase):
    def test_invites_user_to_room(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.return_value = "@alice:matrix.example.com"
        mock_client.get_power_level_for_scope.return_value = 0

        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory(username="alice")
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

        tasks.invite_user_to_room(str(room.uuid), str(user.uuid))

        mock_client.invite_user.assert_called_once_with(
            "!test:matrix.example.com", "@alice:matrix.example.com"
        )
        # The appservice accepts the invite on the user's behalf, so no user
        # access token (and no Matrix device) is created for the join.
        mock_client.join_room_as_user.assert_called_once_with(
            "!test:matrix.example.com", "@alice:matrix.example.com"
        )
        member = models.MatrixRoomMember.objects.get(room=room, user=user)
        self.assertEqual(member.membership_state, models.MembershipStates.JOINED)

    def test_invite_falls_back_to_invited_when_auto_join_fails(self, mock_client):
        # When the join fails, the row is recorded as INVITED — the bot's
        # invite is the durable side effect, and the user can accept later.
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.return_value = "@alice:matrix.example.com"
        mock_client.get_power_level_for_scope.return_value = 0
        mock_client.join_room_as_user.side_effect = RuntimeError("join refused")

        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory(username="alice")
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

        tasks.invite_user_to_room(str(room.uuid), str(user.uuid))

        mock_client.invite_user.assert_called_once_with(
            "!test:matrix.example.com", "@alice:matrix.example.com"
        )
        member = models.MatrixRoomMember.objects.get(room=room, user=user)
        self.assertEqual(member.membership_state, models.MembershipStates.INVITED)

    def test_sets_power_level_for_admin(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.return_value = "@admin:matrix.example.com"
        mock_client.get_power_level_for_scope.return_value = 50

        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory(username="admin")
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

        tasks.invite_user_to_room(str(room.uuid), str(user.uuid))

        mock_client.set_power_level.assert_called_once_with(
            "!test:matrix.example.com", "@admin:matrix.example.com", 50
        )


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class KickUserTaskTest(TestCase):
    def test_kicks_user_from_room(self, mock_client):
        mock_client.is_enabled.return_value = True

        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )
        member = models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id="@user:matrix.example.com",
            membership_state=models.MembershipStates.JOINED,
        )

        tasks.kick_user_from_room(str(room.uuid), str(user.uuid))

        mock_client.kick_user.assert_called_once_with(
            "!test:matrix.example.com",
            "@user:matrix.example.com",
            reason="Role revoked in Waldur",
        )
        member.refresh_from_db()
        self.assertEqual(member.membership_state, models.MembershipStates.LEFT)

    def test_skips_when_user_not_member(self, mock_client):
        mock_client.is_enabled.return_value = True

        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

        tasks.kick_user_from_room(str(room.uuid), str(user.uuid))
        mock_client.kick_user.assert_not_called()

    def test_kicks_user_via_profile_when_no_member_record(self, mock_client):
        # A MatrixRoomMember row can be missing while the user is still joined
        # on the homeserver, so the kick must fall back to their Matrix profile.
        mock_client.is_enabled.return_value = True

        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )
        models.MatrixUserProfile.objects.create(
            user=user,
            matrix_user_id="@user:matrix.example.com",
        )

        tasks.kick_user_from_room(str(room.uuid), str(user.uuid))

        mock_client.kick_user.assert_called_once_with(
            "!test:matrix.example.com",
            "@user:matrix.example.com",
            reason="Role revoked in Waldur",
        )

    def test_retries_when_kick_fails(self, mock_client):
        # A failed kick must propagate so Celery retries it; swallowing the
        # error would silently leave a revoked user with chat access.
        mock_client.is_enabled.return_value = True
        mock_client.kick_user.side_effect = RuntimeError("Matrix unavailable")

        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )
        models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id="@user:matrix.example.com",
            membership_state=models.MembershipStates.JOINED,
        )

        with self.assertRaises(RuntimeError):
            tasks.kick_user_from_room(str(room.uuid), str(user.uuid))

    def test_kicks_deactivated_user(self, mock_client):
        # Deactivation IS the revocation trigger — User.objects (the active
        # manager) would silently skip the row and leave the deactivated user
        # joined with a live token.
        mock_client.is_enabled.return_value = True

        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory(is_active=False)
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )
        models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id="@deactivated:matrix.example.com",
            membership_state=models.MembershipStates.JOINED,
        )

        tasks.kick_user_from_room(str(room.uuid), str(user.uuid))

        mock_client.kick_user.assert_called_once_with(
            "!test:matrix.example.com",
            "@deactivated:matrix.example.com",
            reason="Role revoked in Waldur",
        )


@mock.patch("waldur_mastermind.matrix_chat.tasks.config")
@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class DisableRoomTaskTest(TestCase):
    def _disabling_room(self):
        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.DISABLING,
            content_type=ct,
            object_id=project.id,
        )
        return room

    def test_deactivation_message_has_no_attribution(self, mock_client, mock_config):
        mock_client.is_enabled.return_value = True
        mock_config.MATRIX_HISTORY_EXPORT_ENABLED = False
        room = self._disabling_room()

        tasks.disable_room(str(room.uuid))

        mock_client.send_message.assert_called_once_with(
            "!test:matrix.example.com", "Chat room was deactivated"
        )

    def test_deactivation_message_includes_reason(self, mock_client, mock_config):
        mock_client.is_enabled.return_value = True
        mock_config.MATRIX_HISTORY_EXPORT_ENABLED = False
        room = self._disabling_room()

        tasks.disable_room(str(room.uuid), reason="project termination")

        mock_client.send_message.assert_called_once_with(
            "!test:matrix.example.com",
            "Chat room was deactivated due to project termination",
        )

    def test_no_export_when_discarding_history(self, mock_client, mock_config):
        # The caller asked to drop history, so the on-deletion export must be
        # skipped entirely rather than created and then deleted.
        mock_client.is_enabled.return_value = True
        mock_config.MATRIX_HISTORY_EXPORT_ENABLED = True
        room = self._disabling_room()

        tasks.disable_room(str(room.uuid), delete_history=True)

        self.assertFalse(room.exports.exists())


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class SendRoomNotificationTaskTest(TestCase):
    def test_sends_message_to_active_room(self, mock_client):
        mock_client.is_enabled.return_value = True

        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

        tasks.send_room_notification(str(room.uuid), "Hello from Waldur")

        mock_client.send_message.assert_called_once_with(
            "!test:matrix.example.com", "Hello from Waldur"
        )

    def test_skips_when_disabled(self, mock_client):
        mock_client.is_enabled.return_value = False

        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

        tasks.send_room_notification(str(room.uuid), "Hello")
        mock_client.send_message.assert_not_called()

    def test_skips_inactive_room(self, mock_client):
        mock_client.is_enabled.return_value = True

        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ARCHIVED,
            content_type=ct,
            object_id=project.id,
        )

        tasks.send_room_notification(str(room.uuid), "Hello")
        mock_client.send_message.assert_not_called()

    def test_handles_send_failure(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.send_message.side_effect = Exception("Send failed")

        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

        # Should not raise
        tasks.send_room_notification(str(room.uuid), "Hello")


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class ExportRoomHistoryTaskTest(TestCase):
    def _create_room_and_export(self):
        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )
        export = models.MatrixHistoryExport.objects.create(
            room=room,
            export_type=models.ExportTypes.MANUAL,
        )
        return room, export

    @mock.patch("waldur_mastermind.matrix_chat.tasks.config")
    def test_exports_messages(self, mock_config, mock_client):
        mock_config.MATRIX_EXPORT_MEDIA = True
        mock_client.is_enabled.return_value = True
        mock_client.get_room_messages.side_effect = [
            {
                "messages": [
                    {
                        "event_id": "$1",
                        "sender": "@alice:matrix.example.com",
                        "body": "Hello",
                        "timestamp": 1234567890,
                        "type": "m.room.message",
                        "msgtype": "m.text",
                    }
                ],
                "end_token": "token_1",
            },
            {"messages": [], "end_token": None},
        ]

        room, export = self._create_room_and_export()
        tasks.export_room_history(str(export.uuid))

        export.refresh_from_db()
        self.assertEqual(export.state, models.ExportStates.COMPLETED)
        self.assertEqual(export.message_count, 1)
        self.assertEqual(export.media_count, 0)
        self.assertTrue(export.export_file)
        self.assertFalse(export.media_file)

    def test_handles_export_failure(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.get_room_messages.side_effect = Exception("API error")

        room, export = self._create_room_and_export()
        tasks.export_room_history(str(export.uuid))

        export.refresh_from_db()
        self.assertEqual(export.state, models.ExportStates.FAILED)
        self.assertIn("API error", export.error_message)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.config")
    def test_exports_with_media(self, mock_config, mock_client):
        mock_config.MATRIX_EXPORT_MEDIA = True
        mock_client.is_enabled.return_value = True
        mock_client.get_room_messages.side_effect = [
            {
                "messages": [
                    {
                        "event_id": "$img1",
                        "sender": "@alice:matrix.example.com",
                        "body": "photo.jpg",
                        "timestamp": 1234567890,
                        "type": "m.room.message",
                        "msgtype": "m.image",
                        "has_media": True,
                        "media_url": "mxc://example.com/media123",
                        "media_info": {"mimetype": "image/jpeg", "size": 5},
                    },
                    {
                        "event_id": "$txt1",
                        "sender": "@bob:matrix.example.com",
                        "body": "Nice photo!",
                        "timestamp": 1234567891,
                        "type": "m.room.message",
                        "msgtype": "m.text",
                    },
                ],
                "end_token": "token_1",
            },
            {"messages": [], "end_token": None},
        ]
        mock_client.download_media.return_value = (
            b"fake-image-data",
            "image/jpeg",
            "photo.jpg",
        )

        room, export = self._create_room_and_export()
        tasks.export_room_history(str(export.uuid))

        export.refresh_from_db()
        self.assertEqual(export.state, models.ExportStates.COMPLETED)
        self.assertEqual(export.message_count, 2)
        self.assertEqual(export.media_count, 1)
        self.assertTrue(export.media_file)

        # Verify ZIP contents
        export.media_file.seek(0)
        with zipfile.ZipFile(BytesIO(export.media_file.read())) as zf:
            names = zf.namelist()
            self.assertEqual(len(names), 1)
            self.assertIn("photo.jpg", names[0])
            self.assertEqual(zf.read(names[0]), b"fake-image-data")

        # Verify JSON has media_path
        export.export_file.seek(0)
        export_data = json.loads(export.export_file.read())
        img_msg = [m for m in export_data["messages"] if m["event_id"] == "$img1"][0]
        self.assertIn("media_path", img_msg)
        self.assertEqual(export_data["media_count"], 1)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.config")
    def test_media_download_failure_does_not_fail_export(
        self, mock_config, mock_client
    ):
        mock_config.MATRIX_EXPORT_MEDIA = True
        mock_client.is_enabled.return_value = True
        mock_client.get_room_messages.side_effect = [
            {
                "messages": [
                    {
                        "event_id": "$img1",
                        "sender": "@alice:matrix.example.com",
                        "body": "photo.jpg",
                        "timestamp": 1234567890,
                        "type": "m.room.message",
                        "msgtype": "m.image",
                        "has_media": True,
                        "media_url": "mxc://example.com/broken",
                        "media_info": {},
                    },
                ],
                "end_token": None,
            },
        ]
        mock_client.download_media.side_effect = Exception("download failed")

        room, export = self._create_room_and_export()
        tasks.export_room_history(str(export.uuid))

        export.refresh_from_db()
        self.assertEqual(export.state, models.ExportStates.COMPLETED)
        self.assertEqual(export.message_count, 1)
        self.assertEqual(export.media_count, 0)
        self.assertFalse(export.media_file)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.config")
    def test_media_skipped_when_disabled(self, mock_config, mock_client):
        mock_config.MATRIX_EXPORT_MEDIA = False
        mock_client.is_enabled.return_value = True
        mock_client.get_room_messages.side_effect = [
            {
                "messages": [
                    {
                        "event_id": "$img1",
                        "sender": "@alice:matrix.example.com",
                        "body": "photo.jpg",
                        "timestamp": 1234567890,
                        "type": "m.room.message",
                        "msgtype": "m.image",
                        "has_media": True,
                        "media_url": "mxc://example.com/media123",
                        "media_info": {},
                    },
                ],
                "end_token": None,
            },
        ]

        room, export = self._create_room_and_export()
        tasks.export_room_history(str(export.uuid))

        export.refresh_from_db()
        self.assertEqual(export.state, models.ExportStates.COMPLETED)
        self.assertEqual(export.media_count, 0)
        mock_client.download_media.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class StaffJoinRoomTaskTest(TestCase):
    def _make_room(self):
        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        return models.MatrixRoom.objects.create(
            room_id="!staff:matrix.example.com",
            room_name="Staff Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

    def test_join_sets_moderator_badge_and_announces(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.return_value = "@staff:matrix.example.com"

        room = self._make_room()
        user = structure_factories.UserFactory(
            username="staff", is_staff=True, full_name="Staff Member"
        )

        tasks.staff_join_room(str(room.uuid), str(user.uuid))

        mock_client.invite_user.assert_called_once_with(
            "!staff:matrix.example.com", "@staff:matrix.example.com"
        )
        mock_client.join_room_as_user.assert_called_once_with(
            "!staff:matrix.example.com", "@staff:matrix.example.com"
        )
        mock_client.set_power_level.assert_called_once_with(
            "!staff:matrix.example.com", "@staff:matrix.example.com", 50
        )
        member = models.MatrixRoomMember.objects.get(room=room, user=user)
        self.assertEqual(member.power_level, 50)
        self.assertEqual(member.membership_state, models.MembershipStates.JOINED)
        mock_client.send_message.assert_called_once_with(
            "!staff:matrix.example.com", "Staff Member joined the room."
        )

    def test_join_left_invited_when_join_fails(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.return_value = "@staff:matrix.example.com"
        mock_client.join_room_as_user.side_effect = RuntimeError("join refused")
        room = self._make_room()
        user = structure_factories.UserFactory(username="staff", is_staff=True)

        tasks.staff_join_room(str(room.uuid), str(user.uuid))

        member = models.MatrixRoomMember.objects.get(room=room, user=user)
        self.assertEqual(member.membership_state, models.MembershipStates.INVITED)

    def test_join_skips_inactive_room(self, mock_client):
        mock_client.is_enabled.return_value = True
        room = self._make_room()
        room.state = models.RoomStates.ARCHIVED
        room.save(update_fields=["state"])
        user = structure_factories.UserFactory(is_staff=True)

        tasks.staff_join_room(str(room.uuid), str(user.uuid))

        mock_client.invite_user.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class StaffLeaveRoomTaskTest(TestCase):
    def test_leave_announces_then_leaves_and_marks_left(self, mock_client):
        mock_client.is_enabled.return_value = True

        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!staff:matrix.example.com",
            room_name="Staff Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )
        user = structure_factories.UserFactory(
            username="staff", is_staff=True, full_name="Staff Member"
        )
        member = models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id="@staff:matrix.example.com",
            power_level=50,
            membership_state=models.MembershipStates.JOINED,
        )

        tasks.staff_leave_room(str(room.uuid), str(user.uuid))

        mock_client.send_message.assert_called_once_with(
            "!staff:matrix.example.com", "Staff Member left the room."
        )
        mock_client.leave_room_as_user.assert_called_once_with(
            "!staff:matrix.example.com", "@staff:matrix.example.com"
        )
        member.refresh_from_db()
        self.assertEqual(member.membership_state, models.MembershipStates.LEFT)


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class SyncProjectMembersJoinTest(TestCase):
    def test_member_left_invited_when_join_fails(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.side_effect = (
            lambda user: f"@{user.username}:matrix.example.com"
        )
        mock_client.get_power_level_for_scope.return_value = 0
        mock_client.join_room_as_user.side_effect = RuntimeError("join refused")
        fixture = fixtures.MatrixChatFixture()
        admin = fixture.admin
        room = fixture.matrix_room

        tasks.sync_project_members_to_room(str(room.uuid))

        member = models.MatrixRoomMember.objects.get(room=room, user=admin)
        self.assertEqual(member.membership_state, models.MembershipStates.INVITED)

    def test_project_member_is_joined_through_appservice(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.return_value = "@admin:matrix.example.com"
        mock_client.get_power_level_for_scope.return_value = 0
        fixture = fixtures.MatrixChatFixture()
        admin = fixture.admin
        room = fixture.matrix_room

        tasks.sync_project_members_to_room(str(room.uuid))

        mock_client.join_room_as_user.assert_any_call(
            room.room_id, "@admin:matrix.example.com"
        )
        member = models.MatrixRoomMember.objects.get(room=room, user=admin)
        self.assertEqual(member.membership_state, models.MembershipStates.JOINED)


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class SyncDoesNotKickStaffTest(TestCase):
    def test_staff_member_survives_sync(self, mock_client):
        # A staff member self-joined the room but has no project/customer role,
        # so the stale-member sweep would otherwise kick them.
        mock_client.is_enabled.return_value = True

        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!sync:matrix.example.com",
            room_name="Sync Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )
        staff = structure_factories.UserFactory(is_staff=True)
        models.MatrixRoomMember.objects.create(
            room=room,
            user=staff,
            matrix_user_id="@staff:matrix.example.com",
            power_level=50,
            membership_state=models.MembershipStates.JOINED,
        )

        tasks.sync_project_members_to_room(str(room.uuid))

        mock_client.kick_user.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class BotCommandSenderAuthTest(TestCase):
    """!status/!orders/!members surface project-scoped data; dispatch must be
    gated on the sender mapping to a Waldur user with an active project role."""

    def _build_room_and_event(self, *, sender_id, body="!status"):
        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!room:matrix.example.com",
            room_name="Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )
        event = {
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": body},
            "sender": sender_id,
            "room_id": room.room_id,
            "event_id": "$evt1:matrix.example.com",
        }
        return project, room, event

    def test_command_from_authorized_user_dispatches(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.get_bot_user_id.return_value = "@waldur-bot:matrix.example.com"

        project, room, event = self._build_room_and_event(
            sender_id="@member:matrix.example.com"
        )
        user = structure_factories.UserFactory()
        models.MatrixUserProfile.objects.create(
            user=user,
            matrix_user_id="@member:matrix.example.com",
        )
        from waldur_core.permissions.fixtures import ProjectRole

        project.add_user(user, ProjectRole.MANAGER)

        with mock.patch(
            "waldur_mastermind.matrix_chat.tasks.handle_bot_command.delay"
        ) as mock_dispatch:
            tasks.process_appservice_events("txn1", [event])

        mock_dispatch.assert_called_once_with(
            room.room_id, "@member:matrix.example.com", event["event_id"], "status"
        )
        mock_client.send_reply.assert_not_called()

    def test_command_from_unknown_sender_is_denied(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.get_bot_user_id.return_value = "@waldur-bot:matrix.example.com"

        _, _, event = self._build_room_and_event(
            sender_id="@stranger:other.example.com"
        )

        with mock.patch(
            "waldur_mastermind.matrix_chat.tasks.handle_bot_command.delay"
        ) as mock_dispatch:
            tasks.process_appservice_events("txn2", [event])

        mock_dispatch.assert_not_called()
        # Friendly reply rather than silence — the sender should know why.
        mock_client.send_reply.assert_called_once()


class CleanupAppserviceTransactionsTest(TestCase):
    def test_prunes_old_rows_only(self):
        from datetime import timedelta

        from django.utils import timezone

        recent = models.MatrixAppserviceTransaction.objects.create(
            txn_id="recent",
            event_count=1,
        )
        old = models.MatrixAppserviceTransaction.objects.create(
            txn_id="old",
            event_count=1,
        )
        # auto_now_add means processed_at is already 'now' — backdate the old
        # row via direct queryset update so the cleanup filter matches it.
        cutoff_aged = timezone.now() - timedelta(days=60)
        models.MatrixAppserviceTransaction.objects.filter(pk=old.pk).update(
            processed_at=cutoff_aged
        )

        result = tasks.cleanup_old_appservice_transactions()

        self.assertEqual(result["deleted_count"], 1)
        self.assertTrue(
            models.MatrixAppserviceTransaction.objects.filter(pk=recent.pk).exists()
        )
        self.assertFalse(
            models.MatrixAppserviceTransaction.objects.filter(pk=old.pk).exists()
        )


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class PruneWebDevicesTaskTest(TestCase):
    def test_logs_out_stale_web_devices(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.list_devices.return_value = [{"device_id": "WALDUR_WEB_A"}]
        mock_client.stale_web_devices.return_value = ["WALDUR_WEB_A", "WALDUR_WEB_B"]

        tasks.prune_web_devices("@alice:matrix.example.com", "WALDUR_WEB_NEW")

        self.assertEqual(
            mock_client.stale_web_devices.call_args.kwargs["keep_device_id"],
            "WALDUR_WEB_NEW",
        )

        mock_client.logout_device.assert_has_calls(
            [
                mock.call("@alice:matrix.example.com", "WALDUR_WEB_A"),
                mock.call("@alice:matrix.example.com", "WALDUR_WEB_B"),
            ]
        )

    def test_one_failed_logout_does_not_stop_the_rest(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.stale_web_devices.return_value = ["WALDUR_WEB_A", "WALDUR_WEB_B"]
        mock_client.logout_device.side_effect = [RuntimeError("boom"), None]

        tasks.prune_web_devices("@alice:matrix.example.com")

        self.assertEqual(mock_client.logout_device.call_count, 2)

    def test_skips_when_disabled(self, mock_client):
        mock_client.is_enabled.return_value = False

        tasks.prune_web_devices("@alice:matrix.example.com")

        mock_client.list_devices.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class EndWebSessionsTaskTest(TestCase):
    def test_signs_out_web_devices_of_deactivated_user(self, mock_client):
        # Deactivation is the trigger, so the active manager would miss the user.
        mock_client.is_homeserver_configured.return_value = True
        user = structure_factories.UserFactory(is_active=False)
        models.MatrixUserProfile.objects.create(
            user=user, matrix_user_id="@gone:matrix.example.com", provisioned=True
        )

        tasks.end_web_sessions(user.uuid.hex)

        mock_client.logout_web_devices.assert_called_once_with(
            "@gone:matrix.example.com"
        )

    def test_user_never_provisioned_has_nothing_to_end(self, mock_client):
        mock_client.is_homeserver_configured.return_value = True
        user = structure_factories.UserFactory(is_active=False)

        tasks.end_web_sessions(user.uuid.hex)

        mock_client.logout_web_devices.assert_not_called()

    def test_runs_while_chat_is_switched_off(self, mock_client):
        mock_client.is_enabled.return_value = False
        mock_client.is_homeserver_configured.return_value = True
        user = structure_factories.UserFactory(is_active=False)
        models.MatrixUserProfile.objects.create(
            user=user, matrix_user_id="@gone:matrix.example.com", provisioned=True
        )

        tasks.end_web_sessions(user.uuid.hex)

        mock_client.logout_web_devices.assert_called_once_with(
            "@gone:matrix.example.com"
        )

    def test_skips_without_a_homeserver(self, mock_client):
        mock_client.is_homeserver_configured.return_value = False
        user = structure_factories.UserFactory(is_active=False)
        models.MatrixUserProfile.objects.create(
            user=user, matrix_user_id="@gone:matrix.example.com", provisioned=True
        )

        tasks.end_web_sessions(user.uuid.hex)

        mock_client.logout_web_devices.assert_not_called()

    def test_reactivated_user_keeps_sessions(self, mock_client):
        # The task retries for minutes; a user reactivated meanwhile may have
        # started new sessions that must survive.
        mock_client.is_homeserver_configured.return_value = True
        user = structure_factories.UserFactory(is_active=True)
        models.MatrixUserProfile.objects.create(
            user=user, matrix_user_id="@back:matrix.example.com", provisioned=True
        )

        tasks.end_web_sessions(user.uuid.hex)

        mock_client.logout_web_devices.assert_not_called()


class RevocationRetryTest(TestCase):
    def test_revocations_keep_retrying_through_a_homeserver_restart(self):
        # retry_backoff=True starts at 1 s, so three retries are spent within
        # seconds; revocations must outlast a homeserver restart.
        for task in (
            tasks.end_web_sessions,
            tasks.sign_out_web_devices,
            tasks.kick_user_from_room,
        ):
            self.assertGreaterEqual(task.retry_backoff, 30, task.name)
            self.assertGreaterEqual(task.max_retries, 5, task.name)


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class SignOutWebDevicesTaskTest(TestCase):
    def test_signs_out_web_devices_by_matrix_id(self, mock_client):
        mock_client.is_homeserver_configured.return_value = True

        tasks.sign_out_web_devices("@gone:matrix.example.com")

        mock_client.logout_web_devices.assert_called_once_with(
            "@gone:matrix.example.com"
        )

    def test_runs_while_chat_is_switched_off(self, mock_client):
        mock_client.is_enabled.return_value = False
        mock_client.is_homeserver_configured.return_value = True

        tasks.sign_out_web_devices("@gone:matrix.example.com")

        mock_client.logout_web_devices.assert_called_once_with(
            "@gone:matrix.example.com"
        )

    def test_skips_without_a_homeserver(self, mock_client):
        mock_client.is_homeserver_configured.return_value = False

        tasks.sign_out_web_devices("@gone:matrix.example.com")

        mock_client.logout_web_devices.assert_not_called()
