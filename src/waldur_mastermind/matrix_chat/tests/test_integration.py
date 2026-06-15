"""
Integration tests for the Matrix chat module.

These tests hit the real Tuwunel homeserver at localhost:6167
(from docker/matrix-dev/) with NO mocking of matrix_client.

All tests are automatically skipped when the homeserver is not reachable.
To run:
    cd docker/matrix-dev && docker compose up -d
    DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings_local \
      uv run pytest src/waldur_mastermind/matrix_chat/tests/test_integration.py -v
"""

import logging
from uuid import uuid4

import httpx
import pytest
from constance.test import override_config
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import matrix_client, models, tasks

logger = logging.getLogger(__name__)

# Module-level marker — applied to every test in this file. The marker is
# auto-skipped unless the run explicitly opts in with `-m matrix_integration`
# (see matrix_chat/tests/conftest.py). This replaces an earlier
# @skipUnless(HOMESERVER_AVAILABLE) pattern that probed the homeserver via
# httpx at import time. CI runners that drop (rather than refuse) packets to
# the unbound port wedge the import indefinitely, hanging the whole test
# shard. Marker-based skipping avoids any network I/O during collection.
pytestmark = pytest.mark.matrix_integration

HOMESERVER_URL = "http://localhost:6167"
AS_TOKEN = "dev-as-token-0000000000000000"
HS_TOKEN = "dev-hs-token-0000000000000000"

# ---------------------------------------------------------------------------
# Shared config — mirrors docker/matrix-dev/tuwunel.toml
# ---------------------------------------------------------------------------

MATRIX_CONFIG = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL=HOMESERVER_URL,
    MATRIX_HOMESERVER_DOMAIN="localhost",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
    MATRIX_LOGIN_METHOD="token",
    MATRIX_USER_REGISTRATION_SECRET="devsecret",
    MATRIX_USER_ID_FORMAT="username",
    MATRIX_HISTORY_EXPORT_ENABLED=False,
    MATRIX_EXPORT_MEDIA=False,
)

# Track whether the bot has been registered (idempotent, but save HTTP calls)
_bot_registered = False


def _ensure_bot():
    global _bot_registered
    if not _bot_registered:
        matrix_client.ensure_bot_user_exists()
        _bot_registered = True


def _unique_name(prefix="inttest"):
    """Return a unique name suitable for Matrix aliases and usernames."""
    return f"{prefix}_{uuid4().hex[:12]}"


def _make_room_for_project(project, room_id="", state=models.RoomStates.CREATING):
    """Create a MatrixRoom DB record linked to a project."""
    ct = ContentType.objects.get_for_model(project)
    return models.MatrixRoom.objects.create(
        room_id=room_id or f"placeholder:{uuid4().hex[:8]}",
        room_name=f"Project: {project.name}",
        content_type=ct,
        object_id=project.id,
        created_by=None,
        state=state,
    )


# ===========================================================================
# Test classes
# ===========================================================================


@override_config(**MATRIX_CONFIG)
class MatrixClientBotTest(TestCase):
    """Test bot user registration against the real homeserver."""

    def test_ensure_bot_user_exists(self):
        """Bot registration should succeed (or be idempotent if already exists)."""
        result = matrix_client.ensure_bot_user_exists()
        # First call returns the registration response dict, subsequent calls return None
        self.assertTrue(result is None or isinstance(result, dict))

    def test_ensure_bot_user_exists_idempotent(self):
        """Calling ensure_bot_user_exists twice should not raise."""
        matrix_client.ensure_bot_user_exists()
        result = matrix_client.ensure_bot_user_exists()
        self.assertIsNone(result)  # M_USER_IN_USE → returns None


