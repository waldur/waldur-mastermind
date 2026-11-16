"""Rooms of scopes other than projects, whose app says who belongs in them."""

from datetime import timedelta
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.models import User
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import (
    managers,
    models,
    room_provisioning,
    scopes,
    serializers,
    tasks,
)


class RegisteredScopeMixin:
    """Registers Customer, for the test only, as a kind of room scope whose
    members are whoever the test puts in ``self.member_ids``; the registry's
    fallback (a role on the customer) does not apply to it then."""

    def register_customer_scope(self, **hooks):
        self.member_ids = set()
        scopes.register(
            Customer,
            members=lambda customer: User.all_objects.filter(id__in=self.member_ids),
            **hooks,
        )
        self.addCleanup(scopes.unregister, Customer)

    def make_room(self, scope, **kwargs):
        defaults = dict(
            room_id="!scoped:matrix.example.com",
            room_name="Panel meeting",
            state=models.RoomStates.ACTIVE,
            content_type=ContentType.objects.get_for_model(scope),
            object_id=scope.pk,
        )
        defaults.update(kwargs)
        return models.MatrixRoom.objects.create(**defaults)


def _matrix_id(user):
    return f"@{user.username}:matrix.example.com"


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class SyncRoomMembersTest(RegisteredScopeMixin, TestCase):
    def setUp(self):
        self.register_customer_scope(
            power_level=lambda user, customer: 50 if user.username == "chair" else 0,
            role_label=lambda user, customer: "panel member",
        )
        self.customer = structure_factories.CustomerFactory()
        self.room = self.make_room(self.customer)
        self.alice = structure_factories.UserFactory(username="alice")
        self.member_ids.add(self.alice.id)

    def _enable(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.side_effect = _matrix_id
        # The real dispatch, which the module mock would otherwise replace.
        mock_client.get_power_level_for_scope.side_effect = scopes.power_level

    def _sync(self, mock_client):
        self._enable(mock_client)
        tasks.sync_room_members(str(self.room.uuid))

    def _member(self, user):
        return models.MatrixRoomMember.objects.get(room=self.room, user=user)

    def test_invites_and_joins_the_members_the_scope_names(self, mock_client):
        self._sync(mock_client)

        mock_client.invite_user.assert_called_once_with(
            self.room.room_id, "@alice:matrix.example.com"
        )
        self.assertEqual(
            self._member(self.alice).membership_state, models.MembershipStates.JOINED
        )

    def test_kicks_a_member_the_scope_no_longer_names(self, mock_client):
        bob = structure_factories.UserFactory(username="bob")
        models.MatrixRoomMember.objects.create(
            room=self.room,
            user=bob,
            matrix_user_id="@bob:matrix.example.com",
            membership_state=models.MembershipStates.JOINED,
        )

        self._sync(mock_client)

        mock_client.kick_user.assert_called_once_with(
            self.room.room_id,
            "@bob:matrix.example.com",
            reason="Role removed in Waldur",
        )
        self.assertEqual(
            self._member(bob).membership_state, models.MembershipStates.LEFT
        )

    def test_leaves_staff_who_joined_with_join_alone(self, mock_client):
        staff = structure_factories.UserFactory(is_staff=True)
        models.MatrixRoomMember.objects.create(
            room=self.room,
            user=staff,
            matrix_user_id="@staff:matrix.example.com",
            power_level=50,
            membership_state=models.MembershipStates.JOINED,
            manually_joined=True,
        )

        self._sync(mock_client)

        mock_client.kick_user.assert_not_called()
        self.assertEqual(
            self._member(staff).membership_state, models.MembershipStates.JOINED
        )

    def test_takes_out_support_who_joined_once_the_scope_keeps_them_out(
        self, mock_client
    ):
        support = structure_factories.UserFactory(is_support=True)
        kept_out = set()
        self.register_customer_scope(
            may_join=lambda user, customer: user.pk not in kept_out
        )
        models.MatrixRoomMember.objects.create(
            room=self.room,
            user=support,
            matrix_user_id=_matrix_id(support),
            power_level=50,
            membership_state=models.MembershipStates.JOINED,
            manually_joined=True,
        )
        self.member_ids.add(self.alice.id)
        self._sync(mock_client)
        mock_client.kick_user.assert_not_called()

        kept_out.add(support.pk)
        self._sync(mock_client)
        mock_client.kick_user.assert_called_once_with(
            self.room.room_id, _matrix_id(support), reason="Role removed in Waldur"
        )
        self.assertFalse(models.keeps_room_access(support, self.room))

    def test_skips_an_inactive_member_and_syncs_the_rest(self, mock_client):
        carol = structure_factories.UserFactory(username="carol", is_active=False)
        self.member_ids.add(carol.id)

        self._sync(mock_client)

        provisioned = [c.args[0] for c in mock_client.ensure_user_exists.call_args_list]
        self.assertEqual(provisioned, [self.alice])
        self.assertFalse(
            models.MatrixRoomMember.objects.filter(room=self.room, user=carol).exists()
        )
        self.assertEqual(
            self._member(self.alice).membership_state, models.MembershipStates.JOINED
        )

    def test_kicks_a_member_deactivated_since_the_last_sync(self, mock_client):
        self._sync(mock_client)
        User.objects.filter(pk=self.alice.pk).update(is_active=False)

        self._sync(mock_client)

        mock_client.kick_user.assert_called_once_with(
            self.room.room_id,
            "@alice:matrix.example.com",
            reason="Role removed in Waldur",
        )

    def test_one_failing_member_does_not_stop_the_sync(self, mock_client):
        bob = structure_factories.UserFactory(username="bob")
        self.member_ids.add(bob.id)
        self._enable(mock_client)

        def refuse_alice(user):
            if user == self.alice:
                raise RuntimeError("refused")
            return _matrix_id(user)

        mock_client.ensure_user_exists.side_effect = refuse_alice

        tasks.sync_room_members(str(self.room.uuid))

        self.assertEqual(
            self._member(bob).membership_state, models.MembershipStates.JOINED
        )

    def test_power_level_comes_from_the_registration(self, mock_client):
        chair = structure_factories.UserFactory(username="chair")
        self.member_ids.add(chair.id)

        self._sync(mock_client)

        mock_client.set_power_level.assert_any_call(
            self.room.room_id, "@chair:matrix.example.com", 50
        )
        mock_client.set_power_level.assert_any_call(
            self.room.room_id, "@alice:matrix.example.com", 0
        )
        self.assertEqual(self._member(chair).power_level, 50)

    def test_first_sync_welcomes_without_announcing_roles(self, mock_client):
        self._sync(mock_client)

        bodies = list(
            models.MatrixOutboxMessage.objects.filter(room=self.room).values_list(
                "body", flat=True
            )
        )
        self.assertEqual(len(bodies), 1)
        self.assertTrue(bodies[0].startswith("**Welcome to this chat!**"))

    def test_member_added_after_the_welcome_is_announced(self, mock_client):
        self.room.announced_at = timezone.now()
        self.room.save(update_fields=["announced_at"])
        models.MatrixRoomMember.objects.create(
            room=self.room,
            user=self.alice,
            matrix_user_id="@alice:matrix.example.com",
            membership_state=models.MembershipStates.JOINED,
        )
        bob = structure_factories.UserFactory(username="bob", full_name="Bob B")
        self.member_ids.add(bob.id)

        self._sync(mock_client)

        notice = models.MatrixOutboxMessage.objects.get(room=self.room)
        self.assertEqual(notice.body, "Bob B has been granted the panel member role.")
        self.assertEqual(
            notice.extra_content,
            {
                "m.mentions": {"user_ids": ["@bob:matrix.example.com"]},
                "com.waldur.welcome": True,
            },
        )

    def test_registered_welcome_text_is_used(self, mock_client):
        self.register_customer_scope(welcome=lambda room: "Panel room.")
        self.member_ids.add(self.alice.id)

        self._sync(mock_client)

        self.assertEqual(
            models.MatrixOutboxMessage.objects.get(room=self.room).body, "Panel room."
        )

    def test_the_old_task_name_syncs_any_room(self, mock_client):
        self._enable(mock_client)

        tasks.sync_project_members_to_room(str(self.room.uuid))

        self.assertEqual(
            self._member(self.alice).membership_state, models.MembershipStates.JOINED
        )

    def test_created_room_is_synced_by_the_generic_task(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.create_room.return_value = ("!new:matrix.example.com", False)
        self.room.state = models.RoomStates.CREATING
        self.room.room_id = None
        self.room.save(update_fields=["state", "room_id"])

        with mock.patch.object(tasks, "sync_room_members") as sync:
            tasks.create_room(str(self.room.uuid))

        sync.delay.assert_called_once_with(str(self.room.uuid))
        _, kwargs = mock_client.create_room.call_args
        # Only project rooms get an alias.
        self.assertIsNone(kwargs["alias_localpart"])


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class ResyncScopeRoomsTest(RegisteredScopeMixin, TestCase):
    def setUp(self):
        self.register_customer_scope()
        self.customer = structure_factories.CustomerFactory()

    def test_syncs_the_scope_rooms_after_commit(self, mock_client):
        mock_client.is_enabled.return_value = True
        room = self.make_room(self.customer)

        with (
            mock.patch.object(tasks, "sync_room_members") as sync,
            mock.patch.object(transaction, "on_commit") as on_commit,
        ):
            tasks.resync_scope_rooms(self.customer)
            sync.delay.assert_not_called()
            (callback,) = on_commit.call_args.args
            callback()

        sync.delay.assert_called_once_with(str(room.uuid))

    def test_skips_rooms_that_are_not_active(self, mock_client):
        mock_client.is_enabled.return_value = True
        self.make_room(self.customer, state=models.RoomStates.ARCHIVED)

        with mock.patch.object(tasks, "sync_room_members") as sync:
            tasks.resync_scope_rooms(self.customer)

        sync.delay.assert_not_called()

    def test_reactivation_resyncs_rooms_the_scope_put_the_user_in_meanwhile(
        self, mock_client
    ):
        # Added while deactivated: the sync skipped them and wrote no member
        # row, so only the scope can say which room is theirs.
        mock_client.is_homeserver_configured.return_value = True
        mock_client.is_enabled.return_value = True
        user = structure_factories.UserFactory()
        self.register_customer_scope(
            scope_ids_for_user=lambda u: Customer.objects.filter(
                pk=self.customer.pk if u.id in self.member_ids else None
            ).values("pk")
        )
        self.member_ids.add(user.id)
        room = self.make_room(self.customer)
        other = self.make_room(
            structure_factories.CustomerFactory(), room_id="!other:matrix.example.com"
        )

        with mock.patch.object(tasks, "sync_room_members") as sync:
            tasks.restore_matrix_access(user.uuid.hex)

        sync.delay.assert_called_once_with(str(room.uuid))
        self.assertNotIn(mock.call(str(other.uuid)), sync.delay.call_args_list)

    def test_reactivation_without_the_hook_finds_rooms_by_membership_only(
        self, mock_client
    ):
        mock_client.is_homeserver_configured.return_value = True
        mock_client.is_enabled.return_value = True
        user = structure_factories.UserFactory()
        self.member_ids.add(user.id)
        self.make_room(self.customer)

        with mock.patch.object(tasks, "sync_room_members") as sync:
            tasks.restore_matrix_access(user.uuid.hex)

        sync.delay.assert_not_called()

    def test_reactivation_resyncs_rooms_the_user_was_in(self, mock_client):
        mock_client.is_homeserver_configured.return_value = True
        mock_client.is_enabled.return_value = True
        room = self.make_room(self.customer)
        user = structure_factories.UserFactory()
        models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id="@back:matrix.example.com",
            membership_state=models.MembershipStates.LEFT,
        )

        with mock.patch.object(tasks, "sync_room_members") as sync:
            tasks.restore_matrix_access(user.uuid.hex)

        sync.delay.assert_called_once_with(str(room.uuid))


class HasRoomRoleTest(RegisteredScopeMixin, TestCase):
    def test_registered_scope_decides(self):
        self.register_customer_scope()
        customer = structure_factories.CustomerFactory()
        room = self.make_room(customer)
        member = structure_factories.UserFactory()
        owner = structure_factories.UserFactory()
        customer.add_user(owner, CustomerRole.OWNER)
        self.member_ids.add(member.id)

        self.assertTrue(models.has_room_role(member, room))
        # A role on the object does not count once its app says who belongs.
        self.assertFalse(models.has_room_role(owner, room))
        self.assertFalse(models.keeps_room_access(owner, room))

    def test_unregistered_scope_falls_back_to_a_role_on_it(self):
        customer = structure_factories.CustomerFactory()
        room = self.make_room(customer)
        owner = structure_factories.UserFactory()
        customer.add_user(owner, CustomerRole.OWNER)

        self.assertTrue(models.has_room_role(owner, room))
        self.assertFalse(models.has_room_role(structure_factories.UserFactory(), room))


class RegisteredScopeRoomListTest(RegisteredScopeMixin, test.APITestCase):
    url = "/api/matrix/rooms/"

    def setUp(self):
        self.member = structure_factories.UserFactory()

    def _join(self, room, user, state=models.MembershipStates.JOINED):
        models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id=_matrix_id(user),
            membership_state=state,
        )

    def test_scope_without_an_api_route_serializes_without_a_url(self):
        # A kind of object with no <model>-detail route: the room list used to
        # answer 500 for it.
        profile = models.MatrixUserProfile.objects.create(
            user=structure_factories.UserFactory(),
            matrix_user_id="@profile:matrix.example.com",
        )
        scopes.register(
            models.MatrixUserProfile,
            members=lambda scope: User.objects.none(),
            name=lambda scope: "Panel meeting 1",
        )
        self.addCleanup(scopes.unregister, models.MatrixUserProfile)
        room = self.make_room(profile)
        self._join(room, self.member)

        self.client.force_authenticate(self.member)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertIsNone(response.data[0]["scope"])
        self.assertEqual(response.data[0]["scope_uuid"], profile.uuid.hex)
        self.assertEqual(response.data[0]["scope_name"], "Panel meeting 1")

    def test_registered_url_is_used(self):
        self.register_customer_scope(
            url=lambda scope, request: f"https://example.com/meetings/{scope.pk}/"
        )
        customer = structure_factories.CustomerFactory()
        room = self.make_room(customer)
        self._join(room, self.member)

        self.client.force_authenticate(self.member)
        response = self.client.get(self.url)

        self.assertEqual(
            response.data[0]["scope"], f"https://example.com/meetings/{customer.pk}/"
        )

    def test_the_room_says_what_kind_it_is(self):
        self.register_customer_scope(
            kind="meeting", subtitle=lambda scope: f"Meetings of {scope.name}"
        )
        customer = structure_factories.CustomerFactory(name="Acme")
        meeting = self.make_room(customer)
        self._join(meeting, self.member)
        project = structure_factories.ProjectFactory()
        project_room = self.make_room(project, room_id="!project:matrix.example.com")
        profile = models.MatrixUserProfile.objects.create(
            user=structure_factories.UserFactory(),
            matrix_user_id="@profile:matrix.example.com",
        )
        unregistered = self.make_room(profile, room_id="!other:matrix.example.com")

        self.client.force_authenticate(self.member)
        rows = {r["uuid"]: r for r in self.client.get(self.url).data}
        self.assertEqual(rows[meeting.uuid.hex]["scope_type"], "meeting")
        self.assertEqual(rows[meeting.uuid.hex]["scope_subtitle"], "Meetings of Acme")
        request = test.APIRequestFactory().get(self.url)
        request.user = self.member
        project_row = serializers.MatrixRoomSerializer(
            project_room, context={"request": request}
        ).data
        self.assertEqual(project_row["scope_type"], "project")
        self.assertEqual(project_row["scope_subtitle"], project.customer.name)
        other_row = serializers.MatrixRoomSerializer(
            unregistered, context={"request": request}
        ).data
        self.assertIsNone(other_row["scope_type"])
        self.assertIsNone(other_row["scope_subtitle"])

        for kind, expected in (
            ("meeting", [meeting.uuid.hex]),
            ("project", []),
            ("nothing", []),
        ):
            response = self.client.get(self.url, {"scope_type": kind})
            self.assertEqual([r["uuid"] for r in response.data], expected, kind)

    def test_support_see_every_room_but_those_their_scope_keeps_them_from(self):
        support = structure_factories.UserFactory(is_support=True)
        staff = structure_factories.UserFactory(is_staff=True)
        kept, other = (
            structure_factories.CustomerFactory(),
            structure_factories.CustomerFactory(),
        )
        self.register_customer_scope(
            kept_from=lambda user: [kept.pk] if user.pk == support.pk else []
        )
        kept_room = self.make_room(kept)
        other_room = self.make_room(other, room_id="!other:matrix.example.com")
        self.assertEqual(
            scopes.rooms_kept_from(staff),
            None,
        )

        self.client.force_authenticate(support)
        listed = {r["uuid"] for r in self.client.get(self.url).data}
        self.assertEqual(listed, {other_room.uuid.hex})
        response = self.client.get(f"{self.url}{kept_room.uuid.hex}/members/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.client.force_authenticate(staff)
        listed = {r["uuid"] for r in self.client.get(self.url).data}
        self.assertEqual(listed, {kept_room.uuid.hex, other_room.uuid.hex})

    def test_current_members_see_the_room_and_no_one_else(self):
        self.register_customer_scope()
        customer = structure_factories.CustomerFactory()
        room = self.make_room(customer)
        self._join(room, self.member)
        former = structure_factories.UserFactory()
        self._join(room, former, models.MembershipStates.LEFT)
        # A role on the object no longer shows its room.
        owner = structure_factories.UserFactory()
        customer.add_user(owner, CustomerRole.OWNER)

        self.assertIn(room.id, managers.get_accessible_room_ids(self.member))
        self.assertNotIn(room.id, managers.get_accessible_room_ids(former))
        self.assertNotIn(room.id, managers.get_accessible_room_ids(owner))

        self.client.force_authenticate(self.member)
        response = self.client.get(self.url)
        self.assertEqual([r["uuid"] for r in response.data], [room.uuid.hex])
        self.assertTrue(response.data[0]["scope"].endswith(f"{customer.uuid.hex}/"))


@mock.patch("waldur_mastermind.matrix_chat.room_provisioning.tasks")
class EnsureRoomTest(RegisteredScopeMixin, TestCase):
    def setUp(self):
        self.register_customer_scope()
        self.customer = structure_factories.CustomerFactory(name="Panel A")
        self.creator = structure_factories.UserFactory()
        enabled = mock.patch.object(
            room_provisioning.matrix_client, "is_enabled", return_value=True
        )
        self.is_enabled = enabled.start()
        self.addCleanup(enabled.stop)

    def _existing(self, state, **kwargs):
        return models.MatrixRoom.objects.create(
            room_name="Panel A",
            state=state,
            content_type=ContentType.objects.get_for_model(self.customer),
            object_id=self.customer.pk,
            **kwargs,
        )

    def _age(self, room, minutes):
        models.MatrixRoom.objects.filter(pk=room.pk).update(
            modified=timezone.now() - timedelta(minutes=minutes)
        )

    def test_refuses_while_chat_is_disabled(self, mock_tasks):
        # It used to write the room in CREATING and queue a create_room that
        # returns at once while chat is off, and the room stayed CREATING.
        self.is_enabled.return_value = False

        with self.assertRaises(room_provisioning.MatrixChatDisabled):
            room_provisioning.ensure_room(self.customer)

        self.assertFalse(models.MatrixRoom.objects.exists())
        mock_tasks.create_room.delay.assert_not_called()

    def test_requeues_a_room_left_creating_once_chat_is_back(self, mock_tasks):
        # Written while chat was off, before ensure_room refused then.
        stuck = self._existing(models.RoomStates.CREATING)
        self._age(stuck, 60)

        with mock.patch.object(transaction, "on_commit") as on_commit:
            room, created = room_provisioning.ensure_room(self.customer)
            mock_tasks.create_room.delay.assert_not_called()
            (callback,) = on_commit.call_args.args
            callback()

        self.assertEqual((room, created), (stuck, False))
        self.assertEqual(room.state, models.RoomStates.CREATING)
        mock_tasks.create_room.delay.assert_called_once_with(str(stuck.uuid))

        # Requeued once: the next call leaves the new dispatch to finish.
        room_provisioning.ensure_room(self.customer)
        mock_tasks.create_room.delay.assert_called_once()

    def test_leaves_a_room_still_being_created_to_its_task(self, mock_tasks):
        self._existing(models.RoomStates.CREATING)

        room_provisioning.ensure_room(self.customer)

        mock_tasks.create_room.delay.assert_not_called()

    def test_requeues_a_room_whose_creation_failed(self, mock_tasks):
        erred = self._existing(models.RoomStates.ERROR, error_message="boom")

        room, created = room_provisioning.ensure_room(self.customer)

        self.assertFalse(created)
        erred.refresh_from_db()
        self.assertEqual(erred.state, models.RoomStates.CREATING)
        self.assertEqual(erred.error_message, "")
        mock_tasks.create_room.delay.assert_called_once_with(str(erred.uuid))

    def test_leaves_an_erred_room_that_exists_on_the_homeserver(self, mock_tasks):
        # Erred later, such as a disable that failed: not recreated.
        erred = self._existing(
            models.RoomStates.ERROR, room_id="!erred:matrix.example.com"
        )

        room, _ = room_provisioning.ensure_room(self.customer)

        self.assertEqual(room.state, models.RoomStates.ERROR)
        self.assertEqual(room, erred)
        mock_tasks.create_room.delay.assert_not_called()

    def test_creates_the_room_once(self, mock_tasks):
        # The repository's conftest runs on_commit callbacks at once.
        room, created = room_provisioning.ensure_room(
            self.customer, created_by=self.creator
        )
        again, created_again = room_provisioning.ensure_room(
            self.customer, created_by=self.creator, name="Other"
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(again, room)
        self.assertEqual(room.state, models.RoomStates.CREATING)
        self.assertEqual(room.room_name, "Panel A")
        self.assertEqual(room.created_by, self.creator)
        self.assertEqual(room.scope, self.customer)
        mock_tasks.create_room.delay.assert_called_once_with(str(room.uuid))

    def test_returns_an_archived_room_as_it_is(self, mock_tasks):
        archived = models.MatrixRoom.objects.create(
            room_name="Old",
            state=models.RoomStates.ARCHIVED,
            content_type=ContentType.objects.get_for_model(self.customer),
            object_id=self.customer.pk,
        )

        room, created = room_provisioning.ensure_room(self.customer)

        self.assertEqual((room, created), (archived, False))
        mock_tasks.create_room.delay.assert_not_called()

    def test_name_is_cut_to_the_room_name_column(self, mock_tasks):
        room, _ = room_provisioning.ensure_room(self.customer, name="x" * 300)

        max_length = models.MatrixRoom._meta.get_field("room_name").max_length
        self.assertEqual(room.room_name, "x" * max_length)

    def test_refuses_projects_and_unregistered_scopes(self, mock_tasks):
        with self.assertRaises(ValueError):
            room_provisioning.ensure_room(structure_factories.ProjectFactory())
        with self.assertRaises(ValueError):
            room_provisioning.ensure_room(structure_factories.UserFactory())
        self.assertFalse(models.MatrixRoom.objects.exists())


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class UnregisteredScopeSyncTest(RegisteredScopeMixin, TestCase):
    """Organization rooms, whose kind of scope nobody registered, were never
    synced; syncing them by any role on the organization would bring in
    readers and support and backfill them the room's history."""

    def test_customer_room_sync_invites_nobody(self, mock_client):
        mock_client.is_enabled.return_value = True
        customer = structure_factories.CustomerFactory()
        for role in (CustomerRole.OWNER, CustomerRole.SUPPORT):
            customer.add_user(structure_factories.UserFactory(), role)
        room = self.make_room(customer)
        outsider = structure_factories.UserFactory()
        models.MatrixRoomMember.objects.create(
            room=room,
            user=outsider,
            matrix_user_id=_matrix_id(outsider),
            membership_state=models.MembershipStates.JOINED,
        )

        tasks.sync_room_members(str(room.uuid))

        mock_client.ensure_user_exists.assert_not_called()
        mock_client.invite_user.assert_not_called()
        mock_client.kick_user.assert_not_called()
        self.assertEqual(
            list(room.members.values_list("user_id", flat=True)), [outsider.id]
        )

    def test_reactivation_does_not_sync_a_customer_room(self, mock_client):
        mock_client.is_homeserver_configured.return_value = True
        mock_client.is_enabled.return_value = True
        room = self.make_room(structure_factories.CustomerFactory())
        user = structure_factories.UserFactory()
        models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id=_matrix_id(user),
            membership_state=models.MembershipStates.LEFT,
        )

        with mock.patch.object(tasks, "sync_room_members") as sync:
            tasks.restore_matrix_access(user.uuid.hex)

        sync.delay.assert_not_called()


class DeletedScopeRoomTest(RegisteredScopeMixin, TestCase):
    """Deleting a registered object closes its room; an app gets it by
    registering."""

    def setUp(self):
        self.profile = models.MatrixUserProfile.objects.create(
            user=structure_factories.UserFactory(),
            matrix_user_id="@profile:matrix.example.com",
        )
        scopes.register(
            models.MatrixUserProfile, members=lambda scope: User.objects.none()
        )
        self.addCleanup(scopes.unregister, models.MatrixUserProfile)
        self.room = self.make_room(self.profile)

    def _delete(self, enabled):
        with (
            mock.patch(
                "waldur_mastermind.matrix_chat.matrix_client.is_enabled",
                return_value=enabled,
            ),
            mock.patch.object(tasks.disable_room, "delay") as disable,
            mock.patch.object(transaction, "on_commit") as on_commit,
        ):
            self.profile.delete()
            disable.assert_not_called()
            for call in on_commit.call_args_list:
                call.args[0]()
        self.room.refresh_from_db()
        return disable

    def test_closes_the_room_after_commit(self):
        disable = self._delete(enabled=True)

        self.assertEqual(self.room.state, models.RoomStates.DISABLING)
        disable.assert_called_once_with(
            str(self.room.uuid), reason="Matrix user profile deletion"
        )

    def test_with_chat_disabled_calls_nothing_and_logs(self):
        with self.assertLogs(
            "waldur_mastermind.matrix_chat.handlers", level="WARNING"
        ) as logs:
            disable = self._delete(enabled=False)

        disable.assert_not_called()
        self.assertEqual(self.room.state, models.RoomStates.ACTIVE)
        self.assertIn(str(self.room.uuid), logs.output[0])

    def test_leaves_a_room_that_is_not_active(self):
        models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=models.RoomStates.ARCHIVED
        )

        disable = self._delete(enabled=True)

        disable.assert_not_called()
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)

    def test_no_handler_once_unregistered_or_opted_out(self):
        scopes.register(
            models.MatrixUserProfile,
            members=lambda scope: User.objects.none(),
            close_on_delete=False,
        )

        disable = self._delete(enabled=True)

        disable.assert_not_called()
        self.assertEqual(self.room.state, models.RoomStates.ACTIVE)
