from unittest import mock

from constance.test import override_config
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import models
from waldur_mastermind.matrix_chat.tests import fixtures

# is_enabled() requires all three; the write guard rejects mutations otherwise.
MATRIX_ENABLED_CONFIG = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="as-token",
)


class MatrixRoomListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.url = "/api/matrix/rooms/"

    def test_owner_can_list_rooms(self):
        self.fixture.matrix_room  # ensure room exists
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_anonymous_cannot_list_rooms(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_member_summary_includes_matrix_user_id(self):
        member = self.fixture.matrix_room_member
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        members = response.data[0]["members"]
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["matrix_user_id"], member.matrix_user_id)
        self.assertEqual(members[0]["user_full_name"], member.user.full_name)


@override_config(**MATRIX_ENABLED_CONFIG)
class MatrixRoomCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.url = "/api/matrix/rooms/"

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_staff_can_create_room(self, mock_tasks):
        # Use a fresh project without a room
        from waldur_core.structure.tests import factories as structure_factories

        project = structure_factories.ProjectFactory(customer=self.fixture.customer)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url,
            {"project": project.uuid.hex, "room_name": "My Room"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["room_name"], project.name)
        mock_tasks.create_room.delay.assert_called_once()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_owner_cannot_create_room(self, mock_tasks):
        # Demo policy: only staff/support provision rooms, not customer owners.
        from waldur_core.structure.tests import factories as structure_factories

        project = structure_factories.ProjectFactory(customer=self.fixture.customer)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url,
            {"project": project.uuid.hex, "room_name": "My Room"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_tasks.create_room.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_cannot_create_duplicate_room(self, mock_tasks):
        self.fixture.matrix_room  # ensure room exists
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url,
            {"project": self.fixture.project.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_owner_cannot_create_room(self):
        from waldur_core.structure.tests import factories as structure_factories

        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.post(
            self.url,
            {"project": self.fixture.project.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_config(**MATRIX_ENABLED_CONFIG)
class MatrixRoomActionsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_owner_can_sync_members(self, mock_tasks):
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/sync_members/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_tasks.sync_project_members_to_room.delay.assert_called_once()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_owner_can_export_history(self, mock_tasks):
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/export_history/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_tasks.export_room_history.delay.assert_called_once()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_owner_can_retry_creating_room(self, mock_tasks):
        self.room.state = models.RoomStates.CREATING
        self.room.save(update_fields=["state"])

        url = f"/api/matrix/rooms/{self.room.uuid.hex}/retry/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_tasks.create_room.delay.assert_called_once_with(self.room.uuid.hex)
        mock_tasks.disable_room.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_owner_can_retry_erred_room(self, mock_tasks):
        self.room.state = models.RoomStates.ERROR
        self.room.error_message = "boom"
        self.room.save(update_fields=["state", "error_message"])

        url = f"/api/matrix/rooms/{self.room.uuid.hex}/retry/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.room.refresh_from_db()
        # ERROR rooms transition back to CREATING and clear the error message
        # before the create task is re-dispatched.
        self.assertEqual(self.room.state, models.RoomStates.CREATING)
        self.assertEqual(self.room.error_message, "")
        mock_tasks.create_room.delay.assert_called_once_with(self.room.uuid.hex)
        mock_tasks.disable_room.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_owner_can_retry_disabling_room(self, mock_tasks):
        self.room.state = models.RoomStates.DISABLING
        self.room.save(update_fields=["state"])

        url = f"/api/matrix/rooms/{self.room.uuid.hex}/retry/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        # The original delete_history choice is not persisted on the row, so
        # retry defaults to False (non-destructive).
        mock_tasks.disable_room.delay.assert_called_once_with(
            self.room.uuid.hex,
            delete_history=False,
        )
        mock_tasks.create_room.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_cannot_retry_active_room(self, mock_tasks):
        # ACTIVE is a stable state — nothing to retry.
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/retry/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        mock_tasks.create_room.delay.assert_not_called()
        mock_tasks.disable_room.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_cannot_retry_archived_room(self, mock_tasks):
        # ARCHIVED is terminal — re-enable goes through the reactivate action.
        self.room.state = models.RoomStates.ARCHIVED
        self.room.save(update_fields=["state"])

        url = f"/api/matrix/rooms/{self.room.uuid.hex}/retry/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        mock_tasks.create_room.delay.assert_not_called()
        mock_tasks.disable_room.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_owner_can_reactivate_room(self, mock_tasks):
        self.room.state = models.RoomStates.ARCHIVED
        self.room.save(update_fields=["state"])

        url = f"/api/matrix/rooms/{self.room.uuid.hex}/reactivate/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.ACTIVE)
        mock_tasks.sync_project_members_to_room.delay.assert_called_once_with(
            self.room.uuid.hex
        )
        # The bot announces the reactivation without naming the initiator: only
        # staff/owners can reactivate, so attribution adds noise, not value.
        mock_tasks.send_room_notification.delay.assert_called_once_with(
            self.room.uuid.hex, "Chat room was reactivated"
        )


class MatrixHistoryExportListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.url = "/api/matrix/exports/"

    def test_can_list_exports(self):
        self.fixture.history_export  # ensure export exists
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_anonymous_cannot_list_exports(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class MatrixRoomMemberFilterTest(test.APITestCase):
    """?member=true narrows the room list to rooms the caller belongs to.

    The user-facing chat list passes the param; the staff admin view omits it
    and keeps seeing every room.
    """

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.url = "/api/matrix/rooms/"
        self.room = self.fixture.matrix_room

    def _create_other_room(self):
        project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        ct = ContentType.objects.get_for_model(project)
        return models.MatrixRoom.objects.create(
            room_id="!other:matrix.example.com",
            room_name="Other room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )

    def _add_member(self, user, membership_state):
        return models.MatrixRoomMember.objects.create(
            room=self.room,
            user=user,
            matrix_user_id=f"@{user.username}:matrix.example.com",
            membership_state=membership_state,
        )

    def test_member_filter_returns_only_rooms_the_user_belongs_to(self):
        self._create_other_room()
        self._add_member(self.fixture.staff, models.MembershipStates.JOINED)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"member": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        room_ids = {r["room_id"] for r in response.data}
        self.assertEqual(room_ids, {self.room.room_id})

    def test_without_member_filter_staff_sees_all_rooms(self):
        other = self._create_other_room()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        room_ids = {r["room_id"] for r in response.data}
        self.assertIn(self.room.room_id, room_ids)
        self.assertIn(other.room_id, room_ids)

    def test_member_filter_excludes_rooms_the_user_has_left(self):
        self._add_member(self.fixture.staff, models.MembershipStates.LEFT)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"member": "true"})

        room_ids = {r["room_id"] for r in response.data}
        self.assertNotIn(self.room.room_id, room_ids)

    def test_member_filter_excludes_role_accessible_non_member_rooms(self):
        # The customer owner has role-based access to the room but is not a
        # member, so the member filter must exclude it.
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url, {"member": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(list(response.data), [])


class EligibleProjectsTest(test.APITestCase):
    """GET /api/matrix/rooms/eligible_projects/ lists projects the caller
    can create a new MatrixRoom for: caller is customer owner (or staff),
    and the project has no existing MatrixRoom row."""

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        # Materialize project up front — the lazy cached_property would otherwise
        # only fire on the assertion line, after the API call has run.
        self.project = self.fixture.project
        self.url = "/api/matrix/rooms/eligible_projects/"

    def _project_uuids(self, response):
        return {p["uuid"] for p in response.data}

    def test_anonymous_cannot_list_eligible_projects(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_owner_sees_project_without_room(self):
        # fixture.project has no room
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.fixture.project.uuid.hex, self._project_uuids(response))

    def test_owner_does_not_see_project_with_existing_room(self):
        self.fixture.matrix_room  # active room on fixture.project
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(self.fixture.project.uuid.hex, self._project_uuids(response))

    def test_owner_does_not_see_project_with_archived_room(self):
        # Archived rooms still occupy the (content_type, object_id) unique
        # slot — creating a new room would fail, so the project is not eligible.
        room = self.fixture.matrix_room
        room.state = models.RoomStates.ARCHIVED
        room.save(update_fields=["state"])

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)

        self.assertNotIn(self.fixture.project.uuid.hex, self._project_uuids(response))

    def test_owner_does_not_see_projects_from_other_customers(self):
        other_customer = structure_factories.CustomerFactory()
        other_project = structure_factories.ProjectFactory(customer=other_customer)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)

        self.assertNotIn(other_project.uuid.hex, self._project_uuids(response))

    def test_non_owner_sees_no_projects(self):
        # Project admin/manager/member are not customer owners and cannot
        # create rooms, so eligible_projects must return empty for them.
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(list(response.data), [])

    def test_staff_sees_eligible_projects_across_customers(self):
        other_customer = structure_factories.CustomerFactory()
        other_project = structure_factories.ProjectFactory(customer=other_customer)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        uuids = self._project_uuids(response)
        self.assertIn(self.fixture.project.uuid.hex, uuids)
        self.assertIn(other_project.uuid.hex, uuids)

    def test_customer_uuid_filter_narrows_results(self):
        # A second customer the same owner is also OWNER of.
        from waldur_core.permissions.fixtures import CustomerRole

        other_customer = structure_factories.CustomerFactory()
        other_customer.add_user(self.fixture.owner, CustomerRole.OWNER)
        other_project = structure_factories.ProjectFactory(customer=other_customer)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            self.url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )

        uuids = self._project_uuids(response)
        self.assertIn(self.fixture.project.uuid.hex, uuids)
        self.assertNotIn(other_project.uuid.hex, uuids)

    def test_response_includes_customer_metadata(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)

        entry = next(
            p for p in response.data if p["uuid"] == self.fixture.project.uuid.hex
        )
        self.assertEqual(entry["name"], self.fixture.project.name)
        self.assertEqual(entry["customer_uuid"], self.fixture.customer.uuid.hex)
        self.assertEqual(entry["customer_name"], self.fixture.customer.name)


class CurrentUserMembershipStateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room

    def test_reflects_joined_membership(self):
        models.MatrixRoomMember.objects.create(
            room=self.room,
            user=self.fixture.staff,
            matrix_user_id="@staff:matrix.example.com",
            membership_state=models.MembershipStates.JOINED,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(f"/api/matrix/rooms/{self.room.uuid.hex}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["current_user_membership_state"], "joined")

    def test_null_for_non_member(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(f"/api/matrix/rooms/{self.room.uuid.hex}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["current_user_membership_state"])


@override_config(**MATRIX_ENABLED_CONFIG)
class MatrixRoomJoinLeaveTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_staff_can_join(self, mock_tasks):
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/join/"
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_tasks.staff_join_room.delay.assert_called_once_with(
            self.room.uuid.hex, self.fixture.staff.uuid.hex
        )

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_owner_cannot_join(self, mock_tasks):
        # The customer owner has role-based access to the room but is not staff.
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/join/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_tasks.staff_join_room.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_cannot_join_inactive_room(self, mock_tasks):
        self.room.state = models.RoomStates.ARCHIVED
        self.room.save(update_fields=["state"])
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/join/"
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        mock_tasks.staff_join_room.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_staff_can_leave(self, mock_tasks):
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/leave/"
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_tasks.staff_leave_room.delay.assert_called_once_with(
            self.room.uuid.hex, self.fixture.staff.uuid.hex
        )


@override_config(**MATRIX_ENABLED_CONFIG)
class MatrixRoomTeardownPermissionTest(test.APITestCase):
    """Demo policy: owners manage rooms but only staff/support tear them down."""

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_owner_cannot_disable_room(self, mock_tasks):
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/disable/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url, {"delete_history": False})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_tasks.disable_room.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_staff_can_disable_room(self, mock_tasks):
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/disable/"
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url, {"delete_history": False})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.DISABLING)

    def test_owner_cannot_destroy_room(self):
        self.room.state = models.RoomStates.ARCHIVED
        self.room.save(update_fields=["state"])
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.MatrixRoom.objects.filter(uuid=self.room.uuid).exists())

    def test_staff_can_destroy_room(self):
        self.room.state = models.RoomStates.ARCHIVED
        self.room.save(update_fields=["state"])
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/"
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.MatrixRoom.objects.filter(uuid=self.room.uuid).exists())


@override_config(**MATRIX_ENABLED_CONFIG)
class MatrixRoomFsmEdgeTest(test.APITestCase):
    """The state-mutating actions wrap django_fsm transitions in try/except —
    an invalid transition must surface as a clean 400, not a 500."""

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_disable_already_disabling_room_rejects(self, mock_tasks):
        self.room.state = models.RoomStates.DISABLING
        self.room.save(update_fields=["state"])
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/disable/"
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url, {"delete_history": False})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_tasks.disable_room.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks")
    def test_reactivate_active_room_rejects(self, mock_tasks):
        url = f"/api/matrix/rooms/{self.room.uuid.hex}/reactivate/"
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_tasks.sync_project_members_to_room.delay.assert_not_called()


