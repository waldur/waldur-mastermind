from django.contrib.contenttypes.models import ContentType
from django.utils.functional import cached_property

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.matrix_chat import models


class MatrixChatFixture(ProjectFixture):
    @cached_property
    def matrix_room(self):
        ct = ContentType.objects.get_for_model(self.project)
        return models.MatrixRoom.objects.create(
            room_id="!test_room:matrix.example.com",
            room_name=f"Project: {self.project.name}",
            room_alias=f"#waldur-{self.project.uuid.hex[:8]}:matrix.example.com",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=self.project.id,
            created_by=self.owner,
        )

    @cached_property
    def matrix_user_profile(self):
        return models.MatrixUserProfile.objects.create(
            user=self.admin,
            matrix_user_id=f"@{self.admin.username}:matrix.example.com",
            provisioned=True,
        )

    @cached_property
    def matrix_room_member(self):
        return models.MatrixRoomMember.objects.create(
            room=self.matrix_room,
            user=self.admin,
            matrix_user_id=f"@{self.admin.username}:matrix.example.com",
            power_level=50,
            membership_state=models.MembershipStates.JOINED,
        )

    @cached_property
    def history_export(self):
        return models.MatrixHistoryExport.objects.create(
            room=self.matrix_room,
            export_type=models.ExportTypes.MANUAL,
            state=models.ExportStates.COMPLETED,
            message_count=10,
        )
