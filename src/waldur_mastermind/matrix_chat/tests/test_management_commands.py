from io import StringIO
from unittest import mock

from constance.test import override_config
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from waldur_core.structure.models import Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import models

# is_enabled() requires all three; the command refuses to run otherwise.
MATRIX_ENABLED_CONFIG = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="as-token",
)


@override_config(**MATRIX_ENABLED_CONFIG)
@mock.patch("waldur_mastermind.matrix_chat.room_provisioning.tasks")
class ProvisionMatrixRoomsTest(TestCase):
    def _call(self, *args):
        out = StringIO()
        call_command("provision_matrix_rooms", *args, stdout=out)
        return out.getvalue()

    def _room_count(self):
        project_ct = ContentType.objects.get_for_model(Project)
        return models.MatrixRoom.objects.filter(content_type=project_ct).count()

    def test_rooms_are_created_for_projects_without_one(self, mock_tasks):
        projects = [structure_factories.ProjectFactory() for _ in range(3)]

        output = self._call()

        self.assertEqual(self._room_count(), 3)
        self.assertEqual(mock_tasks.create_room.delay.call_count, 3)
        for project in projects:
            self.assertIn(str(project.uuid), output)

    def test_long_project_name_does_not_stop_the_backfill(self, mock_tasks):
        # Ordered by name, so a crash on the first project would repeat on
        # every re-run and strand every project after it.
        structure_factories.ProjectFactory(name="a" * 300)
        structure_factories.ProjectFactory(name="b")

        self._call()

        self.assertEqual(self._room_count(), 2)

    def test_room_created_concurrently_is_reported_as_skipped(self, mock_tasks):
        structure_factories.ProjectFactory()

        with mock.patch(
            "waldur_mastermind.matrix_chat.room_provisioning.provision_project_room",
            return_value=None,
        ):
            output = self._call()

        self.assertIn("room exists", output)
        self.assertIn("skipped 1", output)

    def test_existing_rooms_are_left_alone(self, mock_tasks):
        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        models.MatrixRoom.objects.create(
            room_name="existing",
            content_type=ct,
            object_id=project.id,
        )

        self._call()

        self.assertEqual(self._room_count(), 1)
        mock_tasks.create_room.delay.assert_not_called()

    def test_archived_room_still_blocks_creation(self, mock_tasks):
        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        models.MatrixRoom.objects.create(
            room_name="archived",
            state=models.RoomStates.ARCHIVED,
            content_type=ct,
            object_id=project.id,
        )

        self._call()

        self.assertEqual(self._room_count(), 1)
        mock_tasks.create_room.delay.assert_not_called()

    def test_dry_run_creates_nothing(self, mock_tasks):
        structure_factories.ProjectFactory()

        output = self._call("--dry-run")

        self.assertEqual(self._room_count(), 0)
        mock_tasks.create_room.delay.assert_not_called()
        self.assertIn("would create", output)

    def test_customer_filter_limits_scope(self, mock_tasks):
        target = structure_factories.ProjectFactory()
        structure_factories.ProjectFactory()

        self._call("--customer", target.customer.uuid.hex)

        self.assertEqual(self._room_count(), 1)
        self.assertTrue(
            models.MatrixRoom.objects.filter(object_id=target.id).exists(),
        )

    def test_negative_limit_is_an_error(self, mock_tasks):
        structure_factories.ProjectFactory()

        with self.assertRaises(CommandError):
            self._call("--limit", "-1")

        self.assertEqual(self._room_count(), 0)

    def test_unknown_customer_is_an_error(self, mock_tasks):
        structure_factories.ProjectFactory()

        with self.assertRaises(CommandError):
            self._call("--customer", "0" * 32)

        self.assertEqual(self._room_count(), 0)

    def test_malformed_customer_is_an_error(self, mock_tasks):
        with self.assertRaises(CommandError):
            self._call("--customer", "foo")

        self.assertEqual(self._room_count(), 0)

    def test_limit_caps_the_number_of_rooms(self, mock_tasks):
        for _ in range(3):
            structure_factories.ProjectFactory()

        self._call("--limit", "2")

        self.assertEqual(self._room_count(), 2)

    def test_command_refuses_to_run_while_matrix_is_disabled(self, mock_tasks):
        structure_factories.ProjectFactory()

        with override_config(MATRIX_ENABLED=False):
            with self.assertRaises(CommandError):
                self._call()

        self.assertEqual(self._room_count(), 0)

    def test_dry_run_works_while_matrix_is_disabled(self, mock_tasks):
        structure_factories.ProjectFactory()

        with override_config(MATRIX_ENABLED=False):
            output = self._call("--dry-run")

        self.assertIn("would create", output)
        self.assertEqual(self._room_count(), 0)
