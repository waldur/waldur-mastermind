from unittest import mock

import jwt
from constance.test import override_config
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import (
    livekit_client,
    matrix_client,
    models,
    tasks,
)
from waldur_mastermind.matrix_chat.matrix_client import MatrixClientError
from waldur_mastermind.matrix_chat.tests import fixtures


class CallNamingTest(test.APITestCase):
    # Values that lk-jwt-service produced for the same inputs: a call keeps its
    # room and identities whichever service issued the token.
    def test_room_name_matches_lk_jwt_service(self):
        self.assertEqual(
            livekit_client.call_room_name("!notaroom699:localhost"),
            "m17+DYPdIQ518Ep8kwcwoopwmIFICEC0dVOPAuUul0I",
        )

    def test_identity_matches_lk_jwt_service(self):
        self.assertEqual(
            livekit_client.call_identity("@owner:localhost", "probe"),
            "dvsVGufQ3kd1wnBJ8QWTqtnQAFaxts7nnj3QSYkj7lI",
        )


@override_config(
    MATRIX_HOMESERVER_URL="http://tuwunel.internal:6167",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
class IsJoinedTest(test.APITestCase):
    def _response(self, status_code, body):
        response = mock.Mock(status_code=status_code, text=str(body))
        response.json.return_value = body
        return response

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.httpx.request")
    def test_asks_as_the_user(self, mock_request):
        mock_request.return_value = self._response(
            200, {"joined_rooms": ["!a:example.com"]}
        )

        self.assertTrue(matrix_client.is_joined("@u:example.com", "!a:example.com"))
        self.assertFalse(matrix_client.is_joined("@u:example.com", "!b:example.com"))
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["params"], {"user_id": "@u:example.com"})

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.httpx.request")
    def test_refusal_raises(self, mock_request):
        mock_request.return_value = self._response(403, {"errcode": "M_FORBIDDEN"})

        with self.assertRaises(MatrixClientError):
            matrix_client.is_joined("@u:example.com", "!a:example.com")


@override_config(
    MATRIX_LIVEKIT_KEY="devkey",
    MATRIX_LIVEKIT_SECRET="devsecret",
    MATRIX_LIVEKIT_URL="http://livekit:7880",
)
class CreateCallRoomTest(test.APITestCase):
    @mock.patch("waldur_mastermind.matrix_chat.livekit_client.httpx.post")
    def test_creates_room_with_a_room_create_grant(self, mock_post):
        mock_post.return_value = mock.Mock(status_code=200)
        mock_post.return_value.json.return_value = {"name": "r"}

        livekit_client.create_call_room("r")

        args, kwargs = mock_post.call_args
        self.assertEqual(
            args[0], "http://livekit:7880/twirp/livekit.RoomService/CreateRoom"
        )
        self.assertEqual(kwargs["json"]["name"], "r")
        token = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, "devsecret", algorithms=["HS256"])
        self.assertTrue(claims["video"]["roomCreate"])


LIVEKIT = override_config(
    MATRIX_LIVEKIT_KEY="devkey",
    MATRIX_LIVEKIT_SECRET="devsecret",
    MATRIX_LIVEKIT_URL="http://livekit:7880",
)
ROOM_ID = "!test_room:matrix.example.com"


def _participant(identity, matrix_user_id=None):
    attributes = (
        {livekit_client.MATRIX_USER_ATTRIBUTE: matrix_user_id} if matrix_user_id else {}
    )
    return {"identity": identity, "attributes": attributes}


@LIVEKIT
class RemoveFromCallTest(test.APITestCase):
    @mock.patch("waldur_mastermind.matrix_chat.livekit_client._twirp_call")
    def test_removes_only_the_users_participants(self, mock_twirp):
        mock_twirp.side_effect = lambda method, body, **kw: (
            {
                "participants": [
                    _participant("a1", "@alice:example.com"),
                    _participant("b1", "@bob:example.com"),
                    _participant("a2", "@alice:example.com"),
                    _participant("x1"),
                ]
            }
            # The web chat's call; the Element Call one is empty.
            if method == "ListParticipants"
            and body["room"] == livekit_client.call_room_name(ROOM_ID)
            else {}
        )

        removed = livekit_client.remove_from_call(ROOM_ID, "@alice:example.com")

        self.assertEqual(removed, 2)
        room_name = livekit_client.call_room_name(ROOM_ID)
        removals = [
            c.args[1]
            for c in mock_twirp.call_args_list
            if c.args[0] == "RemoveParticipant"
        ]
        self.assertEqual(
            removals,
            [
                {"room": room_name, "identity": "a1"},
                {"room": room_name, "identity": "a2"},
            ],
        )

    @mock.patch("waldur_mastermind.matrix_chat.livekit_client._twirp_call")
    def test_ending_a_call_that_is_not_going_on_is_fine(self, mock_twirp):
        mock_twirp.side_effect = livekit_client.LiveKitClientError("gone", 404)

        livekit_client.end_call(ROOM_ID)