@override_config(**MATRIX_CONFIG)
class MatrixClientRoomLifecycleTest(TestCase):
    """Test room creation, messaging, and replies against the real homeserver."""

    def setUp(self):
        super().setUp()
        _ensure_bot()

    def test_create_room(self):
        name = _unique_name("room")
        room_id, alias_was_set = matrix_client.create_room(name)
        self.assertTrue(room_id.startswith("!"))
        self.assertFalse(alias_was_set)  # no alias_localpart provided

    def test_create_room_with_alias(self):
        alias = f"waldur-{_unique_name('alias')}"
        room_id, alias_was_set = matrix_client.create_room(
            f"Room {alias}", alias_localpart=alias
        )
        self.assertTrue(room_id.startswith("!"))
        self.assertTrue(alias_was_set)

    def test_create_room_duplicate_alias_falls_back(self):
        alias = f"waldur-{_unique_name('dupalias')}"
        matrix_client.create_room("First", alias_localpart=alias)
        # Second room with same alias should fall back to no-alias
        room_id, alias_was_set = matrix_client.create_room(
            "Second", alias_localpart=alias
        )
        self.assertTrue(room_id.startswith("!"))
        self.assertFalse(alias_was_set)

    def test_send_message(self):
        room_id, _ = matrix_client.create_room(_unique_name("msgroom"))
        event_id = matrix_client.send_message(room_id, "Hello from integration test")
        self.assertTrue(event_id.startswith("$"))

    def test_get_room_messages(self):
        room_id, _ = matrix_client.create_room(_unique_name("getmsg"))
        matrix_client.send_message(room_id, "test message")
        result = matrix_client.get_room_messages(room_id, limit=10)
        self.assertIn("messages", result)
        self.assertIn("end_token", result)
        bodies = [m.get("body") for m in result["messages"] if m.get("body")]
        self.assertIn("test message", bodies)

    def test_send_reply(self):
        room_id, _ = matrix_client.create_room(_unique_name("reply"))
        event_id = matrix_client.send_message(room_id, "original")
        reply_event_id = matrix_client.send_reply(room_id, event_id, "reply text")
        self.assertTrue(reply_event_id.startswith("$"))
        self.assertNotEqual(event_id, reply_event_id)


@override_config(**MATRIX_CONFIG)
class MatrixClientUserProvisioningTest(TestCase):
    """Test user provisioning, invite, join, kick, power levels."""

    def setUp(self):
        super().setUp()
        _ensure_bot()
        # Create a shared room for tests that need one
        self._room_id, _ = matrix_client.create_room(_unique_name("userprov"))

    def _make_waldur_user(self):
        return structure_factories.UserFactory(
            username=_unique_name("user"),
            email=f"{_unique_name('mail')}@test.local",
        )

    def test_ensure_user_exists(self):
        user = self._make_waldur_user()
        matrix_user_id = matrix_client.ensure_user_exists(user)
        self.assertTrue(matrix_user_id.startswith("@"))
        self.assertIn(":localhost", matrix_user_id)

        # Profile should be created and marked provisioned
        profile = models.MatrixUserProfile.objects.get(user=user)
        self.assertTrue(profile.provisioned)

    def test_ensure_user_exists_idempotent(self):
        user = self._make_waldur_user()
        mid1 = matrix_client.ensure_user_exists(user)
        mid2 = matrix_client.ensure_user_exists(user)
        self.assertEqual(mid1, mid2)

    def test_set_display_name(self):
        user = self._make_waldur_user()
        matrix_user_id = matrix_client.ensure_user_exists(user)
        # Should not raise
        matrix_client.set_display_name(matrix_user_id, "Integration Tester")

    def test_invite_user(self):
        user = self._make_waldur_user()
        matrix_user_id = matrix_client.ensure_user_exists(user)
        result = matrix_client.invite_user(self._room_id, matrix_user_id)
        self.assertTrue(result)

    def test_join_room_as_self(self):
        user = self._make_waldur_user()
        matrix_user_id = matrix_client.ensure_user_exists(user)
        matrix_client.invite_user(self._room_id, matrix_user_id)
        access_token = matrix_client.get_access_token_for_user(user)
        result = matrix_client.join_room_as_self(self._room_id, access_token)
        self.assertTrue(result)

    def test_kick_user(self):
        user = self._make_waldur_user()
        matrix_user_id = matrix_client.ensure_user_exists(user)
        matrix_client.invite_user(self._room_id, matrix_user_id)
        access_token = matrix_client.get_access_token_for_user(user)
        matrix_client.join_room_as_self(self._room_id, access_token)
        result = matrix_client.kick_user(
            self._room_id, matrix_user_id, reason="test kick"
        )
        self.assertTrue(result)

    def test_set_power_level(self):
        user = self._make_waldur_user()
        matrix_user_id = matrix_client.ensure_user_exists(user)
        matrix_client.invite_user(self._room_id, matrix_user_id)
        access_token = matrix_client.get_access_token_for_user(user)
        matrix_client.join_room_as_self(self._room_id, access_token)
        result = matrix_client.set_power_level(self._room_id, matrix_user_id, 50)
        self.assertTrue(result)

    def test_get_access_token_for_user(self):
        user = self._make_waldur_user()
        matrix_client.ensure_user_exists(user)
        token = matrix_client.get_access_token_for_user(user)
        self.assertIsInstance(token, str)
        self.assertTrue(len(token) > 0)

    def test_join_without_invite_raises(self):
        """Joining a room without an invite should fail (room is invite-only)."""
        user = self._make_waldur_user()
        matrix_client.ensure_user_exists(user)
        # Create a fresh room to ensure user was never invited
        fresh_room_id, _ = matrix_client.create_room(_unique_name("noinvite"))
        access_token = matrix_client.get_access_token_for_user(user)
        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.join_room_as_self(fresh_room_id, access_token)


