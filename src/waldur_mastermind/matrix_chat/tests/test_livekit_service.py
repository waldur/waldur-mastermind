import logging
from unittest import mock

import httpx
import jwt
import respx
from constance.test import override_config
from django.core.cache import cache
from django.test import RequestFactory
from rest_framework import status, test
from rest_framework.throttling import ScopedRateThrottle

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import (
    handlers,
    livekit_client,
    matrix_client,
    models,
    views,
)
from waldur_mastermind.matrix_chat.matrix_client import MatrixClientError
from waldur_mastermind.matrix_chat.tests import fixtures

GET_TOKEN_URL = "/api/matrix/livekit/get_token"
SFU_GET_URL = "/api/matrix/livekit/sfu/get"
DELEGATE_URL = "/api/matrix/livekit/delegate_delayed_leave"
HOMESERVER = "http://tuwunel.internal:6167"
DOMAIN = "matrix.example.com"
ALICE = f"@alice:{DOMAIN}"
ROOM = f"!room:{DOMAIN}"
OPENID = {
    "access_token": "openid-secret",
    "token_type": "Bearer",
    "matrix_server_name": DOMAIN,
    "expires_in": 3600,
}

CONFIG = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL=HOMESERVER,
    MATRIX_HOMESERVER_DOMAIN=DOMAIN,
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
    MATRIX_LIVEKIT_KEY="devkey",
    MATRIX_LIVEKIT_SECRET="devsecret",
    MATRIX_LIVEKIT_PUBLIC_URL="wss://matrix.example.com/livekit",
)


def get_token_body(**overrides):
    body = {
        "room_id": ROOM,
        "slot_id": "m.call#ROOM",
        "openid_token": dict(OPENID),
        "member": {
            "id": f"{ALICE}:DEVICE1",
            "claimed_user_id": ALICE,
            "claimed_device_id": "DEVICE1",
        },
    }
    body.update(overrides)
    return body


def sfu_get_body(**overrides):
    body = {"room": ROOM, "openid_token": dict(OPENID), "device_id": "DEVICE1"}
    body.update(overrides)
    return body


