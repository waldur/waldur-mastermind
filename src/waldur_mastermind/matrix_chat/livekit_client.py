"""Thin synchronous client for the LiveKit Twirp admin API.

Matches the module-level helper style of ``matrix_client.py``: no classes, plain
``httpx.post`` (a Twirp call is a single round-trip). LiveKit is reached on the
internal ``livekit:7880`` endpoint with an HS256 admin JWT minted from the
``MATRIX_LIVEKIT_KEY`` / ``MATRIX_LIVEKIT_SECRET`` Constance settings.
"""

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


def _mint_admin_token(room: str = "") -> str:
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
    payload = {
        "iss": _get_key(),
        "nbf": now,
        "exp": now + LIVEKIT_TOKEN_TTL_SECONDS,
        "video": video,
    }
    return jwt.encode(payload, _get_secret(), algorithm="HS256")


def _twirp_call(method: str, body: dict, room: str = "") -> dict:
    url = f"{get_internal_url()}/twirp/livekit.RoomService/{method}"
    token = _mint_admin_token(room)
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
