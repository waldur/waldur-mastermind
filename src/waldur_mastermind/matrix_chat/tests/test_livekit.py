"""Tests for the LiveKit Calls observability endpoints.

No real network egress — ``httpx.post`` and the ``livekit_client`` helpers are
mocked at the module boundary. CI does not run the matrix-rtc Compose profile.
"""

from unittest import mock

import jwt
from constance.test import override_config
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import livekit_client

livekit_configured = override_config(
    MATRIX_LIVEKIT_KEY="devkey",
    MATRIX_LIVEKIT_SECRET="devsecret",
    MATRIX_LIVEKIT_URL="http://livekit:7880",
)
livekit_unconfigured = override_config(
    MATRIX_LIVEKIT_KEY="",
    MATRIX_LIVEKIT_SECRET="",
)

OVERVIEW_URL = "/api/admin/matrix/livekit/overview/"
PARTICIPANTS_URL = "/api/admin/matrix/livekit/participants/"


def _room(name="room1", participants=2, publishers=1):
    return {
        "sid": "RM_test",
        "name": name,
        "num_participants": participants,
        "num_publishers": publishers,
        "creation_time": 1700000000,
        "max_participants": 0,
        "metadata": "",
    }


def _http_response(status_code=200, json_body=None, text=""):
    response = mock.Mock(status_code=status_code, text=text)
    if json_body is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = json_body
    return response


class LiveKitClientTest(test.APITestCase):
    @livekit_configured
    def test_mint_token_round_trip(self):
        token = livekit_client._mint_admin_token()
        decoded = jwt.decode(token, "devsecret", algorithms=["HS256"])
        self.assertTrue(decoded["video"]["roomList"])
        self.assertTrue(decoded["video"]["roomAdmin"])
        self.assertEqual(decoded["iss"], "devkey")

    @livekit_configured
    def test_mint_token_scopes_room_admin_to_room(self):
        # LiveKit rejects ListParticipants unless roomAdmin names the room.
        token = livekit_client._mint_admin_token(room="room1")
        decoded = jwt.decode(token, "devsecret", algorithms=["HS256"])
        self.assertEqual(decoded["video"]["room"], "room1")
        self.assertTrue(decoded["video"]["roomAdmin"])

    @livekit_configured
    def test_mint_token_omits_room_when_unscoped(self):
        token = livekit_client._mint_admin_token()
        decoded = jwt.decode(token, "devsecret", algorithms=["HS256"])
        self.assertNotIn("room", decoded["video"])

    @livekit_unconfigured
    def test_is_configured_false_when_settings_missing(self):
        self.assertFalse(livekit_client.is_configured())

    @livekit_configured
    @mock.patch("waldur_mastermind.matrix_chat.livekit_client.httpx.post")
    def test_list_rooms_normalizes_snakecase(self, mock_post):
        # livekit-server returns snake_case JSON, int64 fields as strings.
        mock_post.return_value = _http_response(
            json_body={
                "rooms": [
                    {
                        "sid": "RM_1",
                        "name": "room1",
                        "num_participants": 3,
                        "num_publishers": 2,
                        "creation_time": "1700000000",
                        "max_participants": 0,
                        "metadata": "",
                    }
                ]
            }
        )
        rooms = livekit_client.list_rooms()
        self.assertEqual(rooms[0]["num_participants"], 3)
        self.assertEqual(rooms[0]["num_publishers"], 2)
        self.assertEqual(rooms[0]["creation_time"], 1700000000)

    @livekit_configured
    @mock.patch("waldur_mastermind.matrix_chat.livekit_client.httpx.post")
    def test_list_participants_normalizes_snakecase(self, mock_post):
        mock_post.return_value = _http_response(
            json_body={
                "participants": [
                    {
                        "sid": "PA_1",
                        "identity": "@alice:example.com",
                        "state": "ACTIVE",
                        "is_publisher": True,
                        "joined_at": "1700000100",
                        "tracks": [
                            {
                                "sid": "TR_1",
                                "name": "camera",
                                "type": "VIDEO",
                                "muted": False,
                                "width": 1280,
                                "height": 720,
                            }
                        ],
                    }
                ]
            }
        )
        participants = livekit_client.list_participants("room1")
        self.assertTrue(participants[0]["is_publisher"])
        self.assertEqual(participants[0]["joined_at"], 1700000100)
        self.assertEqual(participants[0]["tracks"][0]["width"], 1280)

    @livekit_configured
    @mock.patch("waldur_mastermind.matrix_chat.livekit_client.httpx.post")
    def test_non_json_200_raises_client_error(self, mock_post):
        # A health/proxy page answering 200 on the signalling port must not
        # escape as an unhandled 500.
        mock_post.return_value = _http_response(status_code=200, text="<html>")
        with self.assertRaises(livekit_client.LiveKitClientError):
            livekit_client.list_rooms()

    @livekit_configured
    @mock.patch("waldur_mastermind.matrix_chat.livekit_client.httpx.post")
    def test_rejected_token_carries_status_code(self, mock_post):
        mock_post.return_value = _http_response(
            status_code=401, text="invalid token: signature is invalid"
        )
        with self.assertRaises(livekit_client.LiveKitClientError) as ctx:
            livekit_client.list_rooms()
        self.assertEqual(ctx.exception.status_code, 401)


