from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import handlers, matrix_client, models, tasks
from waldur_mastermind.matrix_chat.tests import fixtures


def _room(project, room_id="!room:matrix.example.com"):
    return models.MatrixRoom.objects.create(
        room_id=room_id,
        room_name=project.name,
        state=models.RoomStates.ACTIVE,
        content_type=ContentType.objects.get_for_model(project),
        object_id=project.id,
    )


def _member(room, user, manually_joined=False):
    return models.MatrixRoomMember.objects.create(
        room=room,
        user=user,
        matrix_user_id=f"@{user.username}:matrix.example.com",
        membership_state=models.MembershipStates.JOINED,
        manually_joined=manually_joined,
    )


def _profile(user):
    return models.MatrixUserProfile.objects.create(
        user=user,
        matrix_user_id=f"@{user.username}:matrix.example.com",
        provisioned=True,
    )


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class SyncRevocationTest(TestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.room = _room(self.project)

    def _sync(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.side_effect = (
            lambda user: f"@{user.username}:matrix.example.com"
        )
        mock_client.get_power_level_for_scope.return_value = 0
        tasks.sync_project_members_to_room(str(self.room.uuid))

    def test_deactivated_member_is_not_rejoined_and_is_kicked(self, mock_client):
        # Deactivation leaves the user's roles in place, so sync used to
        # re-invite and re-join a deactivated user on every run.
        user = structure_factories.UserFactory()
        self.project.add_user(user, ProjectRole.MEMBER)
        member = _member(self.room, user)
        user.is_active = False
        user.save()

        self._sync(mock_client)

        mock_client.ensure_user_exists.assert_not_called()
        mock_client.kick_user.assert_called_once_with(
            self.room.room_id, member.matrix_user_id, reason=mock.ANY
        )
        member.refresh_from_db()
        self.assertEqual(member.membership_state, models.MembershipStates.LEFT)

    def test_staff_who_joined_with_the_button_stays(self, mock_client):
        staff = structure_factories.UserFactory(is_staff=True)
        _member(self.room, staff, manually_joined=True)

        self._sync(mock_client)

        mock_client.kick_user.assert_not_called()

    def test_demoted_staff_who_joined_with_the_button_is_kicked(self, mock_client):
        former_staff = structure_factories.UserFactory(is_staff=False)
        _member(self.room, former_staff, manually_joined=True)

        self._sync(mock_client)

        mock_client.kick_user.assert_called_once()

    def test_staff_without_a_role_who_did_not_join_with_the_button_is_kicked(
        self, mock_client
    ):
        # Only the Join button exempts staff now, not being staff as such.
        staff = structure_factories.UserFactory(is_staff=True)
        _member(self.room, staff, manually_joined=False)

        self._sync(mock_client)

        mock_client.kick_user.assert_called_once()


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class StaffJoinMarksMembershipTest(TestCase):
    def test_staff_join_marks_the_membership_as_manual(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.return_value = "@staff:matrix.example.com"
        fixture = fixtures.MatrixChatFixture()
        staff = structure_factories.UserFactory(is_staff=True)

        tasks.staff_join_room(str(fixture.matrix_room.uuid), str(staff.uuid))

        member = models.MatrixRoomMember.objects.get(
            room=fixture.matrix_room, user=staff
        )
        self.assertTrue(member.manually_joined)


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class EndMatrixAccessTest(TestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.room = _room(self.project)
        self.user = structure_factories.UserFactory(is_active=False)
        self.profile = _profile(self.user)
        self.member = _member(self.room, self.user)

    def test_signs_out_every_device_and_leaves_every_room(self, mock_client):
        # Element sessions too, not just Waldur's web devices: a deactivated
        # user keeps no way into the rooms.
        mock_client.is_homeserver_configured.return_value = True

        tasks.end_matrix_access(self.user.uuid.hex)

        mock_client.logout_all_devices.assert_called_once_with(
            self.profile.matrix_user_id
        )
        mock_client.kick_user.assert_called_once_with(
            self.room.room_id, self.member.matrix_user_id, reason=mock.ANY
        )
        self.member.refresh_from_db()
        self.assertEqual(self.member.membership_state, models.MembershipStates.LEFT)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.kick_member")
    def test_a_failed_kick_is_retried_and_not_recorded_as_left(
        self, mock_kick_member, mock_client
    ):
        mock_client.is_homeserver_configured.return_value = True
        mock_client.kick_user.side_effect = matrix_client.MatrixClientError("503")

        tasks.end_matrix_access(self.user.uuid.hex)

        self.member.refresh_from_db()
        self.assertEqual(self.member.membership_state, models.MembershipStates.JOINED)
        mock_kick_member.delay.assert_called_once_with(self.member.uuid.hex, mock.ANY)

    def test_reactivated_user_keeps_access(self, mock_client):
        mock_client.is_homeserver_configured.return_value = True
        self.user.is_active = True
        self.user.save()

        tasks.end_matrix_access(self.user.uuid.hex)

        mock_client.logout_all_devices.assert_not_called()
        mock_client.kick_user.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class KickMemberTest(TestCase):
    def test_kicks_and_records_the_member_as_left(self, mock_client):
        mock_client.is_homeserver_configured.return_value = True
        fixture = fixtures.MatrixChatFixture()
        member = fixture.matrix_room_member
        fixture.matrix_room.state = models.RoomStates.DISABLING
        fixture.matrix_room.save()

        tasks.kick_member(member.uuid.hex, "Chat room was deactivated")

        mock_client.kick_user.assert_called_once_with(
            fixture.matrix_room.room_id,
            member.matrix_user_id,
            reason="Chat room was deactivated",
        )
        member.refresh_from_db()
        self.assertEqual(member.membership_state, models.MembershipStates.LEFT)

    def test_a_member_who_already_left_is_not_kicked(self, mock_client):
        mock_client.is_homeserver_configured.return_value = True
        fixture = fixtures.MatrixChatFixture()
        member = fixture.matrix_room_member
        member.membership_state = models.MembershipStates.LEFT
        member.save()

        tasks.kick_member(member.uuid.hex, "Chat room was deactivated")

        mock_client.kick_user.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.tasks.kick_member")
@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class DisableRoomFailedKickTest(TestCase):
    def test_failed_kick_is_retried_and_not_recorded_as_left(
        self, mock_client, mock_kick_member
    ):
        mock_client.is_enabled.return_value = True
        mock_client.kick_user.side_effect = matrix_client.MatrixClientError("503")
        fixture = fixtures.MatrixChatFixture()
        member = fixture.matrix_room_member
        fixture.matrix_room.state = models.RoomStates.DISABLING
        fixture.matrix_room.save()

        tasks.disable_room(str(fixture.matrix_room.uuid))

        member.refresh_from_db()
        self.assertEqual(member.membership_state, models.MembershipStates.JOINED)
        mock_kick_member.delay.assert_called_once_with(member.uuid.hex, mock.ANY)


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class CustomerRoleRevokedTest(TestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.room = _room(self.project)
        self.user = structure_factories.UserFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)
        _member(self.room, self.user)

    def _revoke(self):
        role = UserRole.objects.get(user=self.user, scope=self.customer)
        role.is_active = False
        role.save()
        with self.captureOnCommitCallbacks(execute=True):
            handlers.on_role_revoked(sender=UserRole, instance=role)

    def test_revoking_the_customer_role_kicks_from_its_project_rooms(
        self, mock_client, mock_tasks
    ):
        # Sync invites customer-level role holders into every project room, so
        # losing that role must take them out again.
        mock_client.is_enabled.return_value = True

        self._revoke()

        mock_tasks.kick_user_from_room.delay.assert_called_once_with(
            str(self.room.uuid), str(self.user.uuid)
        )

    def test_rooms_the_user_is_not_in_are_left_alone(self, mock_client, mock_tasks):
        # Kicking someone who is not in a room fails and is retried for minutes.
        mock_client.is_enabled.return_value = True
        other_project = structure_factories.ProjectFactory(customer=self.customer)
        _room(other_project, room_id="!other:matrix.example.com")

        self._revoke()

        mock_tasks.kick_user_from_room.delay.assert_called_once_with(
            str(self.room.uuid), str(self.user.uuid)
        )

    def test_someone_else_having_left_does_not_hide_the_room(
        self, mock_client, mock_tasks
    ):
        mock_client.is_enabled.return_value = True
        departed = _member(self.room, structure_factories.UserFactory())
        departed.membership_state = models.MembershipStates.LEFT
        departed.save()

        self._revoke()

        mock_tasks.kick_user_from_room.delay.assert_called_once_with(
            str(self.room.uuid), str(self.user.uuid)
        )

    def test_a_remaining_project_role_keeps_the_user_in(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        self.project.add_user(self.user, ProjectRole.MEMBER)

        self._revoke()

        mock_tasks.kick_user_from_room.delay.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class ProjectRoleRevokedStaffTest(TestCase):
    def test_staff_who_joined_with_the_button_keeps_the_room(
        self, mock_client, mock_tasks
    ):
        # Staff can join any room with the button, so losing a project role
        # should not take away a room they joined that way.
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        room = _room(project)
        staff = structure_factories.UserFactory(is_staff=True)
        project.add_user(staff, ProjectRole.MEMBER)
        _member(room, staff, manually_joined=True)
        role = UserRole.objects.get(user=staff, scope=project)
        role.is_active = False
        role.save()

        with self.captureOnCommitCallbacks(execute=True):
            handlers.on_role_revoked(sender=UserRole, instance=role)

        mock_tasks.kick_user_from_room.delay.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class ReactivationTest(TestCase):
    def test_reactivation_resyncs_the_users_project_rooms(
        self, mock_client, mock_tasks
    ):
        # Deactivation kicked them; nothing else brings them back.
        mock_client.is_enabled.return_value = True
        mock_client.is_homeserver_configured.return_value = True
        project = structure_factories.ProjectFactory()
        room = _room(project)
        user = structure_factories.UserFactory()
        project.add_user(user, ProjectRole.MEMBER)
        user.is_active = False
        user.save()

        user.is_active = True
        with self.captureOnCommitCallbacks(execute=True):
            user.save()

        mock_tasks.sync_project_members_to_room.delay.assert_called_once_with(
            str(room.uuid)
        )


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class StaleRetryTest(TestCase):
    # A kick retried for up to 25 minutes can outlive its cause.

    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.room = _room(self.project)
        self.user = structure_factories.UserFactory()
        self.project.add_user(self.user, ProjectRole.MEMBER)
        self.member = _member(self.room, self.user)

    def test_a_user_reactivated_meanwhile_is_not_kicked(self, mock_client):
        mock_client.is_homeserver_configured.return_value = True

        tasks.kick_member(self.member.uuid.hex, "Account deactivated in Waldur")

        mock_client.kick_user.assert_not_called()
        self.member.refresh_from_db()
        self.assertEqual(self.member.membership_state, models.MembershipStates.JOINED)

    def test_a_deactivated_user_is_still_kicked(self, mock_client):
        mock_client.is_homeserver_configured.return_value = True
        self.user.is_active = False
        self.user.save()

        tasks.kick_member(self.member.uuid.hex, "Account deactivated in Waldur")

        mock_client.kick_user.assert_called_once()

    def test_members_of_a_room_being_disabled_are_still_kicked(self, mock_client):
        mock_client.is_homeserver_configured.return_value = True
        self.room.state = models.RoomStates.DISABLING
        self.room.save()

        tasks.kick_member(self.member.uuid.hex, "Chat room was deactivated")

        mock_client.kick_user.assert_called_once()

    def test_a_role_granted_again_meanwhile_keeps_the_user(self, mock_client):
        mock_client.is_enabled.return_value = True

        tasks.kick_user_from_room(str(self.room.uuid), str(self.user.uuid))

        mock_client.kick_user.assert_not_called()

    def test_a_customer_reader_role_does_not_keep_the_user(self, mock_client):
        # Member sync leaves readers out, so a retry must not keep one in.
        mock_client.is_enabled.return_value = True
        self.project.remove_user(self.user, ProjectRole.MEMBER)
        self.project.customer.add_user(self.user, CustomerRole.READER)

        tasks.kick_user_from_room(str(self.room.uuid), str(self.user.uuid))

        mock_client.kick_user.assert_called_once()


class KeepsRoomAccessTest(TestCase):
    def test_customer_rooms_follow_customer_roles(self):
        customer = structure_factories.CustomerFactory()
        room = models.MatrixRoom.objects.create(
            room_id="!c:matrix.example.com",
            room_name="customer",
            state=models.RoomStates.ACTIVE,
            content_type=ContentType.objects.get_for_model(customer),
            object_id=customer.id,
        )
        owner = structure_factories.UserFactory()
        customer.add_user(owner, CustomerRole.OWNER)

        self.assertTrue(models.keeps_room_access(owner, room))
        self.assertFalse(
            models.keeps_room_access(structure_factories.UserFactory(), room)
        )


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class StaffDemotionTest(TestCase):
    def test_demotion_takes_staff_out_of_rooms_they_joined_with_the_button(
        self, mock_client, mock_tasks
    ):
        # The Join button exempts staff only while they are staff; nothing
        # else would take a demoted user out until someone syncs the room.
        mock_client.is_enabled.return_value = True
        room = _room(structure_factories.ProjectFactory())
        staff = structure_factories.UserFactory(is_staff=True)
        _member(room, staff, manually_joined=True)

        staff.is_staff = False
        with self.captureOnCommitCallbacks(execute=True):
            staff.save()

        mock_tasks.kick_user_from_room.delay.assert_called_once_with(
            str(room.uuid), str(staff.uuid)
        )

    def test_unrelated_saves_of_staff_do_nothing(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        room = _room(structure_factories.ProjectFactory())
        staff = structure_factories.UserFactory(is_staff=True)
        _member(room, staff, manually_joined=True)

        staff.first_name = "Renamed"
        with self.captureOnCommitCallbacks(execute=True):
            staff.save()

        mock_tasks.kick_user_from_room.delay.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.tasks.kick_member")
@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class FailedLogoutStillKicksTest(TestCase):
    def test_room_kicks_do_not_wait_for_the_device_logout(
        self, mock_client, mock_kick_member
    ):
        # One device the homeserver keeps rejecting must not keep the user in
        # every room through all retries.
        mock_client.is_homeserver_configured.return_value = True
        mock_client.MatrixClientError = matrix_client.MatrixClientError
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixClientError(
            "503"
        )
        project = structure_factories.ProjectFactory()
        room = _room(project)
        user = structure_factories.UserFactory(is_active=False)
        _profile(user)
        member = _member(room, user)

        with self.assertRaises(matrix_client.MatrixClientError):
            tasks.end_matrix_access(user.uuid.hex)

        mock_client.kick_user.assert_called_once()
        member.refresh_from_db()
        self.assertEqual(member.membership_state, models.MembershipStates.LEFT)


@mock.patch("waldur_mastermind.matrix_chat.tasks.kick_from_room")
@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class DeletedUserFailedKickTest(TestCase):
    def test_a_failed_kick_is_retried_on_its_own(self, mock_client, mock_kick):
        # The membership rows go with the user, so nothing else would retry it.
        mock_client.is_homeserver_configured.return_value = True
        mock_client.kick_user.side_effect = matrix_client.MatrixClientError("503")

        tasks.end_deleted_user_access("@gone:matrix.example.com", ["!r:hs"])

        mock_kick.delay.assert_called_once_with(
            "!r:hs", "@gone:matrix.example.com", "Account deleted in Waldur"
        )
