"""Thin synchronous client for the LiveKit Twirp admin API.

Matches the module-level helper style of ``matrix_client.py``: no classes, plain
``httpx.post`` (a Twirp call is a single round-trip). LiveKit is reached on the
internal ``livekit:7880`` endpoint with an HS256 admin JWT minted from the
``MATRIX_LIVEKIT_KEY`` / ``MATRIX_LIVEKIT_SECRET`` Constance settings.
"""

import base64
import hashlib
import json
import logging
import time

import httpx
import jwt
from constance import config

logger = logging.getLogger(__name__)

# Per-call budget. LiveKit lives next to mastermind on the internal network, so
# a slow response means it is wedged — fail fast rather than hold the staff
# request open.
LIVEKIT_TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)

# The admin token is minted per request; a short TTL keeps it from being useful
# if it ever leaks out of the backend.
LIVEKIT_TOKEN_TTL_SECONDS = 60

DEFAULT_INTERNAL_URL = "http://livekit:7880"


class LiveKitClientError(Exception):
    """Raised on transport failure or a non-success LiveKit response.

    ``status_code`` carries the upstream HTTP status when LiveKit answered
    (0 for a transport failure), so views can tell a rejected admin token
    (401/403) apart from an unreachable service.
    """

    def __init__(self, message, status_code=0):
        super().__init__(message)
        self.status_code = status_code


def _get_key():
    return config.MATRIX_LIVEKIT_KEY


def _get_secret():
    return config.MATRIX_LIVEKIT_SECRET


def is_configured() -> bool:
    """Both credentials must be present; views translate a False to 503."""
    return bool(_get_key() and _get_secret())


def get_internal_url() -> str:
    return config.MATRIX_LIVEKIT_URL or DEFAULT_INTERNAL_URL


def get_public_url() -> str:
    """The signaling URL handed to browsers joining a call."""
    return config.MATRIX_LIVEKIT_PUBLIC_URL


def _mint_admin_token(room: str = "", create: bool = False) -> str:
    """Mint a short-lived HS256 admin JWT with room-list / room-admin grants.

    ``roomAdmin`` is scoped per-room by LiveKit: room-specific calls such as
    ListParticipants are rejected (401 ``permissions denied``) unless the grant
    also names the target room. Pass ``room`` for those; ListRooms needs none.
    """
    now = int(time.time())
    video: dict = {
        "roomList": True,
        "roomAdmin": True,
    }
    if room:
        video["room"] = room
    if create:
        video["roomCreate"] = True
    payload = {
        "iss": _get_key(),
        "nbf": now,
        "exp": now + LIVEKIT_TOKEN_TTL_SECONDS,
        "video": video,
    }
    return jwt.encode(payload, _get_secret(), algorithm="HS256")


def _twirp_call(method: str, body: dict, room: str = "", create=False) -> dict:
    url = f"{get_internal_url()}/twirp/livekit.RoomService/{method}"
    token = _mint_admin_token(room, create=create)
    try:
        response = httpx.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=LIVEKIT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise LiveKitClientError(f"LiveKit request failed: {exc}") from exc

    if response.status_code != 200:
        # Twirp errors are JSON ({"code", "msg"}); the HTTP auth layer rejects a
        # bad token with a plain-text body. Surface the status either way.
        raise LiveKitClientError(
            f"LiveKit returned HTTP {response.status_code}: {response.text[:200]}",
            status_code=response.status_code,
        )

    try:
        return response.json()
    except ValueError as exc:
        # A 200 with a non-JSON body (e.g. a health/proxy page answering on the
        # signalling port) would otherwise escape as an unhandled 500.
        raise LiveKitClientError(
            f"LiveKit returned a non-JSON 200 body: {exc}",
            status_code=response.status_code,
        ) from exc


def _as_int(value) -> int:
    # livekit-server serialises int64 fields (creation_time, joined_at) as strings.
    if value in (None, ""):
        return 0
    return int(value)


# livekit-server returns snake_case JSON (verified against
# livekit/livekit-server) — not the camelCase that protojson emits elsewhere.
def _normalize_track(track: dict) -> dict:
    return {
        "sid": track.get("sid", ""),
        "name": track.get("name", ""),
        "type": track.get("type", ""),
        "muted": bool(track.get("muted", False)),
        "width": _as_int(track.get("width")),
        "height": _as_int(track.get("height")),
    }


def _normalize_participant(participant: dict) -> dict:
    return {
        "sid": participant.get("sid", ""),
        "identity": participant.get("identity", ""),
        "state": participant.get("state", ""),
        "is_publisher": bool(participant.get("is_publisher", False)),
        "joined_at": _as_int(participant.get("joined_at")),
        "tracks": [
            _normalize_track(track) for track in participant.get("tracks") or []
        ],
    }


