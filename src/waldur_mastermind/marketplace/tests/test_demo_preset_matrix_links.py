"""A demo preset load keeps Waldur's links to Matrix accounts and rooms for the
users and scopes the new data has again."""

import json
from io import StringIO
from unittest import mock

from django.test import TransactionTestCase

from waldur_core.core.models import User
from waldur_core.structure.models import Project
from waldur_mastermind.marketplace.demo_presets import matrix_links
from waldur_mastermind.marketplace.tests.test_demo_preset_deployment import (
    DeploymentPresetsMixin,
    _customer,
    _preset,
    _user,
)
from waldur_mastermind.matrix_chat.models import (
    MatrixRoom,
    MatrixRoomMember,
    MatrixUserProfile,
    MembershipStates,
    RoomStates,
)

CUSTOMER_UUID = "a3000000000000000000000000000007"
PROJECT_UUID = "c3000000000000000000000000000007"


USER_UUIDS = {
    "owner": "000000000000000000000000000000b1",
    "member": "000000000000000000000000000000b2",
    "outsider": "000000000000000000000000000000b3",
}


def _role(uuid, username, role_name):
    return {
        "uuid": uuid,
        "user_uuid": USER_UUIDS[username],
        "user_username": username,
        "role_name": role_name,
        "scope_type": "structure.project",
        "scope_uuid": PROJECT_UUID,
        "is_active": True,
    }


CHAT = _preset(
    "chat",
    users=[
        _user(USER_UUIDS["owner"], "owner"),
        _user(USER_UUIDS["member"], "member"),
        _user(USER_UUIDS["outsider"], "outsider"),
    ],
    customers=[_customer(CUSTOMER_UUID, "Chat Org")],
    projects=[
        {"uuid": PROJECT_UUID, "name": "Chat Project", "customer_uuid": CUSTOMER_UUID}
    ],
    user_roles=[
        _role("e00000000000000000000000000000b1", "owner", "PROJECT.MANAGER"),
        _role("e00000000000000000000000000000b2", "member", "PROJECT.MEMBER"),
    ],
)