@override_config(MATRIX_ENABLED=True)
class MatrixCredentialsGateTest(test.APITestCase):
    """The credentials endpoint is gated on MATRIX_ENABLED + a per-user
    rate limit. The flag-off path must 404 (not 200, not 403) to avoid
    leaking the endpoint's existence."""

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.url = "/api/matrix/credentials/"

    def test_disabled_returns_404(self):
        with override_config(MATRIX_ENABLED=False):
            self.client.force_authenticate(self.fixture.staff)
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class MatrixHistoryExportDownloadTest(test.APITestCase):
    """Export files are served through a permission-checking view, not the
    raw FileField URL. Members get the bytes; non-members get 404 (matching
    the rest of the API's leak-resistant denial style)."""

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.export = self.fixture.history_export
        self.export.export_file.save("test.json", ContentFile(b'{"hello":"world"}'))
        self.export.media_file.save("test.zip", ContentFile(b"PK\x03\x04"))

    def _url(self, kind):
        return f"/api/matrix/exports/{self.export.uuid}/download/{kind}/"

    def test_owner_can_download_export(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self._url("export"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_export_served_as_binary_attachment_not_json(self):
        # The export file is JSON, but it must be streamed as an opaque
        # attachment. Served as application/json (Django's default guess for
        # a .json file), the SPA's get<Blob>() helper parses it instead of
        # returning a Blob, so URL.createObjectURL throws and the download
        # fails with "Type error".
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self._url("export"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", response["Content-Disposition"])

    def test_outsider_gets_404(self):
        other = structure_factories.UserFactory()
        self.client.force_authenticate(other)
        response = self.client.get(self._url("export"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_gets_401(self):
        response = self.client.get(self._url("export"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unknown_kind_returns_404(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            f"/api/matrix/exports/{self.export.uuid}/download/totallyfake/"
        )
        # Bogus 'kind' values fall back to media_file lookup, which is empty
        # for the unknown branch — still a 404 either way.
        self.assertIn(
            response.status_code,
            (status.HTTP_404_NOT_FOUND, status.HTTP_400_BAD_REQUEST),
        )


class MatrixWriteGuardTest(test.APITestCase):
    """With Matrix disabled, writes that would only strand rooms in a transient
    state (their tasks no-op against no homeserver) are rejected, while reads
    stay available so the admin rooms list remains viewable. Default constance
    leaves Matrix disabled, so no override is needed here."""

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room

    def test_disabled_blocks_room_creation(self):
        project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            "/api/matrix/rooms/",
            {"project": project.uuid.hex, "room_name": "My Room"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disabled_blocks_room_action(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            f"/api/matrix/rooms/{self.room.uuid.hex}/sync_members/"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disabled_blocks_reprovision(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post("/api/admin/matrix/reprovision/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disabled_still_allows_listing(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get("/api/matrix/rooms/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