@LIVEKIT
@mock.patch("waldur_mastermind.matrix_chat.tasks.livekit_client.remove_from_call")
@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class RemovalEndsCallPresenceTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room
        self.member = self.fixture.matrix_room_member
        self.user = self.fixture.admin

    def test_kick_member(self, mock_matrix, mock_remove):
        # No role any more, so the retry-guard lets the kick through.
        self.fixture.project.remove_user(self.user)

        tasks.kick_member(self.member.uuid.hex, "Role revoked")

        mock_remove.assert_called_once_with(ROOM_ID, self.member.matrix_user_id)
        self.member.refresh_from_db()
        self.assertEqual(self.member.membership_state, models.MembershipStates.LEFT)

    def test_a_livekit_failure_does_not_fail_the_kick(self, mock_matrix, mock_remove):
        mock_remove.side_effect = livekit_client.LiveKitClientError("down")
        self.fixture.project.remove_user(self.user)

        tasks.kick_member(self.member.uuid.hex, "Role revoked")

        mock_matrix.kick_user.assert_called_once()
        self.member.refresh_from_db()
        self.assertEqual(self.member.membership_state, models.MembershipStates.LEFT)

    def test_end_matrix_access(self, mock_matrix, mock_remove):
        mock_matrix.logout_all_devices.return_value = None
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        tasks.end_matrix_access(self.user.uuid.hex)

        mock_remove.assert_called_once_with(ROOM_ID, self.member.matrix_user_id)

    def test_staff_leave_room_without_a_membership_row(self, mock_matrix, mock_remove):
        staff = structure_factories.UserFactory(is_staff=True)
        models.MatrixUserProfile.objects.create(
            user=staff, matrix_user_id="@staff:matrix.example.com"
        )

        tasks.staff_leave_room(self.room.uuid.hex, staff.uuid.hex)

        mock_remove.assert_called_once_with(ROOM_ID, "@staff:matrix.example.com")

    def test_kick_user_from_room(self, mock_matrix, mock_remove):
        self.fixture.project.remove_user(self.user)

        tasks.kick_user_from_room(self.room.uuid.hex, self.user.uuid.hex)

        mock_remove.assert_called_once_with(ROOM_ID, self.member.matrix_user_id)

    def test_end_deleted_user_access(self, mock_matrix, mock_remove):
        mock_matrix.get_bot_user_id.return_value = "@bot:matrix.example.com"
        mock_matrix.MatrixUserNotFound = matrix_client.MatrixUserNotFound
        mock_matrix.set_password.side_effect = matrix_client.MatrixUserNotFound()

        with mock.patch.object(tasks, "_sign_out_all", return_value=(False, None)):
            tasks.end_deleted_user_access("@gone:matrix.example.com", [ROOM_ID])

        mock_remove.assert_called_once_with(ROOM_ID, "@gone:matrix.example.com")

    @mock.patch("waldur_mastermind.matrix_chat.tasks.livekit_client.end_call")
    def test_disable_room_ends_the_call(self, mock_end, mock_matrix, mock_remove):
        models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=models.RoomStates.DISABLING
        )

        tasks.disable_room(self.room.uuid.hex, delete_history=True)

        mock_end.assert_called_once_with(ROOM_ID)
        mock_remove.assert_called_once_with(ROOM_ID, self.member.matrix_user_id)
        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.livekit_client.end_call")
    def test_disable_room_survives_a_livekit_failure(
        self, mock_end, mock_matrix, mock_remove
    ):
        mock_end.side_effect = livekit_client.LiveKitClientError("down")
        models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=models.RoomStates.DISABLING
        )

        tasks.disable_room(self.room.uuid.hex, delete_history=True)

        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