@override_config(**MATRIX_CONFIG)
class MatrixCredentialsEndpointIntegrationTest(test.APITestCase):
    """
    Integration test for the M_FORBIDDEN fix.

    The credentials endpoint at GET /api/matrix/credentials/?room_uuid=...
    must both invite AND join the user so that the returned access token
    can immediately perform actions (like typing indicators) in the room.
    """

    def setUp(self):
        super().setUp()
        _ensure_bot()
        self.fixture_project = structure_factories.ProjectFactory()
        self.fixture_user = structure_factories.UserFactory(
            username=_unique_name("creduser"),
        )
        from waldur_core.permissions.fixtures import ProjectRole

        self.fixture_project.add_user(self.fixture_user, ProjectRole.ADMIN)

    def _create_real_room_for_project(self, project):
        """Create a real Matrix room and a corresponding DB record."""
        room_name = _unique_name("credroom")
        room_id, _ = matrix_client.create_room(room_name)
        ct = ContentType.objects.get_for_model(project)
        return models.MatrixRoom.objects.create(
            room_id=room_id,
            room_name=room_name,
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

    def _join_room(self, room, user):
        """Register the user as a room member.

        Rooms created directly in tests skip the project-member sync, so the
        MatrixRoomMember row that gates conversation access must be added
        explicitly.
        """
        return models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id=matrix_client.ensure_user_exists(user),
            membership_state=models.MembershipStates.JOINED,
        )

    def test_credentials_with_room_uuid_invites_and_joins_user(self):
        """
        Regression test for M_FORBIDDEN bug.

        After calling GET /api/matrix/credentials/?room_uuid=<uuid>,
        the returned access_token must be able to PUT a typing indicator
        to the homeserver without getting M_FORBIDDEN.
        """
        room = self._create_real_room_for_project(self.fixture_project)
        self._join_room(room, self.fixture_user)

        self.client.force_authenticate(self.fixture_user)
        response = self.client.get(
            "/api/matrix/credentials/",
            {"room_uuid": room.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # The response must include room_id and access_token
        self.assertIn("room_id", response.data)
        self.assertIn("access_token", response.data)
        self.assertEqual(response.data["room_id"], room.room_id)

        access_token = response.data["access_token"]
        room_id = response.data["room_id"]
        matrix_user_id = response.data["matrix_user_id"]

        # Use the returned token to send a typing indicator directly to the homeserver.
        # This would fail with 403 M_FORBIDDEN if the user was only invited but not joined.
        typing_resp = httpx.put(
            f"{HOMESERVER_URL}/_matrix/client/v3/rooms/{room_id}/typing/{matrix_user_id}",
            json={"typing": True, "timeout": 5000},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=5,
        )
        self.assertEqual(
            typing_resp.status_code,
            200,
            f"Typing indicator failed — user not joined? Response: {typing_resp.text}",
        )

    def test_credentials_multiple_users_same_room(self):
        """Two different users should both be able to get credentials for the same room."""
        room = self._create_real_room_for_project(self.fixture_project)

        user2 = structure_factories.UserFactory(username=_unique_name("creduser2"))
        from waldur_core.permissions.fixtures import ProjectRole

        self.fixture_project.add_user(user2, ProjectRole.MANAGER)

        self._join_room(room, self.fixture_user)
        self._join_room(room, user2)

        # First user
        self.client.force_authenticate(self.fixture_user)
        resp1 = self.client.get(
            "/api/matrix/credentials/", {"room_uuid": room.uuid.hex}
        )
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        self.assertIn("access_token", resp1.data)

        # Second user
        self.client.force_authenticate(user2)
        resp2 = self.client.get(
            "/api/matrix/credentials/", {"room_uuid": room.uuid.hex}
        )
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertIn("access_token", resp2.data)

        # Both tokens should be different
        self.assertNotEqual(resp1.data["access_token"], resp2.data["access_token"])

    def test_credentials_without_room_uuid_returns_no_room_id(self):
        """Without room_uuid param, response should not include room_id or access_token."""
        # Ensure user is provisioned
        matrix_client.ensure_user_exists(self.fixture_user)

        self.client.force_authenticate(self.fixture_user)
        response = self.client.get("/api/matrix/credentials/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("room_id", response.data)
        self.assertNotIn("access_token", response.data)


@override_config(**MATRIX_CONFIG)
class MatrixCreateRoomTaskIntegrationTest(TestCase):
    """Test the create_room Celery task against the real homeserver."""

    def setUp(self):
        super().setUp()
        _ensure_bot()

    def test_create_room_task_provisions_real_room(self):
        """tasks.create_room() should create a real Matrix room and transition CREATING→ACTIVE."""
        project = structure_factories.ProjectFactory(name=_unique_name("taskproj"))
        room = _make_room_for_project(project)
        self.assertEqual(room.state, models.RoomStates.CREATING)

        # Call the task synchronously
        tasks.create_room(str(room.uuid))

        room.refresh_from_db()
        self.assertEqual(room.state, models.RoomStates.ACTIVE)
        self.assertTrue(room.room_id.startswith("!"))


@override_config(**MATRIX_CONFIG)
class MatrixSyncMembersTaskIntegrationTest(test.APITestCase):
    """Test sync_project_members_to_room task against the real homeserver."""

    def setUp(self):
        super().setUp()
        _ensure_bot()

    def test_sync_provisions_users_and_creates_members(self):
        project = structure_factories.ProjectFactory(name=_unique_name("syncproj"))
        user = structure_factories.UserFactory(username=_unique_name("syncuser"))
        from waldur_core.permissions.fixtures import ProjectRole

        project.add_user(user, ProjectRole.ADMIN)

        # Create a real room
        room_id, _ = matrix_client.create_room(_unique_name("syncroom"))
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id=room_id,
            room_name=f"Project: {project.name}",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

        tasks.sync_project_members_to_room(str(room.uuid))

        # User should now have a MatrixUserProfile
        self.assertTrue(
            models.MatrixUserProfile.objects.filter(
                user=user, provisioned=True
            ).exists()
        )
        # Member record should exist
        member = models.MatrixRoomMember.objects.get(room=room, user=user)
        self.assertEqual(member.membership_state, models.MembershipStates.INVITED)
        self.assertGreater(member.power_level, 0)  # Admin gets power level 50


@override_config(**MATRIX_CONFIG)
class MatrixDisableRoomIntegrationTest(test.APITestCase):
    """
    Integration test for the DISABLING state flow.

    Tests the full disable lifecycle:
    POST .../disable/ → DISABLING → tasks.disable_room() → ARCHIVED
    Also verifies that disable_room() skips rooms not in DISABLING state.
    """

    def setUp(self):
        super().setUp()
        _ensure_bot()

    def _create_active_room_with_member(self):
        """Create a real active room with one member."""
        project = structure_factories.ProjectFactory(name=_unique_name("disproj"))
        user = structure_factories.UserFactory(username=_unique_name("disuser"))
        from waldur_core.permissions.fixtures import CustomerRole

        project.customer.add_user(user, CustomerRole.OWNER)

        room_id, _ = matrix_client.create_room(_unique_name("disroom"))
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id=room_id,
            room_name=f"Project: {project.name}",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
            created_by=user,
        )

        # Provision and invite member
        matrix_user_id = matrix_client.ensure_user_exists(user)
        matrix_client.invite_user(room_id, matrix_user_id)
        access_token = matrix_client.get_access_token_for_user(user)
        matrix_client.join_room_as_self(room_id, access_token)
        models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id=matrix_user_id,
            power_level=50,
            membership_state=models.MembershipStates.JOINED,
        )
        return room, user, project

    def test_disable_room_full_lifecycle(self):
        """POST disable → DISABLING, then task → ARCHIVED."""
        room, owner, project = self._create_active_room_with_member()

        # Transition to DISABLING via FSM
        room.begin_disabling()
        room.save(update_fields=["state"])

        room.refresh_from_db()
        self.assertEqual(room.state, models.RoomStates.DISABLING)

        # Run the disable task synchronously
        tasks.disable_room(str(room.uuid), delete_history=False, reason="test")

        room.refresh_from_db()
        self.assertEqual(room.state, models.RoomStates.ARCHIVED)

        # All members should be LEFT
        for member in room.members.all():
            self.assertEqual(member.membership_state, models.MembershipStates.LEFT)

    def test_disable_room_skips_non_disabling_state(self):
        """disable_room() should skip if room is not in DISABLING state."""
        room, _, _ = self._create_active_room_with_member()
        self.assertEqual(room.state, models.RoomStates.ACTIVE)

        # Call disable_room without transitioning to DISABLING first
        tasks.disable_room(str(room.uuid))

        room.refresh_from_db()
        # Should still be ACTIVE — task should have skipped
        self.assertEqual(room.state, models.RoomStates.ACTIVE)

    def test_disable_via_api_endpoint(self):
        """POST /api/matrix/rooms/<uuid>/disable/ should trigger the full flow."""
        room, owner, project = self._create_active_room_with_member()

        self.client.force_authenticate(owner)
        url = f"/api/matrix/rooms/{room.uuid.hex}/disable/"
        response = self.client.post(url, {"delete_history": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        room.refresh_from_db()
        self.assertEqual(room.state, models.RoomStates.DISABLING)

        # Now run the task synchronously (normally dispatched by Celery)
        tasks.disable_room(str(room.uuid), delete_history=False, reason="test")

        room.refresh_from_db()
        self.assertEqual(room.state, models.RoomStates.ARCHIVED)


@override_config(**MATRIX_CONFIG)
class MatrixRoomAPIIntegrationTest(test.APITestCase):
    """Test the full room API flow: create via POST → task → send message → retrieve."""

    def setUp(self):
        super().setUp()
        _ensure_bot()

    def test_full_api_room_create_flow(self):
        """POST /api/matrix/rooms/ → create_room task → room becomes ACTIVE."""
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(
            customer=customer, name=_unique_name("apiproj")
        )
        owner = structure_factories.UserFactory(username=_unique_name("apiowner"))
        from waldur_core.permissions.fixtures import CustomerRole

        customer.add_user(owner, CustomerRole.OWNER)

        self.client.force_authenticate(owner)

        # Create room via API
        response = self.client.post(
            "/api/matrix/rooms/",
            {"project": project.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        room_uuid = response.data["uuid"]

        # Run the task synchronously (normally dispatched by Celery)
        tasks.create_room(room_uuid)

        room = models.MatrixRoom.objects.get(uuid=room_uuid)
        self.assertEqual(room.state, models.RoomStates.ACTIVE)
        self.assertTrue(room.room_id.startswith("!"))

    def test_room_visibility_staff_vs_unrelated_user(self):
        """Staff can see all rooms; unrelated users see none."""
        project = structure_factories.ProjectFactory(name=_unique_name("visproj"))
        room_id, _ = matrix_client.create_room(_unique_name("visroom"))
        ct = ContentType.objects.get_for_model(project)
        models.MatrixRoom.objects.create(
            room_id=room_id,
            room_name="Visibility Test",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

        # Staff can see it
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff_user)
        resp = self.client.get("/api/matrix/rooms/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        room_ids = [r["room_id"] for r in resp.data]
        self.assertIn(room_id, room_ids)

        # Unrelated user cannot
        stranger = structure_factories.UserFactory(username=_unique_name("stranger"))
        self.client.force_authenticate(stranger)
        resp = self.client.get("/api/matrix/rooms/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        room_ids = [r["room_id"] for r in resp.data]
        self.assertNotIn(room_id, room_ids)


@override_config(**MATRIX_CONFIG)
class MatrixMessageFlowIntegrationTest(TestCase):
    """
    Complete message lifecycle: create room → provision user → invite →
    join → set power level → send message → retrieve → kick.
    """

    def setUp(self):
        super().setUp()
        _ensure_bot()

    def test_complete_message_lifecycle(self):
        # 1. Create room
        room_id, _ = matrix_client.create_room(_unique_name("lifecycle"))

        # 2. Provision user
        user = structure_factories.UserFactory(username=_unique_name("lcuser"))
        matrix_user_id = matrix_client.ensure_user_exists(user)

        # 3. Invite
        matrix_client.invite_user(room_id, matrix_user_id)

        # 4. Join
        access_token = matrix_client.get_access_token_for_user(user)
        matrix_client.join_room_as_self(room_id, access_token)

        # 5. Set power level
        matrix_client.set_power_level(room_id, matrix_user_id, 50)

        # 6. Send message as bot
        event_id = matrix_client.send_message(room_id, "Bot says hello")
        self.assertTrue(event_id.startswith("$"))

        # 7. Retrieve messages
        result = matrix_client.get_room_messages(room_id, limit=50)
        bodies = [m.get("body") for m in result["messages"] if m.get("body")]
        self.assertIn("Bot says hello", bodies)

        # 8. Kick user
        result = matrix_client.kick_user(room_id, matrix_user_id, reason="test done")
        self.assertTrue(result)

    def test_user_can_send_typing_after_join(self):
        """After invite+join, user's own token can send typing indicators."""
        room_id, _ = matrix_client.create_room(_unique_name("typingflow"))
        user = structure_factories.UserFactory(username=_unique_name("typuser"))
        matrix_user_id = matrix_client.ensure_user_exists(user)

        matrix_client.invite_user(room_id, matrix_user_id)
        token = matrix_client.get_access_token_for_user(user)
        matrix_client.join_room_as_self(room_id, token)

        typing_resp = httpx.put(
            f"{HOMESERVER_URL}/_matrix/client/v3/rooms/{room_id}/typing/{matrix_user_id}",
            json={"typing": True, "timeout": 5000},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        self.assertEqual(
            typing_resp.status_code,
            200,
            f"Typing indicator failed: {typing_resp.text}",
        )