class MatrixLinksTest(DeploymentPresetsMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        (self.built_in / "chat.json").write_text(json.dumps(CHAT))

    def link(self):
        """Matrix records as a deployment with chat has them before a load."""
        project = Project.objects.get(uuid=PROJECT_UUID)
        room = MatrixRoom.objects.create(
            scope=project,
            room_id="!room:example.org",
            room_alias="#waldur-chat:example.org",
            room_name="Chat Project",
            state=RoomStates.ACTIVE,
            created_by=User.objects.get(username="owner"),
        )
        for username in ("owner", "member", "outsider"):
            user = User.objects.get(username=username)
            MatrixUserProfile.objects.create(
                user=user,
                matrix_user_id=f"@{username}:example.org",
                provisioned=True,
                recovery_key=f"recovery-{username}",
            )
            # The outsider joined while they held a role the new data drops.
            MatrixRoomMember.objects.create(
                room=room,
                user=user,
                matrix_user_id=f"@{username}:example.org",
                membership_state=MembershipStates.JOINED,
            )
        return project

    def test_links_survive_a_cleanup_load(self):
        self.load("chat")
        old_project = self.link()
        old_owner = User.objects.get(username="owner")

        result = self.load("chat")

        owner = User.objects.get(username="owner")
        project = Project.objects.get(uuid=PROJECT_UUID)
        self.assertNotEqual(owner.pk, old_owner.pk)
        self.assertNotEqual(project.pk, old_project.pk)

        profile = MatrixUserProfile.objects.get(user=owner)
        self.assertEqual(profile.matrix_user_id, "@owner:example.org")
        self.assertTrue(profile.provisioned)
        self.assertEqual(profile.recovery_key, "recovery-owner")

        room = MatrixRoom.objects.get(room_id="!room:example.org")
        self.assertEqual(room.scope, project)
        self.assertEqual(room.room_alias, "#waldur-chat:example.org")
        self.assertEqual(room.state, RoomStates.ACTIVE)
        self.assertEqual(room.created_by, owner)
        self.assertIn(
            "Kept the Matrix links of 3 user(s) and 1 room(s)", result["output"]
        )

    def test_membership_comes_back_only_for_who_still_belongs(self):
        self.load("chat")
        self.link()

        self.load("chat")

        room = MatrixRoom.objects.get(room_id="!room:example.org")
        self.assertEqual(
            sorted(
                MatrixRoomMember.objects.filter(room=room).values_list(
                    "user__username", flat=True
                )
            ),
            ["member", "owner"],
        )
        # Their account is kept all the same.
        self.assertTrue(
            MatrixUserProfile.objects.filter(user__username="outsider").exists()
        )

    def test_links_the_new_data_has_no_user_or_scope_for_are_dropped(self):
        self.load("chat")
        self.link()

        self.load("site")

        self.assertFalse(MatrixUserProfile.objects.exists())
        self.assertFalse(MatrixRoom.objects.exists())
        self.assertFalse(MatrixRoomMember.objects.exists())

    def test_a_load_that_keeps_the_users_points_the_room_at_the_new_project(self):
        self.load("chat")
        self.link()

        self.load("chat", skip_users=True)

        room = MatrixRoom.objects.get(room_id="!room:example.org")
        self.assertEqual(room.scope, Project.objects.get(uuid=PROJECT_UUID))
        self.assertEqual(MatrixRoom.objects.count(), 1)

    def test_a_load_that_keeps_the_users_drops_a_room_whose_project_is_gone(self):
        self.load("chat")
        self.link()

        self.load("site", skip_users=True)

        self.assertFalse(MatrixRoom.objects.exists())

    def test_kept_room_replaces_one_the_import_started_creating(self):
        self.load("chat")
        self.link()
        links = matrix_links.snapshot()
        MatrixRoom.objects.all().delete()
        project = Project.objects.get(uuid=PROJECT_UUID)
        # What MATRIX_AUTO_CREATE_PROJECT_ROOMS does for a new project: a row
        # with no homeserver room yet.
        MatrixRoom.objects.create(scope=project, room_name="Chat Project")

        matrix_links.restore(links, StringIO())

        room = MatrixRoom.objects.get(
            content_type__model="project", object_id=project.pk
        )
        self.assertEqual(room.room_id, "!room:example.org")
        self.assertEqual(room.room_alias, "#waldur-chat:example.org")

    def test_kept_room_does_not_replace_a_real_room_of_the_project(self):
        self.load("chat")
        self.link()
        links = matrix_links.snapshot()
        MatrixRoom.objects.all().delete()
        project = Project.objects.get(uuid=PROJECT_UUID)
        MatrixRoom.objects.create(
            scope=project, room_id="!other:example.org", state=RoomStates.ACTIVE
        )

        matrix_links.restore(links, StringIO())

        self.assertEqual(
            list(MatrixRoom.objects.values_list("room_id", flat=True)),
            ["!other:example.org"],
        )

    def test_member_sync_is_queued_for_kept_rooms_after_the_load(self):
        self.load("chat")
        self.link()

        with (
            mock.patch(
                "waldur_mastermind.matrix_chat.matrix_client.is_enabled",
                return_value=True,
            ),
            mock.patch(
                "waldur_mastermind.matrix_chat.tasks.sync_project_members_to_room.delay"
            ) as delay,
        ):
            self.load("chat")

        room = MatrixRoom.objects.get(room_id="!room:example.org")
        delay.assert_called_once_with(room.uuid.hex)

    def test_dry_run_changes_nothing(self):
        self.load("chat")
        project = self.link()

        self.load("chat", dry_run=True)

        self.assertEqual(MatrixUserProfile.objects.count(), 3)
        self.assertEqual(MatrixRoom.objects.get().scope, project)
