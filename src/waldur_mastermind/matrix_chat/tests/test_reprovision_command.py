from io import StringIO
from unittest import mock

from constance.test import override_config
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from waldur_mastermind.matrix_chat import models
from waldur_mastermind.matrix_chat.tests import fixtures

ENABLED = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)


def run(**kwargs):
    out = StringIO()
    call_command("reprovision_matrix_rooms", stdout=out, **kwargs)
    return out.getvalue()


@override_config(**ENABLED)
class ReprovisionMatrixRoomsTest(TestCase):
    """The shell-side twin of POST /api/admin/matrix/reprovision/.

    Both call the same function, so these cover the command's own behaviour:
    the guards, the dry run and the confirmation.
    """

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room
        self.profile = self.fixture.matrix_user_profile

    def test_active_rooms_are_reset_for_recreation(self):
        run(yes=True)

        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.CREATING)
        self.assertIsNone(self.room.room_id)
        self.assertEqual(self.room.room_alias, "")

    def test_provisioned_profiles_are_reset(self):
        self.profile.access_token = "syt_old_homeserver_token"
        self.profile.save()

        run(yes=True)

        self.profile.refresh_from_db()
        self.assertFalse(self.profile.provisioned)
        self.assertEqual(self.profile.access_token, "")
        self.assertIsNone(self.profile.provisioned_at)

    def test_each_reset_room_is_queued_for_creation(self):
        with mock.patch(
            "waldur_mastermind.matrix_chat.tasks.create_room.delay"
        ) as queued:
            run(yes=True)

        queued.assert_called_once_with(str(self.room.uuid))

    def test_rooms_that_are_not_active_are_left_alone(self):
        self.room.state = models.RoomStates.ARCHIVED
        self.room.save()

        run(yes=True)

        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
        self.assertEqual(self.room.room_id, "!test_room:matrix.example.com")

    def test_dry_run_reports_the_counts_and_writes_nothing(self):
        output = run(dry_run=True)

        self.assertIn("1 room(s)", output)
        self.assertIn("1 user profile(s)", output)
        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.ACTIVE)

    def test_declining_the_prompt_changes_nothing(self):
        """The ids and tokens it drops can only be replaced by a homeserver, so
        the destructive path is not the default one."""
        with mock.patch("builtins.input", return_value="n"):
            with self.assertRaises(CommandError):
                run()

        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.ACTIVE)

    def test_no_terminal_is_explained_rather_than_a_traceback(self):
        """A Job or cron run has no stdin, and input() raises there.

        It already fails safe, but an EOFError traceback reads like a bug in the
        command rather than a missing flag.
        """
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(CommandError) as ctx:
                run()

        self.assertIn("-y", str(ctx.exception))
        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.ACTIVE)

    def test_confirming_the_prompt_proceeds(self):
        with mock.patch("builtins.input", return_value="y"):
            run()

        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.CREATING)

    @override_config(MATRIX_ENABLED=False)
    def test_it_refuses_to_run_against_a_disabled_integration(self):
        """Queued creation tasks would have no homeserver to talk to, leaving
        every room stuck in 'creating'."""
        with self.assertRaises(CommandError) as ctx:
            run(yes=True)

        self.assertIn("disabled", str(ctx.exception))
        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.ACTIVE)

    @override_config(MATRIX_ENABLED=False)
    def test_dry_run_still_reports_the_counts_when_matrix_is_disabled(self):
        """A dry run queues nothing, so a missing homeserver cannot strand
        anything, and counting before the new one is switched on is a normal
        migration step."""
        output = run(dry_run=True)

        self.assertIn("1 room(s)", output)
        self.assertIn("1 user profile(s)", output)


@override_config(**ENABLED)
class ReprovisionNothingToDoTest(TestCase):
    def test_an_empty_deployment_says_so_instead_of_prompting(self):
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            output = run()

        self.assertIn("Nothing to reprovision", output)