@override_config(**CONFIG)
class LiveKitServiceTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.patches = {
            name: mock.patch.object(matrix_client, name, **kwargs).start()
            for name, kwargs in {
                "get_openid_user": {"return_value": ALICE},
                "is_joined": {"return_value": True},
                "list_devices": {
                    "return_value": [{"device_id": "OTHER"}, {"device_id": "DEVICE1"}]
                },
            }.items()
        }
        self.create_room = mock.patch.object(livekit_client, "create_call_room").start()
        self.addCleanup(mock.patch.stopall)

    def claims(self, response):
        return jwt.decode(response.data["jwt"], "devsecret", algorithms=["HS256"])

    def assert_refused(self, response, code=status.HTTP_403_FORBIDDEN):
        self.assertEqual(response.status_code, code)
        self.assertNotIn("jwt", response.data)
        self.create_room.assert_not_called()

    def test_get_token_issues_a_room_scoped_token(self):
        response = self.client.post(GET_TOKEN_URL, get_token_body(), format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["url"], "wss://matrix.example.com/livekit")
        claims = self.claims(response)
        room_name = livekit_client.call_room_name(ROOM, "m.call#ROOM")
        self.assertEqual(claims["video"]["room"], room_name)
        self.assertEqual(
            claims["sub"],
            livekit_client.call_identity(ALICE, "DEVICE1", f"{ALICE}:DEVICE1"),
        )
        self.assertEqual(
            claims["attributes"], {livekit_client.MATRIX_USER_ATTRIBUTE: ALICE}
        )
        self.assertNotIn("canUpdateOwnMetadata", claims["video"])
        self.assertFalse(claims["video"]["roomCreate"])
        self.create_room.assert_called_once_with(room_name)
        self.patches["get_openid_user"].assert_called_once_with("openid-secret")
        self.patches["is_joined"].assert_called_once_with(ALICE, ROOM)
        self.patches["list_devices"].assert_called_once_with(ALICE)

    def test_sfu_get_issues_a_legacy_token(self):
        response = self.client.post(SFU_GET_URL, sfu_get_body(), format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        claims = self.claims(response)
        # lk-jwt-service's legacy derivation, so Element Call's calls keep
        # their room whichever endpoint a client used.
        self.assertEqual(
            claims["video"]["room"],
            livekit_client.call_room_name(ROOM, "m.call#ROOM"),
        )
        self.assertEqual(claims["sub"], f"{ALICE}:DEVICE1")

    def test_the_web_chat_slot_is_accepted(self):
        response = self.client.post(
            GET_TOKEN_URL, get_token_body(slot_id="0"), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.claims(response)["video"]["room"], livekit_client.call_room_name(ROOM)
        )

    def test_unknown_slot_is_refused(self):
        response = self.client.post(
            GET_TOKEN_URL, get_token_body(slot_id="other"), format="json"
        )

        self.assert_refused(response, status.HTTP_400_BAD_REQUEST)

    def test_bad_openid_token_is_unauthorized(self):
        self.patches["get_openid_user"].return_value = None

        for url, body in (
            (GET_TOKEN_URL, get_token_body()),
            (SFU_GET_URL, sfu_get_body()),
        ):
            response = self.client.post(url, body, format="json")
            self.assert_refused(response, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(response.data["errcode"], "M_UNAUTHORIZED")

    def test_token_of_another_homeserver_is_refused_unasked(self):
        openid = dict(OPENID, matrix_server_name="evil.example.org")

        response = self.client.post(
            GET_TOKEN_URL, get_token_body(openid_token=openid), format="json"
        )

        self.assert_refused(response)
        self.patches["get_openid_user"].assert_not_called()

    def test_subject_on_another_domain_is_refused(self):
        self.patches["get_openid_user"].return_value = "@alice:evil.example.org"

        response = self.client.post(SFU_GET_URL, sfu_get_body(), format="json")

        self.assert_refused(response)

    def test_claimed_user_must_be_the_token_subject(self):
        self.patches["get_openid_user"].return_value = f"@bob:{DOMAIN}"

        response = self.client.post(GET_TOKEN_URL, get_token_body(), format="json")

        self.assert_refused(response, status.HTTP_401_UNAUTHORIZED)

    def test_non_member_is_refused(self):
        self.patches["is_joined"].return_value = False

        for url, body in (
            (GET_TOKEN_URL, get_token_body()),
            (SFU_GET_URL, sfu_get_body()),
        ):
            response = self.client.post(url, body, format="json")
            self.assert_refused(response)
            # The same answer as for any other refusal.
            self.assertEqual(response.data["errcode"], "M_FORBIDDEN")

    def test_foreign_device_is_refused(self):
        response = self.client.post(
            SFU_GET_URL, sfu_get_body(device_id="MADE-UP"), format="json"
        )

        self.assert_refused(response)

    def test_deactivated_waldur_user_is_refused(self):
        user = structure_factories.UserFactory(is_active=False)
        models.MatrixUserProfile.objects.create(user=user, matrix_user_id=ALICE)

        response = self.client.post(GET_TOKEN_URL, get_token_body(), format="json")

        self.assert_refused(response)

    def test_the_bot_is_refused(self):
        self.patches["get_openid_user"].return_value = matrix_client.get_bot_user_id()

        response = self.client.post(SFU_GET_URL, sfu_get_body(), format="json")

        self.assert_refused(response)

    def test_homeserver_failure_fails_closed(self):
        self.patches["is_joined"].side_effect = MatrixClientError("down")

        response = self.client.post(GET_TOKEN_URL, get_token_body(), format="json")

        self.assert_refused(response, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_delayed_leave_delegation_is_refused_as_bad_json(self):
        # Element Call retries without it on M_BAD_JSON.
        response = self.client.post(
            SFU_GET_URL,
            sfu_get_body(delay_id="d1", delay_timeout=5000),
            format="json",
        )

        self.assert_refused(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["errcode"], "M_BAD_JSON")

    def test_missing_member_is_bad_json(self):
        response = self.client.post(
            GET_TOKEN_URL, get_token_body(member={}), format="json"
        )

        self.assert_refused(response, status.HTTP_400_BAD_REQUEST)

    def test_waldur_authentication_is_not_used(self):
        # A signed-in Waldur user is no one to this API.
        self.client.force_authenticate(structure_factories.UserFactory())
        self.patches["get_openid_user"].return_value = None

        response = self.client.post(GET_TOKEN_URL, get_token_body(), format="json")

        self.assert_refused(response, status.HTTP_401_UNAUTHORIZED)

    @override_config(MATRIX_LIVEKIT_PUBLIC_URL="")
    def test_unconfigured_calls_are_unavailable(self):
        response = self.client.post(GET_TOKEN_URL, get_token_body(), format="json")

        self.assert_refused(response, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_responses_allow_any_origin_without_credentials(self):
        response = self.client.post(
            GET_TOKEN_URL,
            get_token_body(),
            format="json",
            HTTP_ORIGIN="https://element.example.org",
        )

        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertNotIn("Access-Control-Allow-Credentials", response)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_cors_preflight(self):
        for url in (GET_TOKEN_URL, SFU_GET_URL):
            response = self.client.options(
                url,
                HTTP_ORIGIN="https://element.example.org",
                HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response["Access-Control-Allow-Origin"], "*")
            self.assertIn("POST", response["Access-Control-Allow-Methods"])
            self.assertIn("Content-Type", response["Access-Control-Allow-Headers"])

    def test_other_api_preflights_stay_closed(self):
        response = self.client.options(
            "/api/matrix/call-token/",
            HTTP_ORIGIN="https://element.example.org",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        )

        self.assertNotIn("Access-Control-Allow-Origin", response)

    def test_django_cors_headers_is_told_to_allow_the_api(self):
        factory = RequestFactory()

        self.assertTrue(
            handlers.allow_public_cors(None, request=factory.post(GET_TOKEN_URL))
        )
        self.assertFalse(
            handlers.allow_public_cors(None, request=factory.post("/api/users/"))
        )

    def test_member_id_must_name_the_device(self):
        member = {
            "id": "RANDOM",
            "claimed_user_id": ALICE,
            "claimed_device_id": "DEVICE1",
        }

        response = self.client.post(
            GET_TOKEN_URL, get_token_body(member=member), format="json"
        )

        self.assert_refused(response, status.HTTP_400_BAD_REQUEST)
        self.patches["get_openid_user"].assert_not_called()

    def test_the_web_chat_member_id_is_its_device(self):
        member = {
            "id": "DEVICE1",
            "claimed_user_id": ALICE,
            "claimed_device_id": "DEVICE1",
        }

        response = self.client.post(
            GET_TOKEN_URL, get_token_body(member=member, slot_id="0"), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.claims(response)["sub"],
            livekit_client.call_identity(ALICE, "DEVICE1"),
        )

    def post_from(self, forwarded_for, remote_addr="10.0.0.1"):
        return self.client.post(
            SFU_GET_URL,
            sfu_get_body(),
            format="json",
            HTTP_X_FORWARDED_FOR=forwarded_for,
            REMOTE_ADDR=remote_addr,
        )

    def test_a_forged_forwarded_for_does_not_reset_the_client_bucket(self):
        self.patches["is_joined"].return_value = False
        with mock.patch.dict(
            ScopedRateThrottle.THROTTLE_RATES, {"matrix_livekit_token": "2/hour"}
        ):
            # The proxy appends the address it saw: the client's own part varies.
            codes = [
                self.post_from(f"198.51.100.{i}, 203.0.113.7").status_code
                for i in range(3)
            ]
            other_client = self.post_from("198.51.100.1, 203.0.113.8").status_code

        self.assertEqual(codes[-1], status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(other_client, status.HTTP_403_FORBIDDEN)

    def test_a_port_on_the_edge_hop_does_not_reset_the_client_bucket(self):
        self.patches["is_joined"].return_value = False
        for hops in (
            [f"1.2.3.4:{port}" for port in (5678, 5679, 5680)],
            [f"[::1]:{port}" for port in (5678, 5679, 5680)],
        ):
            cache.clear()
            with mock.patch.dict(
                ScopedRateThrottle.THROTTLE_RATES, {"matrix_livekit_token": "2/hour"}
            ):
                codes = [self.post_from(hop).status_code for hop in hops]

            self.assertEqual(codes[-1], status.HTTP_429_TOO_MANY_REQUESTS, hops)

    def test_the_edge_address_is_stripped_of_its_port(self):
        factory = RequestFactory()
        for forwarded, expected in (
            ("9.9.9.9, 1.2.3.4:5678", "1.2.3.4"),
            ("[::1]:5678", "::1"),
            ("2001:db8::1", "2001:db8::1"),
        ):
            request = factory.post("/", HTTP_X_FORWARDED_FOR=forwarded)
            self.assertEqual(views._edge_client_address(request), expected)

    def test_one_user_is_capped_across_addresses(self):
        with mock.patch.dict(
            ScopedRateThrottle.THROTTLE_RATES,
            {"matrix_livekit_token_user": "2/hour"},
        ):
            codes = [self.post_from(f"203.0.113.{i}").status_code for i in range(3)]

        self.assertEqual(codes[:2], [status.HTTP_200_OK] * 2)
        self.assertEqual(codes[-1], status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(self.create_room.call_count, 2)

    def test_is_throttled_per_client(self):
        self.patches["is_joined"].return_value = False
        with mock.patch.dict(
            ScopedRateThrottle.THROTTLE_RATES, {"matrix_livekit_token": "2/hour"}
        ):
            codes = [
                self.client.post(SFU_GET_URL, sfu_get_body(), format="json").status_code
                for _ in range(3)
            ]

        self.assertEqual(codes[-1], status.HTTP_429_TOO_MANY_REQUESTS)

    @override_config(MATRIX_ENABLED=False)
    def test_disabled_chat_is_not_found(self):
        response = self.client.post(GET_TOKEN_URL, get_token_body(), format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def assert_delegation_not_supported(self, response):
        # Element Call takes any status but 404 as support for delegation.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["errcode"], "M_NOT_FOUND")
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_element_calls_delegation_probe_is_told_it_is_not_supported(self):
        # Element Call's probe: a bare cross-origin POST, without a body.
        response = self.client.post(
            DELEGATE_URL, HTTP_ORIGIN="https://element.example.org"
        )

        self.assert_delegation_not_supported(response)

    def test_delayed_leave_delegation_is_not_supported(self):
        body = {
            "url": "wss://matrix.example.com/livekit",
            "room_id": ROOM,
            "slot_id": "m.call#ROOM",
            "openid_token": dict(OPENID),
            "member": get_token_body()["member"],
            "delay_id": "syd_delay123",
            "delay_timeout": 3_600_000,
        }

        response = self.client.post(DELEGATE_URL, body, format="json")

        self.assert_delegation_not_supported(response)
        for patch in self.patches.values():
            patch.assert_not_called()
        self.create_room.assert_not_called()

    def test_delegation_preflight_is_allowed(self):
        response = self.client.options(
            DELEGATE_URL,
            HTTP_ORIGIN="https://element.example.org",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")

    def test_delegation_probes_are_not_throttled(self):
        with mock.patch.dict(
            ScopedRateThrottle.THROTTLE_RATES, {"matrix_livekit_token": "2/hour"}
        ):
            for _ in range(3):
                self.assert_delegation_not_supported(self.client.post(DELEGATE_URL))
            # Nor do they use up the token budget of the same client.
            self.assertEqual(
                self.client.post(
                    SFU_GET_URL, sfu_get_body(), format="json"
                ).status_code,
                status.HTTP_200_OK,
            )

    @override_config(MATRIX_ENABLED=False)
    def test_delegation_is_not_supported_with_chat_disabled(self):
        self.assert_delegation_not_supported(self.client.post(DELEGATE_URL))


@override_config(**CONFIG)
class LiveKitWaldurRoomTest(test.APITestCase):
    """A room Waldur manages takes the user's Waldur access as well as their
    homeserver membership."""

    def setUp(self):
        cache.clear()
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room
        self.profile = self.fixture.matrix_user_profile
        self.fixture.matrix_room_member
        self.patches = {
            name: mock.patch.object(matrix_client, name, **kwargs).start()
            for name, kwargs in {
                "get_openid_user": {"return_value": self.profile.matrix_user_id},
                "is_joined": {"return_value": True},
                "list_devices": {"return_value": [{"device_id": "DEVICE1"}]},
            }.items()
        }
        self.create_room = mock.patch.object(livekit_client, "create_call_room").start()
        self.addCleanup(mock.patch.stopall)

    def post(self, room_id=None):
        return self.client.post(
            SFU_GET_URL,
            sfu_get_body(room=room_id or self.room.room_id),
            format="json",
        )

    def assert_forbidden(self, response):
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data,
            {"errcode": "M_FORBIDDEN", "error": "You may not join this call."},
        )
        self.create_room.assert_not_called()

    def test_member_with_a_role_gets_a_token(self):
        response = self.post()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.create_room.assert_called_once()

    def test_removed_project_member_is_refused(self):
        # Still joined on the homeserver: the kick has not landed yet.
        self.fixture.project.remove_user(self.fixture.admin)

        self.assert_forbidden(self.post())

    def test_archived_room_is_refused(self):
        models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=models.RoomStates.ARCHIVED
        )

        self.assert_forbidden(self.post())

    def test_matrix_user_unknown_to_waldur_is_refused_in_a_waldur_room(self):
        self.patches["get_openid_user"].return_value = f"@stranger:{DOMAIN}"

        self.assert_forbidden(self.post())

    def test_room_waldur_does_not_manage_takes_homeserver_membership(self):
        # Direct messages and rooms made in Element: no Waldur role applies.
        self.fixture.project.remove_user(self.fixture.admin)
        unmanaged = f"!dm:{DOMAIN}"

        response = self.post(room_id=unmanaged)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.patches["is_joined"].return_value = False
        self.create_room.reset_mock()
        self.assert_forbidden(self.post(room_id=unmanaged))

    def test_non_member_is_refused_alike_for_known_and_unknown_rooms(self):
        self.patches["is_joined"].return_value = False
        models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=models.RoomStates.ARCHIVED
        )

        known = self.post()
        unknown = self.post(room_id=f"!nosuchroom:{DOMAIN}")

        self.assert_forbidden(known)
        self.assert_forbidden(unknown)
        self.assertEqual(known.data, unknown.data)


@override_config(**CONFIG)
class GetOpenIdUserTest(test.APITestCase):
    url = f"{HOMESERVER}/_matrix/federation/v1/openid/userinfo"

    @respx.mock
    def test_returns_the_subject(self):
        route = respx.get(self.url).mock(
            return_value=httpx.Response(200, json={"sub": ALICE})
        )

        self.assertEqual(matrix_client.get_openid_user("tok"), ALICE)
        self.assertEqual(route.calls.last.request.url.params["access_token"], "tok")

    @respx.mock
    def test_unrecognised_token_is_none(self):
        respx.get(self.url).mock(
            return_value=httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"})
        )

        self.assertIsNone(matrix_client.get_openid_user("tok"))

    @respx.mock
    def test_other_failures_raise_without_the_token(self):
        respx.get(self.url).mock(side_effect=httpx.ConnectError("refused"))

        with self.assertRaises(MatrixClientError) as cm:
            matrix_client.get_openid_user("openid-secret")
        self.assertNotIn("openid-secret", str(cm.exception))

    def test_httpx_request_log_hides_the_token(self):
        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            "",
            0,
            'HTTP Request: %s %s "%s %d %s"',
            ("GET", f"{self.url}?access_token=openid-secret", "HTTP/1.1", 200, "OK"),
            None,
        )

        for log_filter in logging.getLogger("httpx").filters:
            log_filter.filter(record)

        self.assertNotIn("openid-secret", record.getMessage())
        self.assertIn("access_token=[Filtered]", record.getMessage())


@override_config(
    MATRIX_LIVEKIT_KEY="devkey",
    MATRIX_LIVEKIT_SECRET="devsecret",
    MATRIX_LIVEKIT_URL="http://livekit:7880",
)
class RemoveFromEveryCallSlotTest(test.APITestCase):
    @mock.patch("waldur_mastermind.matrix_chat.livekit_client._twirp_call")
    def test_covers_the_element_call_and_web_chat_slots(self, mock_twirp):
        mock_twirp.side_effect = lambda method, body, **kw: (
            {
                "participants": [
                    {
                        "identity": f"{ALICE}:D1",
                        "attributes": {livekit_client.MATRIX_USER_ATTRIBUTE: ALICE},
                    }
                ]
            }
            if method == "ListParticipants"
            else {}
        )

        removed = livekit_client.remove_from_call(ROOM, ALICE)

        self.assertEqual(removed, 2)
        listed = {
            c.args[1]["room"]
            for c in mock_twirp.call_args_list
            if c.args[0] == "ListParticipants"
        }
        self.assertEqual(
            listed,
            {
                livekit_client.call_room_name(ROOM, "m.call#ROOM"),
                livekit_client.call_room_name(ROOM, "0"),
            },
        )
