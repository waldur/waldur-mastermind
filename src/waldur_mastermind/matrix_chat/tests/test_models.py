from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import models


class MatrixUserProfileTest(TestCase):
    def test_create_profile(self):
        user = structure_factories.UserFactory()
        profile = models.MatrixUserProfile.objects.create(
            user=user,
            matrix_user_id=f"@{user.username}:matrix.example.com",
        )
        self.assertFalse(profile.provisioned)
        self.assertIsNone(profile.provisioned_at)

    def test_mark_provisioned(self):
        user = structure_factories.UserFactory()
        profile = models.MatrixUserProfile.objects.create(
            user=user,
            matrix_user_id=f"@{user.username}:matrix.example.com",
        )
        profile.mark_provisioned()
        profile.refresh_from_db()
        self.assertTrue(profile.provisioned)
        self.assertIsNotNone(profile.provisioned_at)

    def test_unique_matrix_user_id(self):
        user1 = structure_factories.UserFactory()
        user2 = structure_factories.UserFactory()
        models.MatrixUserProfile.objects.create(
            user=user1,
            matrix_user_id="@shared_id:matrix.example.com",
        )
        with self.assertRaises(Exception):
            models.MatrixUserProfile.objects.create(
                user=user2,
                matrix_user_id="@shared_id:matrix.example.com",
            )


class MatrixRoomTest(TestCase):
    def test_create_room_for_project(self):
        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            content_type=ct,
            object_id=project.id,
        )
        self.assertEqual(room.scope, project)
        self.assertEqual(room.project, project)
        self.assertEqual(room.state, models.RoomStates.CREATING)

    def test_multiple_unprovisioned_rooms_do_not_collide(self):
        project1 = structure_factories.ProjectFactory()
        project2 = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project1)

        room1 = models.MatrixRoom.objects.create(
            room_name="Room 1", content_type=ct, object_id=project1.id
        )
        room2 = models.MatrixRoom.objects.create(
            room_name="Room 2", content_type=ct, object_id=project2.id
        )

        self.assertIsNone(room1.room_id)
        self.assertIsNone(room2.room_id)

    def test_unique_scope_constraint(self):
        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        models.MatrixRoom.objects.create(
            room_id="!test1:matrix.example.com",
            room_name="Test Room 1",
            content_type=ct,
            object_id=project.id,
        )
        with self.assertRaises(Exception):
            models.MatrixRoom.objects.create(
                room_id="!test2:matrix.example.com",
                room_name="Test Room 2",
                content_type=ct,
                object_id=project.id,
            )


class MatrixRoomMemberTest(TestCase):
    def test_create_member(self):
        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            content_type=ct,
            object_id=project.id,
        )
        member = models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id=f"@{user.username}:matrix.example.com",
        )
        self.assertEqual(member.membership_state, models.MembershipStates.INVITED)
        self.assertEqual(member.power_level, 0)


class MatrixHistoryExportTest(TestCase):
    def test_create_export(self):
        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            content_type=ct,
            object_id=project.id,
        )
        export = models.MatrixHistoryExport.objects.create(
            room=room,
            export_type=models.ExportTypes.MANUAL,
        )
        self.assertEqual(export.state, models.ExportStates.PENDING)
        self.assertEqual(export.message_count, 0)