def _normalize_room(room: dict) -> dict:
    return {
        "sid": room.get("sid", ""),
        "name": room.get("name", ""),
        "num_participants": _as_int(room.get("num_participants")),
        "num_publishers": _as_int(room.get("num_publishers")),
        "creation_time": _as_int(room.get("creation_time")),
        "max_participants": _as_int(room.get("max_participants")),
        "metadata": room.get("metadata", ""),
    }


def list_rooms() -> list[dict]:
    data = _twirp_call("ListRooms", {})
    return [_normalize_room(room) for room in data.get("rooms") or []]


def list_participants(room_name: str) -> list[dict]:
    data = _twirp_call("ListParticipants", {"room": room_name}, room=room_name)
    return [
        _normalize_participant(participant)
        for participant in data.get("participants") or []
    ]


# A call's LiveKit room and identities are derived the way lk-jwt-service derives
# them, so a call keeps the same room whichever service issued the token, and the
# web chat can map identities back to Matrix users.
CALL_SLOT_ID = "0"
# Only long enough to connect: the web chat asks for the token right before it
# connects, and ICE/TURN negotiation takes seconds, so 3 minutes leaves ample
# margin for a slow join. A connected participant does not need it after that:
# livekit-server (pkg/service/roommanager.go, refreshToken) sends a fresh
# token on join and every 5 minutes, valid for max(10 minutes, time left on the
# original), carrying over the grants and attributes. So a participant removed
# from a call can reconnect with the token it last received for at most that
# long; a token issued here and never used lapses after 3 minutes.
CALL_TOKEN_TTL_SECONDS = 3 * 60
# Set by Waldur in the token, and not changeable by the participant: their
# token grants no metadata updates. Names whose participants to remove when the
# user loses the room.
MATRIX_USER_ATTRIBUTE = "waldur.matrix_user_id"
# Matching lk-jwt-service: an empty room waits 5 minutes for its first
# participant, and closes 20 seconds after the last one leaves.
CALL_ROOM_EMPTY_TIMEOUT_SECONDS = 5 * 60
CALL_ROOM_DEPARTURE_TIMEOUT_SECONDS = 20


def _hash_strings(*values: str) -> str:
    raw = json.dumps(list(values), separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(raw.encode()).digest()
    return base64.b64encode(digest).decode().rstrip("=")


def call_room_name(matrix_room_id: str, slot_id: str = CALL_SLOT_ID) -> str:
    """The LiveKit room of a Matrix room's call."""
    return _hash_strings(matrix_room_id, slot_id)


def call_identity(matrix_user_id: str, device_id: str) -> str:
    """The LiveKit identity of a user's device in a call."""
    return _hash_strings(matrix_user_id, device_id, device_id)


def create_call_room(room_name: str) -> None:
    """Create the call's LiveKit room; an existing room is left as it is."""
    _twirp_call(
        "CreateRoom",
        {
            "name": room_name,
            "empty_timeout": CALL_ROOM_EMPTY_TIMEOUT_SECONDS,
            "departure_timeout": CALL_ROOM_DEPARTURE_TIMEOUT_SECONDS,
        },
        create=True,
    )


def mint_call_token(room_name: str, identity: str, matrix_user_id: str) -> str:
    """A join token for one LiveKit room, and no other."""
    now = int(time.time())
    payload = {
        "iss": _get_key(),
        "sub": identity,
        "nbf": now,
        "exp": now + CALL_TOKEN_TTL_SECONDS,
        "attributes": {MATRIX_USER_ATTRIBUTE: matrix_user_id},
        # No canUpdateOwnMetadata: the web chat sets no name, metadata or
        # attributes of its own, and the attribute above must stay Waldur's.
        "video": {
            "room": room_name,
            "roomJoin": True,
            "roomCreate": False,
            "canPublish": True,
            "canSubscribe": True,
        },
    }
    return jwt.encode(payload, _get_secret(), algorithm="HS256")


def remove_from_call(matrix_room_id: str, matrix_user_id: str) -> int:
    """Disconnect the user's participants from the room's call.

    LiveKit checks a token only on connect, so someone who loses the room stays
    in a call they are in until removed. Returns how many were removed.
    """
    room_name = call_room_name(matrix_room_id)
    removed = 0
    for participant in (
        _twirp_call("ListParticipants", {"room": room_name}, room=room_name).get(
            "participants"
        )
        or []
    ):
        attributes = participant.get("attributes") or {}
        if attributes.get(MATRIX_USER_ATTRIBUTE) != matrix_user_id:
            continue
        _twirp_call(
            "RemoveParticipant",
            {"room": room_name, "identity": participant.get("identity", "")},
            room=room_name,
        )
        removed += 1
    return removed


def end_call(matrix_room_id: str) -> None:
    """Close the room's call, disconnecting everyone in it."""
    room_name = call_room_name(matrix_room_id)
    try:
        _twirp_call("DeleteRoom", {"room": room_name}, room=room_name, create=True)
    except LiveKitClientError as exc:
        # No call is going on.
        if exc.status_code != 404:
            raise
