from unittest import mock

from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import models, room_provisioning


@mock.patch("waldur_mastermind.matrix_chat.room_provisioning.tasks")
class ProvisionProjectRoomTest(TestCase):
    def test_long_project_name_is_cut_to_the_room_name_column(self, mock_tasks):
        # Project names allow 500 characters, the room name column 255; an
        # uncaught DataError here broke project creation under auto-create.
        project = structure_factories.ProjectFactory(name="x" * 300)

        room = room_provisioning.provision_project_room(project)

        max_length = models.MatrixRoom._meta.get_field("room_name").max_length
        room.refresh_from_db()
        self.assertEqual(room.room_name, "x" * max_length)
        mock_tasks.create_room.delay.assert_called_once_with(str(room.uuid))

    def test_second_call_for_the_same_project_returns_none(self, mock_tasks):
        project = structure_factories.ProjectFactory()

        first = room_provisioning.provision_project_room(project)
        second = room_provisioning.provision_project_room(project)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(models.MatrixRoom.objects.count(), 1)
        mock_tasks.create_room.delay.assert_called_once()