@livekit_configured
class LiveKitOverviewViewTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.user = structure_factories.UserFactory()

    @mock.patch("waldur_mastermind.matrix_chat.views.livekit_client.list_rooms")
    def test_staff_can_list(self, mock_list_rooms):
        mock_list_rooms.return_value = [
            _room("a", participants=2, publishers=1),
            _room("b", participants=3, publishers=2),
        ]
        self.client.force_authenticate(self.staff)
        response = self.client.get(OVERVIEW_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["rooms"]), 2)
        # Totals sum the table rows.
        self.assertEqual(response.data["totals"]["room_count"], 2)
        self.assertEqual(response.data["totals"]["participant_count"], 5)
        self.assertEqual(response.data["totals"]["publisher_count"], 3)
        self.assertEqual(
            response.data["livekit_url"], livekit_client.get_internal_url()
        )

    @mock.patch("waldur_mastermind.matrix_chat.views.livekit_client.list_rooms")
    def test_non_staff_gets_403(self, mock_list_rooms):
        self.client.force_authenticate(self.user)
        response = self.client.get(OVERVIEW_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_list_rooms.assert_not_called()

    def test_unauthenticated_gets_401(self):
        response = self.client.get(OVERVIEW_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @livekit_unconfigured
    def test_not_configured_returns_503(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(OVERVIEW_URL)
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @mock.patch("waldur_mastermind.matrix_chat.views.livekit_client.list_rooms")
    def test_livekit_unreachable_returns_502(self, mock_list_rooms):
        mock_list_rooms.side_effect = livekit_client.LiveKitClientError("boom")
        self.client.force_authenticate(self.staff)
        response = self.client.get(OVERVIEW_URL)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data["detail"], "LiveKit is unreachable.")

    @mock.patch("waldur_mastermind.matrix_chat.views.livekit_client.list_rooms")
    def test_rejected_credentials_returns_502_with_distinct_detail(
        self, mock_list_rooms
    ):
        mock_list_rooms.side_effect = livekit_client.LiveKitClientError(
            "bad token", status_code=401
        )
        self.client.force_authenticate(self.staff)
        response = self.client.get(OVERVIEW_URL)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(
            response.data["detail"], "LiveKit rejected the admin credentials."
        )


@livekit_configured
class LiveKitRoomParticipantsViewTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)

    @mock.patch("waldur_mastermind.matrix_chat.views.livekit_client.list_participants")
    def test_staff_can_list_participants(self, mock_list_participants):
        mock_list_participants.return_value = [
            {
                "sid": "PA_1",
                "identity": "@alice:example.com",
                "state": "ACTIVE",
                "is_publisher": True,
                "joined_at": 1700000100,
                "tracks": [],
            }
        ]
        self.client.force_authenticate(self.staff)
        response = self.client.get(PARTICIPANTS_URL, {"room": "room1"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["identity"], "@alice:example.com")
        mock_list_participants.assert_called_once_with("room1")

    @mock.patch("waldur_mastermind.matrix_chat.views.livekit_client.list_participants")
    def test_room_name_with_slash(self, mock_list_participants):
        # Element Call room names are base64 and routinely contain '/'.
        mock_list_participants.return_value = []
        room = "pms+HQlIqJ7+FpTCdSPxY6y+zH8J49QCkPj7OFU7SbA"
        self.client.force_authenticate(self.staff)
        response = self.client.get(PARTICIPANTS_URL, {"room": room})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_list_participants.assert_called_once_with(room)

    def test_missing_room_param_returns_400(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(PARTICIPANTS_URL)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_mastermind.matrix_chat.views.livekit_client.list_participants")
    def test_unknown_room_returns_empty(self, mock_list_participants):
        # LiveKit answers 200 with an empty list for an unknown room.
        mock_list_participants.return_value = []
        self.client.force_authenticate(self.staff)
        response = self.client.get(PARTICIPANTS_URL, {"room": "ghost"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    @mock.patch("waldur_mastermind.matrix_chat.views.livekit_client.list_participants")
    def test_track_resolution_serialized(self, mock_list_participants):
        mock_list_participants.return_value = [
            {
                "sid": "PA_1",
                "identity": "@alice:example.com",
                "state": "ACTIVE",
                "is_publisher": True,
                "joined_at": 1700000100,
                "tracks": [
                    {
                        "sid": "TR_1",
                        "name": "camera",
                        "type": "VIDEO",
                        "muted": False,
                        "width": 1280,
                        "height": 720,
                    }
                ],
            }
        ]
        self.client.force_authenticate(self.staff)
        response = self.client.get(PARTICIPANTS_URL, {"room": "room1"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        track = response.data[0]["tracks"][0]
        self.assertEqual(track["type"], "VIDEO")
        self.assertEqual(track["width"], 1280)
        self.assertEqual(track["height"], 720)
