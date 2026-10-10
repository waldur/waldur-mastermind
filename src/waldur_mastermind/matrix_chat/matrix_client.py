from __future__ import annotations

import asyncio
import logging
import re
import secrets
import string
from collections import Counter
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx
from constance import config
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from markdown_it import MarkdownIt

from waldur_core.core.clean_html import clean_html
from waldur_core.core.models import User
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.structure.models import Project

from .models import MatrixUserProfile

# matrix-nio is imported lazily via _load_nio() rather than at module top —
# see the "Lazy imports for heavy optional backends" section of CLAUDE.md.
# nio drags in pycryptodome -> cffi -> pycparser (~10 MB) at import, and Matrix
# chat is an optional, Constance-gated feature (MATRIX_ENABLED), so that cost
# must not be paid at Django startup. Every function that references a nio
# symbol calls _load_nio() first, which populates these names into globals().
# The TYPE_CHECKING import below binds the names for the linter/type-checker only;
# it does not execute at runtime, so nio stays unimported until first use.
if TYPE_CHECKING:
    from nio import (
        AsyncClient,
        CallAnswerEvent,
        CallHangupEvent,
        CallInviteEvent,
        DownloadError,
        InviteMemberEvent,
        MemoryDownloadResponse,
        PowerLevelsEvent,
        ReactionEvent,
        RedactedEvent,
        RedactionEvent,
        RoomCreateError,
        RoomCreateResponse,
        RoomGetEventError,
        RoomInviteError,
        RoomInviteResponse,
        RoomKickError,
        RoomKickResponse,
        RoomMemberEvent,
        RoomMessageAudio,
        RoomMessageFile,
        RoomMessageImage,
        RoomMessagesError,
        RoomMessagesResponse,
        RoomMessageVideo,
        RoomNameEvent,
        RoomPutStateError,
        RoomPutStateResponse,
        RoomTopicEvent,
        RoomVisibility,
        StickerEvent,
    )

_NIO_NAMES = (
    "AsyncClient",
    "CallAnswerEvent",
    "CallHangupEvent",
    "CallInviteEvent",
    "DownloadError",
    "InviteMemberEvent",
    "MemoryDownloadResponse",
    "PowerLevelsEvent",
    "ReactionEvent",
    "RedactedEvent",
    "RedactionEvent",
    "RoomCreateError",
    "RoomCreateResponse",
    "RoomGetEventError",
    "RoomInviteError",
    "RoomInviteResponse",
    "RoomKickError",
    "RoomKickResponse",
    "RoomMemberEvent",
    "RoomMessageAudio",
    "RoomMessageFile",
    "RoomMessageImage",
    "RoomMessagesError",
    "RoomMessagesResponse",
    "RoomMessageVideo",
    "RoomNameEvent",
    "RoomPutStateError",
    "RoomPutStateResponse",
    "RoomSendError",
    "RoomSendResponse",
    "RoomTopicEvent",
    "RoomVisibility",
    "StickerEvent",
)


def _load_nio():
    """Import matrix-nio symbols into module globals on first use (idempotent).

    nio drags in pycryptodome -> cffi -> pycparser (~10 MB) at import; deferring
    it keeps that out of startup memory for the optional Matrix feature.
    """
    if "AsyncClient" in globals():
        return
    import nio

    g = globals()
    for _name in _NIO_NAMES:
        g[_name] = getattr(nio, _name)


logger = logging.getLogger(__name__)


class _RedactAccessToken(logging.Filter):
    """httpx logs every request URL at INFO, and the OpenID userinfo call puts
    a user's token in its query string."""

    _token = re.compile(r"(access_token=)[^&\s\"']+")

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._token.sub(r"\1[Filtered]", str(arg))
                if "access_token=" in str(arg)
                else arg
                for arg in record.args
            )
        return True


logging.getLogger("httpx").addFilter(_RedactAccessToken())


class MatrixClientError(Exception):
    pass


class MatrixUserLocked(MatrixClientError):
    """The homeserver refused to act for a user whose account is locked."""


class MatrixUserNotFound(MatrixClientError):
    """The homeserver's admin API has no account with this ID."""


class MatrixAccountIsHomeserverAdmin(MatrixClientError):
    """The account is a homeserver admin's, which Waldur leaves alone."""


class MatrixAdminRequired(MatrixClientError):
    """The homeserver's admin API cannot be used until an operator acts: the
    bot is not a server admin, or the API is not served at MATRIX_HOMESERVER_URL."""


def get_bot_display_name():
    # Derived from SITE_NAME so whitelabel deployments don't surface "Waldur"
    # in Matrix clients.
    return f"{config.SITE_NAME} Bot"


def is_homeserver_configured():
    """Whether Waldur can act on the homeserver, whether or not chat is on."""
    return bool(config.MATRIX_HOMESERVER_URL and config.MATRIX_APPSERVICE_AS_TOKEN)


def is_enabled():
    return bool(config.MATRIX_ENABLED) and is_homeserver_configured()


def get_bot_user_id():
    """Return the bot's full Matrix user ID."""
    localpart = config.MATRIX_APPSERVICE_SENDER_LOCALPART or "waldur-bot"
    return f"@{localpart}:{config.MATRIX_HOMESERVER_DOMAIN}"


def _get_as_token():
    """Return the appservice token Waldur authenticates to the homeserver with."""
    return config.MATRIX_APPSERVICE_AS_TOKEN


def _get_client_params():
    """Read config values synchronously (safe from sync context) and return them."""
    return config.MATRIX_HOMESERVER_URL, get_bot_user_id(), _get_as_token()


def get_public_homeserver_url():
    """URL browser clients should use to reach the homeserver.

    Falls back to MATRIX_HOMESERVER_URL when the public override is unset —
    preserves behavior for deployments where the same URL works from both
    the backend (server-to-server HTTP) and the browser (server-to-client).
    Set MATRIX_HOMESERVER_PUBLIC_URL when the two differ, e.g. a
    Docker-internal name (`http://tuwunel.internal:6167`) on the backend vs
    a Caddy-proxied public URL (`https://waldur.example.com`) in the browser.
    """
    return config.MATRIX_HOMESERVER_PUBLIC_URL or config.MATRIX_HOMESERVER_URL


def ensure_bot_user_exists():
    """Register the appservice bot user on the homeserver if it doesn't exist."""
    homeserver_url = config.MATRIX_HOMESERVER_URL
    as_token = _get_as_token()
    localpart = config.MATRIX_APPSERVICE_SENDER_LOCALPART or "waldur-bot"
    bot_user_id = get_bot_user_id()

    if not homeserver_url or not as_token:
        raise MatrixClientError(
            "MATRIX_HOMESERVER_URL and MATRIX_APPSERVICE_AS_TOKEN must be configured"
        )

    result = _run_async(_register_bot_user_async(homeserver_url, as_token, localpart))
    logger.info("Bot user %s registered on %s", bot_user_id, homeserver_url)
    # Set the bot's display name unconditionally so existing deployments pick
    # it up too — the localpart alone reads as a raw "waldur-bot" handle.
    set_display_name(bot_user_id, get_bot_display_name())
    return result


async def _register_bot_user_async(homeserver_url, as_token, localpart):
    """Register the bot user via the appservice registration API."""
    url = f"{homeserver_url}/_matrix/client/v3/register"

    # Tight timeout so a misconfigured homeserver URL (or a CI/firewall
    # path that silently drops packets) can't hang the setup endpoint
    # for the kernel's full SYN-retry budget. The view treats any
    # exception as "bot autoprovision deferred", so a fast failure is
    # always better than a slow one here.
    timeout = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        response = await http_client.post(
            url,
            json={
                "auth": {"type": "m.login.application_service"},
                "username": localpart,
                # Waldur acts as the bot with the appservice token, never a
                # session of its own.
                "inhibit_login": True,
            },
            headers={"Authorization": f"Bearer {as_token}"},
        )
        if response.status_code == 200:
            return response.json()
        data = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        errcode = data.get("errcode", "")
        if errcode in ("M_USER_IN_USE", "M_EXCLUSIVE"):
            return None  # Already exists or reserved by this appservice
        raise MatrixClientError(
            f"Failed to register bot user {localpart}: "
            f"{errcode} {data.get('error', response.text)}"
        )


def _make_client(homeserver_url, bot_user_id, access_token=None):
    """Create an AsyncClient from pre-read config values."""
    _load_nio()
    client = AsyncClient(homeserver_url, bot_user_id)
    if access_token:
        client.access_token = access_token
    return client


def _run_async(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Every Waldur room is encrypted from its creation: messages reach the homeserver
# only as Megolm ciphertext. A room's encryption cannot be turned off again.
ENCRYPTION_STATE = {
    "type": "m.room.encryption",
    "state_key": "",
    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
}

# Only Waldur (the room creator, at 100) changes room state, the encryption
# setting included; members may post and join calls.
ROOM_POWER_LEVELS = {
    "invite": 100,
    "kick": 100,
    "ban": 100,
    "redact": 100,
    "events_default": 0,
    "state_default": 100,
    "events": {
        "m.room.message": 0,
        "m.room.name": 100,
        "m.room.topic": 100,
        "m.room.avatar": 100,
        "m.room.power_levels": 100,
        "m.room.join_rules": 100,
        "m.room.history_visibility": 100,
        "m.room.canonical_alias": 100,
        "m.room.encryption": 100,
        "org.matrix.msc3401.call.member": 0,
    },
}


def _room_initial_state(is_private):
    return [
        {
            "type": "m.room.join_rules",
            "content": {"join_rule": "invite" if is_private else "public"},
        },
        {
            "type": "m.room.history_visibility",
            "content": {"history_visibility": "shared"},
        },
        ENCRYPTION_STATE,
    ]


async def _create_room_async(
    homeserver_url,
    bot_user_id,
    access_token,
    name,
    alias_localpart=None,
    is_private=True,
):
    _load_nio()
    client = _make_client(homeserver_url, bot_user_id, access_token)
    try:
        response = await client.room_create(
            name=name,
            alias=alias_localpart,
            visibility=RoomVisibility.private if is_private else RoomVisibility.public,
            # Project rooms are for this Waldur's users only; no other server
            # can ever join, whatever the homeserver's federation settings.
            federate=False,
            invite=[],
            initial_state=_room_initial_state(is_private),
            power_level_override=ROOM_POWER_LEVELS,
        )
        if isinstance(response, RoomCreateError):
            # If alias is taken or not in appservice namespace, retry without alias
            error_msg = response.message or ""
            if alias_localpart and (
                "M_ROOM_IN_USE" in error_msg or "M_EXCLUSIVE" in error_msg
            ):
                logger.warning(
                    "Room alias %s already in use, creating without alias",
                    alias_localpart,
                )
                response = await client.room_create(
                    name=name,
                    visibility=RoomVisibility.private
                    if is_private
                    else RoomVisibility.public,
                    federate=False,
                    invite=[],
                    initial_state=_room_initial_state(is_private),
                    power_level_override=ROOM_POWER_LEVELS,
                )
                if isinstance(response, RoomCreateError):
                    raise MatrixClientError(
                        f"Failed to create room: {response.message}"
                    )
                if isinstance(response, RoomCreateResponse):
                    return response.room_id, False
                raise MatrixClientError(f"Unexpected response: {response}")
            else:
                raise MatrixClientError(f"Failed to create room: {response.message}")
        if isinstance(response, RoomCreateResponse):
            return response.room_id, bool(alias_localpart)
        raise MatrixClientError(f"Unexpected response: {response}")
    finally:
        await client.close()


def create_room(name, alias_localpart=None, is_private=True):
    """Create a Matrix room. Returns (room_id, alias_was_set)."""
    homeserver_url, bot_user_id, access_token = _get_client_params()
    return _run_async(
        _create_room_async(
            homeserver_url, bot_user_id, access_token, name, alias_localpart, is_private
        )
    )


async def _invite_user_async(
    homeserver_url, bot_user_id, access_token, room_id, user_id
):
    _load_nio()
    client = _make_client(homeserver_url, bot_user_id, access_token)
    try:
        response = await client.room_invite(room_id, user_id)
        if isinstance(response, RoomInviteError):
            msg = (response.message or "").lower()
            if (
                "already joined" in msg
                or "is joined" in msg
                or "cannot invite user that is joined" in msg
                or "already invited" in msg
            ):
                return True
            raise MatrixClientError(
                f"Failed to invite {user_id} to {room_id}: {response.message}"
            )
        if isinstance(response, RoomInviteResponse):
            return True
        raise MatrixClientError(f"Unexpected response: {response}")
    finally:
        await client.close()


def invite_user(room_id, user_id):
    """Invite a user to a Matrix room."""
    homeserver_url, bot_user_id, access_token = _get_client_params()
    return _run_async(
        _invite_user_async(homeserver_url, bot_user_id, access_token, room_id, user_id)
    )


async def _join_room_as_user_async(homeserver_url, as_token, room_id, matrix_user_id):
    url = f"{homeserver_url}/_matrix/client/v3/join/{room_id}"
    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            url,
            json={},
            headers={"Authorization": f"Bearer {as_token}"},
            params={"user_id": matrix_user_id},
        )
        if response.status_code == 200:
            return True
        data = _parse_json_response(response)
        errcode = data.get("errcode", "")
        if errcode in ("M_FORBIDDEN", "M_BAD_STATE") and (
            "already joined" in data.get("error", "").lower()
            or "already in the room" in data.get("error", "").lower()
        ):
            return True
        raise MatrixClientError(
            f"Failed to join room {room_id}: "
            f"{errcode} {data.get('error', response.text)}"
        )


def join_room_as_user(room_id, matrix_user_id):
    """Join a Matrix room as the user, accepting their pending invite.

    Acts through the appservice token with ?user_id= rather than logging in as
    the user, so the join creates no Matrix device or access token.
    """
    return _run_async(
        _join_room_as_user_async(
            config.MATRIX_HOMESERVER_URL, _get_as_token(), room_id, matrix_user_id
        )
    )


async def _leave_room_as_user_async(homeserver_url, as_token, room_id, matrix_user_id):
    url = f"{homeserver_url}/_matrix/client/v3/rooms/{room_id}/leave"
    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            url,
            json={},
            headers={"Authorization": f"Bearer {as_token}"},
            params={"user_id": matrix_user_id},
        )
        if response.status_code == 200:
            return True
        data = _parse_json_response(response)
        errcode = data.get("errcode", "")
        # Already out of the room — treat as success so leave is idempotent.
        if errcode in ("M_FORBIDDEN", "M_BAD_STATE") and (
            "not in" in data.get("error", "").lower()
            or "not a member" in data.get("error", "").lower()
        ):
            return True
        raise MatrixClientError(
            f"Failed to leave room {room_id}: "
            f"{errcode} {data.get('error', response.text)}"
        )


def leave_room_as_user(room_id, matrix_user_id):
    """Leave a Matrix room as the user, through the appservice like join_room_as_user."""
    # A stored member ID can map onto the bot, and the bot leaving would cost
    # Waldur control of the room.
    if matrix_user_id == get_bot_user_id():
        raise MatrixClientError(f"Refusing to take the bot out of {room_id}")
    return _run_async(
        _leave_room_as_user_async(
            config.MATRIX_HOMESERVER_URL, _get_as_token(), room_id, matrix_user_id
        )
    )


async def _kick_user_async(
    homeserver_url, bot_user_id, access_token, room_id, user_id, reason=""
):
    _load_nio()
    client = _make_client(homeserver_url, bot_user_id, access_token)
    try:
        response = await client.room_kick(room_id, user_id, reason=reason)
        if isinstance(response, RoomKickError):
            raise MatrixClientError(
                f"Failed to kick {user_id} from {room_id}: {response.message}"
            )
        if isinstance(response, RoomKickResponse):
            return True
        raise MatrixClientError(f"Unexpected response: {response}")
    finally:
        await client.close()


def kick_user(room_id, user_id, reason=""):
    """Kick a user from a Matrix room."""
    # As in leave_room_as_user: a stored member ID can map onto the bot. Skipped
    # rather than raised, so revocation retries don't spin on it.
    if user_id == get_bot_user_id():
        logger.warning("Not kicking the bot %s out of %s", user_id, room_id)
        return False
    homeserver_url, bot_user_id, access_token = _get_client_params()
    return _run_async(
        _kick_user_async(
            homeserver_url, bot_user_id, access_token, room_id, user_id, reason
        )
    )


async def _set_power_level_async(
    homeserver_url, bot_user_id, access_token, room_id, user_id, power_level
):
    _load_nio()
    client = _make_client(homeserver_url, bot_user_id, access_token)
    try:
        # Get current power levels
        response = await client.room_get_state_event(room_id, "m.room.power_levels", "")
        if isinstance(response, RoomGetEventError):
            raise MatrixClientError(
                f"Failed to get power levels for {room_id}: {response.message}"
            )

        content = response.content
        users = content.get("users", {})
        default_level = content.get("users_default", 0)
        # Every write is a new state event in the room, and member sync sets
        # the level of every member each time it runs.
        if users.get(user_id, default_level) == power_level:
            return True
        if power_level == default_level:
            del users[user_id]
        else:
            users[user_id] = power_level
        content["users"] = users

        put_response = await client.room_put_state(
            room_id, "m.room.power_levels", content
        )
        if isinstance(put_response, RoomPutStateError):
            raise MatrixClientError(
                f"Failed to set power level for {user_id} in {room_id}: {put_response.message}"
            )
        if isinstance(put_response, RoomPutStateResponse):
            return True
        raise MatrixClientError(f"Unexpected response: {put_response}")
    finally:
        await client.close()


def set_power_level(room_id, user_id, power_level):
    """Set a user's power level in a Matrix room, leaving a level that is already right alone."""
    homeserver_url, bot_user_id, access_token = _get_client_params()
    return _run_async(
        _set_power_level_async(
            homeserver_url, bot_user_id, access_token, room_id, user_id, power_level
        )
    )


# Bot message bodies are markdown. `html=False` keeps raw HTML out of the
# rendered output, so user-controlled data (project/display names, echoed
# commands) interpolated into a message can't inject markup. `breaks=True`
# turns single newlines into <br> so the formatted output keeps the line
# layout of the plaintext fallback.
_MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True})


def render_markdown(body: str) -> str:
    """Render a bot message body (markdown) to the sanitized HTML subset Matrix
    clients expect in `formatted_body`.

    `html=False` already blocks raw-HTML and dangerous-scheme link injection;
    the clean_html pass is defense-in-depth that confines the output to the
    platform-wide nh3 allowlist (a safe subset of Matrix's permitted tags), so
    anything markdown-it emits outside it — e.g. <img> — is dropped."""
    return clean_html(_MARKDOWN.render(body).strip())


def build_text_content(body, msgtype="m.text", reply_to=None):
    """Build an `m.room.message` content dict.

    `body` stays the plaintext (markdown) fallback for clients without HTML
    support; `formatted_body` carries the rendered HTML so Element and other
    rich clients show formatting instead of raw markdown.
    """
    content = {"msgtype": msgtype, "body": body}
    html = render_markdown(body)
    if html:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = html
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return content


def _build_media_message(event):
    """Build a message dict for media events (image, video, audio, file)."""
    msg = {
        "event_id": event.event_id,
        "sender": event.sender,
        "timestamp": event.server_timestamp,
        "type": event.source.get("type", ""),
        "msgtype": event.source.get("content", {}).get("msgtype", ""),
        "body": event.body,
        "has_media": True,
        "media_url": event.url,
        "media_info": event.source.get("content", {}).get("info", {}),
    }
    return msg


def _build_event_message(event):
    """Build a message dict for a generic Matrix event, dispatching by type."""
    _load_nio()
    # Media messages
    if isinstance(
        event, RoomMessageImage | RoomMessageVideo | RoomMessageAudio | RoomMessageFile
    ):
        return _build_media_message(event)

    # Sticker events
    if isinstance(event, StickerEvent):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "type": "m.sticker",
            "body": event.body,
            "has_media": True,
            "media_url": event.url,
            "media_info": event.source.get("content", {}).get("info", {}),
        }

    # Reaction events
    if isinstance(event, ReactionEvent):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "type": "m.reaction",
            "key": event.key,
            "relates_to": event.reacts_to,
        }

    # Call events
    if isinstance(event, CallInviteEvent | CallAnswerEvent | CallHangupEvent):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "type": event.source.get("type", ""),
            "call_id": event.call_id,
        }

    # Redacted events
    if isinstance(event, RedactedEvent):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "type": "m.room.redacted",
            "redacter": event.redacter,
            "reason": event.reason or "",
        }

    # Redaction events
    if isinstance(event, RedactionEvent):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "type": "m.room.redaction",
            "redacts": event.redacts,
            "reason": event.reason or "",
        }

    # Room state: name
    if isinstance(event, RoomNameEvent):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "type": "m.room.name",
            "name": event.name,
        }

    # Room state: topic
    if isinstance(event, RoomTopicEvent):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "type": "m.room.topic",
            "topic": event.topic,
        }

    # Room state: member
    if isinstance(event, RoomMemberEvent | InviteMemberEvent):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "type": "m.room.member",
            "membership": event.membership if hasattr(event, "membership") else "",
            "state_key": event.state_key if hasattr(event, "state_key") else "",
        }

    # Room state: power levels
    if isinstance(event, PowerLevelsEvent):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "type": "m.room.power_levels",
        }

    # Generic fallback: events with body
    if hasattr(event, "body"):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "body": event.body,
            "timestamp": event.server_timestamp,
            "type": event.source.get("type", ""),
            "msgtype": event.source.get("content", {}).get("msgtype", ""),
        }

    # All other events: capture basic metadata
    return {
        "event_id": event.event_id,
        "sender": event.sender,
        "timestamp": event.server_timestamp,
        "type": event.source.get("type", ""),
    }


async def _get_room_messages_async(
    homeserver_url, bot_user_id, access_token, room_id, limit=100, from_token=None
):
    _load_nio()
    client = _make_client(homeserver_url, bot_user_id, access_token)
    try:
        response = await client.room_messages(
            room_id,
            start=from_token or "",
            limit=limit,
            direction="b",  # backwards from most recent
        )
        if isinstance(response, RoomMessagesError):
            raise MatrixClientError(
                f"Failed to get messages for {room_id}: {response.message}"
            )
        if isinstance(response, RoomMessagesResponse):
            messages = [_build_event_message(event) for event in response.chunk]
            return {
                "messages": messages,
                "end_token": response.end,
            }
        raise MatrixClientError(f"Unexpected response: {response}")
    finally:
        await client.close()


def get_room_messages(room_id, limit=100, from_token=None):
    """Get messages from a Matrix room. Returns dict with messages and end_token."""
    homeserver_url, bot_user_id, access_token = _get_client_params()
    return _run_async(
        _get_room_messages_async(
            homeserver_url, bot_user_id, access_token, room_id, limit, from_token
        )
    )


async def _download_media_async(homeserver_url, bot_user_id, access_token, mxc_uri):
    _load_nio()
    client = _make_client(homeserver_url, bot_user_id, access_token)
    try:
        response = await client.download(mxc=mxc_uri)
        if isinstance(response, DownloadError):
            raise MatrixClientError(
                f"Failed to download media {mxc_uri}: {response.message}"
            )
        if isinstance(response, MemoryDownloadResponse):
            return (response.body, response.content_type, response.filename)
        raise MatrixClientError(f"Unexpected download response: {response}")
    finally:
        await client.close()


def download_media(mxc_uri):
    """Download media from a Matrix mxc:// URI. Returns (content_bytes, content_type, filename)."""
    homeserver_url, bot_user_id, access_token = _get_client_params()
    return _run_async(
        _download_media_async(homeserver_url, bot_user_id, access_token, mxc_uri)
    )


async def _register_user_async(
    homeserver_url, bot_user_id, username, password, as_token, registration_secret=""
):
    """Register a Matrix user via the standard CS API.

    Tries multiple registration strategies in order:
    1. m.login.registration_token
    2. m.login.application_service — appservice namespace registration.
    3. m.login.dummy — open registration fallback.

    Returns the registration response dict on success, or an empty dict if the
    user already exists.
    """
    url = f"{homeserver_url}/_matrix/client/v3/register"

    async with httpx.AsyncClient() as http_client:
        # Strategy 1: registration_token flow (two-step UIA).
        if registration_secret:
            result = await _try_registration_token_flow(
                http_client, url, username, password, registration_secret
            )
            if result is not None:
                return result  # Success or M_USER_IN_USE (empty dict)

        # Strategy 2: appservice registration
        result = await _try_appservice_registration(
            http_client, url, username, password, as_token
        )
        if result is not None:
            return result

        # Strategy 3: m.login.dummy fallback (open registration)
        result = await _try_dummy_registration(http_client, url, username, password)
        if result is not None:
            return result

        raise MatrixClientError(
            f"Failed to register user {username}: all strategies exhausted"
        )


async def _try_registration_token_flow(http_client, url, username, password, token):
    """Try m.login.registration_token UIA flow. Returns response dict, empty dict
    for M_USER_IN_USE, or None if this flow is not available."""
    response = await http_client.post(
        url,
        json={"username": username, "password": password, "inhibit_login": True},
    )
    _raise_for_server_error(response)
    data = _parse_json_response(response)
    if data.get("errcode") == "M_USER_IN_USE":
        return {}
    session = data.get("session")
    flows = data.get("flows", [])
    has_reg_token = any(
        "m.login.registration_token" in flow.get("stages", []) for flow in flows
    )
    if not (session and has_reg_token):
        return None  # This flow is not available
    response = await http_client.post(
        url,
        json={
            "auth": {
                "type": "m.login.registration_token",
                "token": token,
                "session": session,
            },
            "username": username,
            "password": password,
            "inhibit_login": True,
        },
    )
    _raise_for_server_error(response)
    if response.status_code == 200:
        return response.json()
    data = _parse_json_response(response)
    if data.get("errcode") == "M_USER_IN_USE":
        return {}
    logger.warning(
        "registration_token flow failed for %s: %s %s",
        username,
        data.get("errcode", ""),
        data.get("error", ""),
    )
    return None  # Fall through to next strategy


async def _try_appservice_registration(http_client, url, username, password, as_token):
    """Try m.login.application_service registration. Returns response dict, empty
    dict for M_USER_IN_USE, or None if not available."""
    response = await http_client.post(
        url,
        json={
            "auth": {"type": "m.login.application_service"},
            "username": username,
            "password": password,
            "inhibit_login": True,
        },
        headers={"Authorization": f"Bearer {as_token}"},
    )
    _raise_for_server_error(response)
    if response.status_code == 200:
        return response.json()
    data = _parse_json_response(response)
    if data.get("errcode") == "M_USER_IN_USE":
        return {}
    return None


async def _try_dummy_registration(http_client, url, username, password):
    """Try m.login.dummy UIA flow. Returns response dict, empty dict for
    M_USER_IN_USE, or None if not available."""
    response = await http_client.post(
        url,
        json={"username": username, "password": password, "inhibit_login": True},
    )
    _raise_for_server_error(response)
    data = _parse_json_response(response)
    if data.get("errcode") == "M_USER_IN_USE":
        return {}
    session = data.get("session")
    flows = data.get("flows", [])
    has_dummy = any("m.login.dummy" in flow.get("stages", []) for flow in flows)
    if not (session and has_dummy):
        return None
    logger.info(
        "Completing m.login.dummy UIA for %s",
        username,
    )
    response = await http_client.post(
        url,
        json={
            "auth": {"type": "m.login.dummy", "session": session},
            "username": username,
            "password": password,
            "inhibit_login": True,
        },
    )
    _raise_for_server_error(response)
    if response.status_code == 200:
        return response.json()
    data = _parse_json_response(response)
    if data.get("errcode") == "M_USER_IN_USE":
        return {}
    return None


def _raise_for_server_error(response):
    # A 5xx, often a proxy's error page, says nothing about which flows the
    # homeserver offers, so it fails registration rather than reading as
    # "not offered" and trying the next flow against the same broken server.
    if response.status_code >= 500:
        raise MatrixClientError(
            f"Homeserver error {response.status_code} during registration"
        )


def _parse_json_response(response):
    if not response.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        data = response.json()
    except ValueError:
        # A proxy's error page can claim to be JSON.
        return {}
    return data if isinstance(data, dict) else {}


def ensure_user_exists(waldur_user):
    """
    Ensure a Matrix user account exists via the standard Matrix registration API.
    Uses POST /_matrix/client/v3/register which works with any Matrix homeserver.
    Returns the matrix_user_id.

    A user with a profile keeps the account it maps to. Without one, an ID
    another user's profile holds is refused, and so is an account that already
    exists: this Waldur did not create it.
    """

    # Check if we already have a profile
    try:
        profile = MatrixUserProfile.objects.get(user=waldur_user)
        if profile.provisioned:
            # Rows mapped onto the bot predate its reservation below, or appear
            # when the bot's localpart is changed to one a user already holds.
            if profile.matrix_user_id == get_bot_user_id():
                raise MatrixClientError(
                    f"Matrix profile {profile.uuid.hex} of {waldur_user} maps to the "
                    f"bot {profile.matrix_user_id}; delete it to provision the user again"
                )
            return profile.matrix_user_id
    except MatrixUserProfile.DoesNotExist:
        profile = None

    # Not the derived ID: after a change of MATRIX_USER_ID_FORMAT or username,
    # or a deliberate link, that can be someone else's account.
    if profile is not None:
        matrix_user_id = profile.matrix_user_id
    else:
        matrix_user_id = generate_matrix_user_id(waldur_user)
    # The appservice acts for whatever ID a user maps to, so a user whose
    # username sanitises to the bot's localpart would act as the bot.
    if matrix_user_id == get_bot_user_id():
        raise MatrixClientError(
            f"{matrix_user_id} is reserved for the bot and cannot be provisioned"
        )
    # Two users can derive one ID, e.g. alice@a.org and alice@b.org under
    # email_local. The account is then Waldur's own, and linking cannot help.
    if profile is None:
        holder = (
            MatrixUserProfile.objects.filter(matrix_user_id=matrix_user_id)
            .exclude(user=waldur_user)
            .select_related("user")
            .first()
        )
        if holder:
            raise MatrixClientError(
                f"{matrix_user_id} is already linked to {holder.user.username}; "
                f"{waldur_user.username} cannot be linked to it too"
            )
    # Checked on the stored ID too: a profile keeps its ID, and SSO reaches
    # whatever account that ID names.
    _check_reachable_only_by_own_subject(waldur_user, matrix_user_id)

    # Extract localpart from the full matrix user ID (@localpart:domain)
    localpart = matrix_user_id.split(":")[0].lstrip("@")

    secret = config.MATRIX_USER_REGISTRATION_SECRET
    if not secret:
        raise MatrixClientError(
            "MATRIX_USER_REGISTRATION_SECRET must be configured for user provisioning"
        )

    homeserver_url, bot_user_id, _ = _get_client_params()
    as_token = config.MATRIX_APPSERVICE_AS_TOKEN
    if not as_token:
        raise MatrixClientError(
            "MATRIX_APPSERVICE_AS_TOKEN must be configured for user provisioning"
        )
    # Known to nobody and stored nowhere: Tuwunel treats an account without a
    # password as deactivated and refuses single sign-on into it.
    result = _run_async(
        _register_user_async(
            homeserver_url, bot_user_id, localpart, new_password(), as_token, secret
        )
    )

    # The account already existed. Without a profile here it is not one this
    # Waldur created, e.g. the homeserver admin's or a self-registered user's,
    # and the appservice would sign this user in as its owner. A profile for
    # this ID that appeared meanwhile is a concurrent provisioning of this same
    # user.
    if not result and profile is None:
        appeared = MatrixUserProfile.objects.filter(user=waldur_user).first()
        if appeared is None:
            # link_matrix_account refuses an admin's account, so don't point
            # the operator at it.
            _refuse_homeserver_admin(matrix_user_id)
            raise MatrixClientError(
                f"{matrix_user_id} already belongs to an account this Waldur did "
                f"not create. Link it deliberately with `waldur link_matrix_account "
                f"{waldur_user.username} {matrix_user_id}`, or give the user "
                f"another Matrix ID."
            )
        if appeared.matrix_user_id != matrix_user_id:
            # A link to another account landed meanwhile; the derived ID is
            # still someone else's.
            raise MatrixClientError(
                f"{waldur_user.username} was linked to {appeared.matrix_user_id} "
                f"meanwhile; try again"
            )
    # A profile linked before adoption was refused can map to the admin's
    # account; reprovisioning re-adopts it otherwise.
    if not result and profile is not None:
        _refuse_homeserver_admin(matrix_user_id)

    # Create or update profile. get_or_create guards against a concurrent
    # provisioning task creating the same OneToOne profile between the earlier
    # lookup and now, which would otherwise raise IntegrityError.
    if profile is None:
        profile, _ = MatrixUserProfile.objects.get_or_create(
            user=waldur_user,
            defaults={"matrix_user_id": matrix_user_id},
        )
        # A link that landed after a successful registration wins.
        matrix_user_id = profile.matrix_user_id

    profile.mark_provisioned()

    # Set the Matrix display name to the user's full name
    display_name = waldur_user.full_name or waldur_user.username
    try:
        set_display_name(matrix_user_id, display_name)
    except Exception as e:
        logger.warning("Failed to set display name for %s: %s", matrix_user_id, e)

    logger.info(
        "Provisioned Matrix user %s for Waldur user %s", matrix_user_id, waldur_user
    )
    return matrix_user_id


def set_display_name(matrix_user_id, display_name):
    """Set the Matrix display name for a user via the appservice token."""
    homeserver_url = config.MATRIX_HOMESERVER_URL
    as_token = _get_as_token()
    if not as_token:
        return

    encoded_user_id = quote(matrix_user_id)
    url = f"{homeserver_url}/_matrix/client/v3/profile/{encoded_user_id}/displayname"

    _run_async(_set_display_name_async(url, as_token, matrix_user_id, display_name))


async def _set_display_name_async(url, as_token, matrix_user_id, display_name):
    async with httpx.AsyncClient() as http_client:
        response = await http_client.put(
            url,
            json={"displayname": display_name},
            headers={"Authorization": f"Bearer {as_token}"},
            params={"user_id": matrix_user_id},
        )
        if response.status_code not in (200, 204):
            logger.warning(
                "Failed to set display name for %s: %s %s",
                matrix_user_id,
                response.status_code,
                response.text,
            )
            return
        logger.info(
            "Set Matrix display name for %s to '%s'",
            matrix_user_id,
            display_name,
        )


# The characters a Matrix localpart may hold; spec 1.8 added "+", which SSO
# logins keep, so Waldur keeps it too. "=" is allowed as well, but it is the
# escape character below, so it is escaped itself rather than kept.
LOCALPART_CHARS = frozenset(string.ascii_lowercase + string.digits + "._-/+")


def _localpart_char(c):
    if c in LOCALPART_CHARS:
        return c
    # Kept as is, a literal "=" would let the username "j=c3=bcri" take the
    # Matrix ID of "jüri". The spec's mapping escapes it the same way.
    if c == "=":
        return "=3d"
    # Other ASCII has always become "_"; changing it would make
    # link_matrix_account --all miss existing accounts after a restore.
    # Non-ASCII letters become "=xx" per UTF-8 byte, so that m.müller and
    # m.möller stay different users.
    if c.isascii():
        return "_"
    return "".join(f"={byte:02x}" for byte in c.encode())


# The one non-ASCII character that lowercases to ASCII: the Kelvin sign
# becomes "k". Matched by no case-insensitive database lookup.
LOWERCASES_TO_ASCII = "\u212a"


def sso_reaches_other_subject(username, matrix_user_id):
    """Whether, with single sign-on, the ID is not the user's own claim.

    The homeserver takes an SSO claim as the localpart as it is (lowercased),
    and a trusted provider signs in to any account so named. An ID Waldur had
    to transform, such as "alice smith" into alice_smith or "a=b" into a=3db,
    is therefore the account of whichever other subject's claim reads like it.
    A non-ASCII username counts too, even where lowercasing turns it into the
    ID, as the Kelvin sign in "\u212aate" does: that ID is also the claim of the
    user kate. False when single sign-on is off.
    """
    if config.MATRIX_EXTERNAL_LOGIN_METHOD != "oidc":
        return False
    if not username.isascii():
        return True
    return matrix_user_id.split(":")[0].lstrip("@") != username.lower()


def _sso_exposure(username, registration_method, matrix_user_id, shares_claim):
    """Why, with single sign-on, someone other than the user could sign in to
    the ID; None if no one could."""
    if config.MATRIX_EXTERNAL_LOGIN_METHOD != "oidc":
        return None
    if sso_reaches_other_subject(username, matrix_user_id):
        return (
            f"their username does not become the Matrix ID {matrix_user_id} unchanged"
        )
    # The homeserver signs in any subject of its own identity provider, so an
    # account named after a user who signs in to Waldur some other way, such
    # as a local or SAML user bob, belongs to whoever that provider calls bob.
    sso_method = config.MATRIX_SSO_REGISTRATION_METHOD.strip()
    if not sso_method:
        return "MATRIX_SSO_REGISTRATION_METHOD is not set"
    if registration_method != sso_method:
        return (
            f"they sign in to Waldur through {registration_method or 'no'} "
            f"identity provider, not through {sso_method}, the one the "
            "homeserver's single sign-on uses"
        )
    # Alice and alice are two Waldur users, and both their claims reach
    # @alice: the homeserver lowercases the claim.
    if shares_claim:
        return "another Waldur user's username differs from theirs only in case"
    return None


def _shares_claim(waldur_user):
    """Whether another Waldur user, active or not, has a username the
    homeserver lowercases to the same claim."""
    claim = waldur_user.username.lower()
    candidates = (
        User.all_objects.filter(
            Q(username__iexact=waldur_user.username)
            | Q(username__contains=LOWERCASES_TO_ASCII)
        )
        .exclude(pk=waldur_user.pk)
        .values_list("username", flat=True)
    )
    return any(username.lower() == claim for username in candidates)


def sso_exposure(waldur_user, matrix_user_id):
    """Why, with single sign-on, someone other than `waldur_user` could sign in
    to `matrix_user_id`; None if no one could, or single sign-on is off."""
    if config.MATRIX_EXTERNAL_LOGIN_METHOD != "oidc":
        return None
    return _sso_exposure(
        waldur_user.username,
        waldur_user.registration_method,
        matrix_user_id,
        _shares_claim(waldur_user),
    )


def sso_exposed_ids():
    """The provisioned Matrix IDs that, with single sign-on, someone other
    than their user could sign in to, sorted."""
    if config.MATRIX_EXTERNAL_LOGIN_METHOD != "oidc":
        return []
    claims = Counter(
        username.lower()
        for username in User.all_objects.values_list("username", flat=True).iterator()
    )
    return [
        matrix_user_id
        for matrix_user_id, username, registration_method in (
            MatrixUserProfile.objects.filter(provisioned=True)
            .order_by("matrix_user_id")
            .values_list(
                "matrix_user_id", "user__username", "user__registration_method"
            )
            .iterator()
        )
        if _sso_exposure(
            username,
            registration_method,
            matrix_user_id,
            claims[username.lower()] > 1,
        )
    ]


def _check_reachable_only_by_own_subject(waldur_user, matrix_user_id):
    """Refuse, with single sign-on, an ID someone else could sign in to."""
    reason = sso_exposure(waldur_user, matrix_user_id)
    if reason:
        raise MatrixClientError(
            f"{waldur_user} cannot be provisioned with single sign-on: {reason}, "
            f"so another identity provider subject could sign in to "
            f"{matrix_user_id}. See the sso_id_collisions diagnostic."
        )


def generate_matrix_user_id(waldur_user):
    """Generate a Matrix user ID from a Waldur user based on configured format."""
    domain = config.MATRIX_HOMESERVER_DOMAIN
    user_id_format = config.MATRIX_USER_ID_FORMAT

    if user_id_format == "uuid":
        localpart = str(waldur_user.uuid).replace("-", "")
    elif user_id_format == "email_local":
        email = waldur_user.email
        if email and "@" in email:
            localpart = email.split("@")[0]
        else:
            localpart = waldur_user.username
    else:  # default: "username"
        localpart = waldur_user.username

    localpart = "".join(_localpart_char(c) for c in localpart.lower())

    return f"@{localpart}:{domain}"


def get_power_level_for_scope(user, scope):
    """
    Determine the Matrix power level for a user in a given scope.

    Returns:
        50 for Project Admin, or anyone who may create the project's room
        0 for all other members

    No Waldur user gets the bot's 100: the bot is not a Waldur user, and one
    named like its localpart is someone else, possibly linked to another
    Matrix account.
    """
    if isinstance(scope, Project):
        # Check if user is project admin
        is_admin = UserRole.objects.filter(
            user=user,
            scope=scope,
            role__name=RoleEnum.PROJECT_ADMIN,
            is_active=True,
        ).exists()
        if is_admin:
            return 50

        # Whoever may create the project's room runs it. Only role grants
        # count: staff and support become moderators by joining the room.
        for room_scope in (scope, scope.customer):
            can_create_room = UserRole.objects.filter(
                user=user,
                scope=room_scope,
                role__permissions__permission=PermissionEnum.CREATE_MATRIX_ROOM,
                is_active=True,
            ).exists()
            if can_create_room:
                return 50

    return 0


# Devices the chat drawer signs in on. Every web session gets its own: Tuwunel
# keeps one refresh token per device, so two browser tabs sharing a device
# revoke it as soon as their refreshes interleave.
WEB_DEVICE_PREFIX = "WALDUR_WEB_"
# Bounds how many web devices a user accumulates. Nothing on the homeserver
# removes a device whose tab was closed, so new sessions sign out the oldest.
MAX_WEB_DEVICES = 10
# Matches the default refresh_token_ttl that Helm and docker-compose set: a web
# device unseen for longer can no longer refresh, so signing it out loses
# nothing. A homeserver configured with a longer refresh lifetime loses
# sessions idle past this one, which only means a new session.
WEB_DEVICE_IDLE = timedelta(hours=24)
# A device seen this recently belongs to an open tab, so the cap never evicts
# it: more open tabs than the cap would otherwise sign each other out in turn.
# Covers the 5-minute access token lifetime plus the homeserver's lag in
# updating last_seen_ts.
RECENT_WEB_DEVICE = timedelta(minutes=10)


def _homeserver_call(method, path, token, **kwargs):
    """Call the homeserver, reporting transport failures as MatrixClientError."""
    try:
        return httpx.request(
            method,
            f"{config.MATRIX_HOMESERVER_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            **kwargs,
        )
    except httpx.HTTPError as e:
        raise MatrixClientError(f"Homeserver unreachable: {e}") from e


def is_homeserver_reachable():
    """Whether the homeserver responds at all."""
    try:
        response = _homeserver_call("GET", "/_matrix/client/versions", _get_as_token())
    except MatrixClientError:
        return False
    return response.status_code == 200


def _refusal(response, action):
    """The error for a call the homeserver refused, a locked account told apart."""
    locked = _parse_json_response(response).get("errcode") == "M_USER_LOCKED"
    return (MatrixUserLocked if locked else MatrixClientError)(
        f"Failed to {action}: {response.status_code} {response.text}"
    )


def _json_body(response):
    try:
        return response.json()
    except ValueError as e:
        raise MatrixClientError(
            f"Homeserver returned no JSON ({response.status_code})"
        ) from e


def _appservice_login(
    matrix_user_id, device_id, refresh_token=False, display_name=None
):
    # Whatever ID reached here, a login as the bot would hand out its power
    # level in every Waldur room.
    if matrix_user_id == get_bot_user_id():
        raise MatrixClientError("Refusing to sign in as the bot")
    body = {
        "type": "m.login.application_service",
        "identifier": {"type": "m.id.user", "user": matrix_user_id},
        "device_id": device_id,
    }
    if refresh_token:
        body["refresh_token"] = True
    if display_name:
        body["initial_device_display_name"] = display_name
    response = _homeserver_call(
        "POST", "/_matrix/client/v3/login", _get_as_token(), json=body
    )
    if response.status_code != 200:
        raise _refusal(response, f"log in as {matrix_user_id}")
    data = _json_body(response)
    if not data.get("access_token"):
        raise MatrixClientError(f"Login as {matrix_user_id} returned no token")
    return data


# The daily prune only asks the homeserver about users who may still have a web
# device. Waldur marks a user whenever it creates one for them, and the prune
# unmarks them once none is left.


def mark_web_session(matrix_user_id):
    """Mark the user as having a web device. Call once the device exists, so a
    prune that unmarks the user before this write has already seen it."""
    MatrixUserProfile.objects.filter(matrix_user_id=matrix_user_id).update(
        last_web_session_at=timezone.now()
    )


def get_web_session_mark(matrix_user_id):
    return (
        MatrixUserProfile.objects.filter(matrix_user_id=matrix_user_id)
        .values_list("last_web_session_at", flat=True)
        .first()
    )


def clear_web_session_mark(matrix_user_id, marked_at):
    """Unmark the user, unless the mark has changed since it read `marked_at`.

    A session started meanwhile has set a newer mark, and its device must keep
    the user marked. Read `marked_at` before listing the devices.
    """
    if marked_at is None:
        return
    MatrixUserProfile.objects.filter(
        matrix_user_id=matrix_user_id, last_web_session_at=marked_at
    ).update(last_web_session_at=None)


def create_web_session(matrix_user_id):
    """Sign the user in on a new web device and return its tokens.

    The tokens are handed to the browser and never stored. With refresh tokens
    the access token expires after the homeserver's access_token_ttl; a
    homeserver without refresh support returns neither expiry nor refresh token.
    """
    # _appservice_login refuses the bot without asking the homeserver.
    if matrix_user_id != get_bot_user_id():
        _refuse_homeserver_admin(matrix_user_id)
    device_id = WEB_DEVICE_PREFIX + secrets.token_hex(6).upper()
    data = _appservice_login(
        matrix_user_id,
        device_id,
        refresh_token=True,
        display_name=f"{config.SITE_NAME} web chat",
    )
    mark_web_session(matrix_user_id)
    return {
        "device_id": data.get("device_id", device_id),
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_in_ms": data.get("expires_in_ms"),
    }


def is_homeserver_admin(matrix_user_id):
    """Whether the homeserver reports the account as one of its admins.

    The admin API answers the appservice only once the bot is a homeserver
    admin itself; None means the homeserver would not tell.
    """
    response = _homeserver_call(
        "GET",
        f"/_synapse/admin/v2/users/{quote(matrix_user_id, safe='')}",
        _get_as_token(),
    )
    body = _parse_json_response(response)
    # Only an unknown user is known not to be an admin; any other 404 is an
    # endpoint the homeserver lacks or a proxy blocks.
    if response.status_code == 404 and body.get("errcode") == "M_NOT_FOUND":
        return False
    # A 200 without the flag, e.g. a proxy's page, says nothing either.
    if response.status_code != 200 or not isinstance(body.get("admin"), bool):
        return None
    return body["admin"]


def _refuse_homeserver_admin(matrix_user_id):
    # An admin's session can run admin-room commands, so no Waldur user may
    # hold one, whoever linked it.
    if is_homeserver_admin(matrix_user_id):
        raise MatrixAccountIsHomeserverAdmin(
            f"{matrix_user_id} is a homeserver admin; Waldur does not act as it "
            "for a user"
        )


def account_exists(matrix_user_id):
    """Whether the homeserver has an account with this ID."""
    response = _homeserver_call(
        "GET",
        f"/_matrix/client/v3/profile/{quote(matrix_user_id, safe='')}",
        _get_as_token(),
    )
    if response.status_code == 404:
        return False
    if response.status_code != 200:
        raise MatrixClientError(
            f"Could not look up {matrix_user_id}: {response.status_code} {response.text}"
        )
    return True


# A web device, so pruning and deactivation remove it if signing it out fails.
DIAGNOSTICS_DEVICE_ID = WEB_DEVICE_PREFIX + "DIAGNOSTICS"


def probe_web_token_lifetime(matrix_user_id):
    """Milliseconds a chat drawer access token lives on the homeserver, None if
    it never expires. Signs in like the drawer on a throwaway device, then signs
    it out again."""
    data = _appservice_login(matrix_user_id, DIAGNOSTICS_DEVICE_ID, refresh_token=True)
    mark_web_session(matrix_user_id)
    lifetime_ms = data.get("expires_in_ms")
    # The measurement stands whatever the logout does; the device is left to
    # pruning.
    try:
        response = _homeserver_call(
            "POST", "/_matrix/client/v3/logout", data["access_token"], json={}
        )
    except MatrixClientError as e:
        logger.warning(
            "Failed to sign out %s of %s: %s",
            DIAGNOSTICS_DEVICE_ID,
            matrix_user_id,
            e,
        )
    else:
        if response.status_code != 200:
            logger.warning(
                "Failed to sign out %s of %s: %s %s",
                DIAGNOSTICS_DEVICE_ID,
                matrix_user_id,
                response.status_code,
                response.text,
            )
    return lifetime_ms


def list_devices(matrix_user_id):
    response = _homeserver_call(
        "GET",
        "/_matrix/client/v3/devices",
        _get_as_token(),
        params={"user_id": matrix_user_id},
    )
    if response.status_code != 200:
        raise _refusal(response, f"list devices of {matrix_user_id}")
    return _json_body(response).get("devices", [])


def is_joined(matrix_user_id, room_id):
    """Whether the homeserver has the user joined to the room now.

    Asks as the user through the appservice. An invite or a past membership
    is not a join.
    """
    response = _homeserver_call(
        "GET",
        "/_matrix/client/v3/joined_rooms",
        _get_as_token(),
        params={"user_id": matrix_user_id},
    )
    if response.status_code != 200:
        raise _refusal(response, f"list joined rooms of {matrix_user_id}")
    joined_rooms = _json_body(response).get("joined_rooms")
    if not isinstance(joined_rooms, list):
        raise MatrixClientError("Homeserver returned no joined_rooms list")
    return room_id in joined_rooms


def get_openid_user(access_token):
    """The Matrix ID a local OpenID token was issued to, or None for a token
    the homeserver does not recognise.

    Uses the federation userinfo endpoint, which takes the token only as a
    query parameter; httpx's request log is redacted for it (see
    _RedactAccessToken).
    """
    try:
        response = httpx.get(
            f"{config.MATRIX_HOMESERVER_URL}/_matrix/federation/v1/openid/userinfo",
            params={"access_token": access_token},
            timeout=10,
        )
    except httpx.HTTPError as e:
        # The exception text carries the URL, token included.
        raise MatrixClientError(f"Homeserver unreachable: {type(e).__name__}") from None
    if response.status_code == 401:
        return None
    if response.status_code != 200:
        raise MatrixClientError(
            f"OpenID userinfo returned {response.status_code}: {response.text[:200]}"
        )
    sub = _json_body(response).get("sub")
    if not isinstance(sub, str) or not sub:
        raise MatrixClientError("OpenID userinfo returned no sub")
    return sub


def list_web_devices(matrix_user_id):
    """The user's devices that Waldur's web chat signed in on. Element and other
    clients the user signed in to are left out."""
    return [
        device
        for device in list_devices(matrix_user_id)
        if device["device_id"].startswith(WEB_DEVICE_PREFIX)
    ]


def logout_device(matrix_user_id, device_id):
    """Remove a device and every token on it.

    The appservice cannot delete a device without user-interactive auth, but a
    token issued on the device can log it out, which removes the device.
    """
    # With a refresh token the helper token expires on the homeserver's
    # access_token_ttl, so a failed /logout does not leave it valid for good.
    token = _appservice_login(matrix_user_id, device_id, refresh_token=True)[
        "access_token"
    ]
    response = _homeserver_call("POST", "/_matrix/client/v3/logout", token, json={})
    if response.status_code != 200:
        raise MatrixClientError(
            f"Failed to log out {device_id} of {matrix_user_id}: "
            f"{response.status_code} {response.text}"
        )


def logout_web_devices(matrix_user_id):
    """End every web chat session of the user, leaving their other devices alone."""
    _logout_devices(
        matrix_user_id, lambda device_id: device_id.startswith(WEB_DEVICE_PREFIX)
    )


def logout_all_devices(matrix_user_id):
    """End every session of the user, external clients such as Element included."""
    _logout_devices(matrix_user_id, lambda device_id: True)


def _logout_devices(matrix_user_id, selected):
    # Tries every device before reporting a failure, so one device the
    # homeserver keeps rejecting cannot shield the others.
    failed = []
    for device in list_devices(matrix_user_id):
        if not selected(device["device_id"]):
            continue
        try:
            logout_device(matrix_user_id, device["device_id"])
        except MatrixUserLocked:
            # Locked meanwhile: the other devices are refused the same way.
            raise
        except MatrixClientError as e:
            failed.append(f"{device['device_id']}: {e}")
    if failed:
        raise MatrixClientError(
            f"Could not sign out devices of {matrix_user_id}: {'; '.join(failed)}"
        )


def stale_web_devices(devices, now_ms, keep_device_id=None):
    """IDs of web devices to sign out: idle past the refresh window, or beyond
    the newest MAX_WEB_DEVICES and not in use. Never `keep_device_id`."""
    web = sorted(
        (d for d in devices if d["device_id"].startswith(WEB_DEVICE_PREFIX)),
        key=lambda d: d.get("last_seen_ts") or 0,
        reverse=True,
    )
    stale = []
    for rank, device in enumerate(web):
        seen = device.get("last_seen_ts")
        # Some homeservers fill last_seen_ts only on a device's first request,
        # so a device never seen is not idle, though it still counts to the cap.
        age = None if seen is None else timedelta(milliseconds=now_ms - seen)
        idle = age is not None and age > WEB_DEVICE_IDLE
        in_use = age is not None and age <= RECENT_WEB_DEVICE
        over_cap = rank >= MAX_WEB_DEVICES and not in_use
        if device["device_id"] != keep_device_id and (idle or over_cap):
            stale.append(device["device_id"])
    return stale


def get_user_matrix_credentials(waldur_user):
    """Return what an external Matrix client needs to sign the user in.

    No secret in any mode: in `password` mode the user generates a password
    (MatrixPasswordView), and Waldur's chat drawer gets its own short-lived
    session.
    """
    method = config.MATRIX_EXTERNAL_LOGIN_METHOD
    if method not in dict(
        settings.CONSTANCE_CONFIG_CHOICES["MATRIX_EXTERNAL_LOGIN_METHOD"]
    ):
        raise MatrixClientError(f"Unknown login method: {method}")

    try:
        profile = MatrixUserProfile.objects.get(user=waldur_user, provisioned=True)
    except MatrixUserProfile.DoesNotExist:
        # Provision user on-demand so they can access credentials immediately
        ensure_user_exists(waldur_user)
        try:
            profile = MatrixUserProfile.objects.get(user=waldur_user, provisioned=True)
        except MatrixUserProfile.DoesNotExist:
            raise MatrixClientError("User has not been provisioned on Matrix yet")

    credentials = {
        "method": method,
        "homeserver_url": get_public_homeserver_url(),
        "matrix_user_id": profile.matrix_user_id,
    }
    return credentials


def get_cross_signing_master_key(matrix_user_id):
    """The user's published cross-signing master key, or None if they have none."""
    response = _homeserver_call(
        "POST",
        "/_matrix/client/v3/keys/query",
        _get_as_token(),
        json={"device_keys": {matrix_user_id: []}},
    )
    if response.status_code != 200:
        raise _refusal(response, f"query the keys of {matrix_user_id}")
    data = _parse_json_response(response)
    # A server that couldn't answer for the user says so in `failures`; that is
    # not the same as the user having no identity.
    if data.get("failures"):
        raise MatrixClientError(
            f"Could not query the keys of {matrix_user_id}: {data['failures']}"
        )
    return data.get("master_keys", {}).get(matrix_user_id)


def _get_account_data(matrix_user_id, event_type):
    """One of the user's account data events, read through the appservice."""
    response = _homeserver_call(
        "GET",
        f"/_matrix/client/v3/user/{quote(matrix_user_id, safe='')}"
        f"/account_data/{quote(event_type, safe='')}",
        _get_as_token(),
        params={"user_id": matrix_user_id},
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise _refusal(response, f"read {event_type} of {matrix_user_id}")
    return _parse_json_response(response)


def get_secret_storage_key_info(matrix_user_id):
    """The description of the user's default secret-storage key, or None."""
    return get_secret_storage_state(matrix_user_id)[1]


def get_secret_storage_state(matrix_user_id):
    """``(key id, key description, master key stored)`` of the user's secret storage.

    The last is whether the cross-signing master key is stored under the default
    key; without it, secret storage that the key opens still unlocks nothing.
    """
    key_id, key_info, stored_master = get_stored_master_key(matrix_user_id)
    return key_id, key_info, stored_master is not None


def get_stored_master_key(matrix_user_id):
    """``(key id, key description, stored master key)`` of the user's secret storage.

    The last is the encrypted ``m.cross_signing.master`` item under the default
    key, or None.
    """
    default = _get_account_data(matrix_user_id, "m.secret_storage.default_key")
    key_id = (default or {}).get("key")
    if not key_id or not isinstance(key_id, str):
        return None, None, None
    key_info = _get_account_data(matrix_user_id, f"m.secret_storage.key.{key_id}")
    master = _get_account_data(matrix_user_id, "m.cross_signing.master") or {}
    encrypted = master.get("encrypted") if isinstance(master, dict) else None
    stored = encrypted.get(key_id) if isinstance(encrypted, dict) else None
    return key_id, key_info, stored if isinstance(stored, dict) else None


def new_password():
    return secrets.token_urlsafe(32)


def _admin_call(method, path, action, **kwargs):
    """Call the homeserver's admin API as the bot, which has to be a homeserver
    admin (`!admin users make-user-admin` on Tuwunel)."""
    response = _homeserver_call(method, path, _get_as_token(), **kwargs)
    if response.status_code == 403:
        raise MatrixAdminRequired(
            f"The homeserver refused to {action}: "
            f"{get_bot_user_id()} is not a homeserver admin"
        )
    # M_NOT_FOUND is the admin API saying the user does not exist. Any other
    # 404 or 405 comes from a proxy that blocks the API or a homeserver
    # without it, and retrying cannot help.
    if response.status_code in (404, 405):
        if _parse_json_response(response).get("errcode") == "M_NOT_FOUND":
            raise MatrixUserNotFound(
                f"Failed to {action}: {response.status_code} {response.text}"
            )
        raise MatrixAdminRequired(
            f"Could not {action}: the homeserver's admin API is not reachable "
            f"at MATRIX_HOMESERVER_URL ({response.status_code})"
        )
    if response.status_code != 200:
        raise MatrixClientError(
            f"Failed to {action}: {response.status_code} {response.text}"
        )
    return response


def _require_no_homeserver_admin(matrix_user_id, action):
    """Raise unless the homeserver says the account is not one of its admins.

    Unlike _refuse_homeserver_admin this does not go ahead on an answer that
    says nothing: the write that follows would succeed on an admin's account
    whenever only this read failed.
    """
    response = _admin_call("GET", _admin_path("v2/users", matrix_user_id), action)
    admin = _parse_json_response(response).get("admin")
    if admin is True:
        raise MatrixAccountIsHomeserverAdmin(
            f"{matrix_user_id} is a homeserver admin; Waldur does not {action}"
        )
    if admin is not False:
        raise MatrixClientError(
            f"Could not {action}: the homeserver did not say whether it is an "
            "admin's account"
        )


def _admin_path(prefix, matrix_user_id):
    # safe="": a "/" or ".." in a generated localpart would otherwise address
    # another admin endpoint.
    return f"/_synapse/admin/{prefix}/{quote(matrix_user_id, safe='')}"


def set_password(matrix_user_id, password, logout_devices=False):
    """Set a user's password, and sign out all their devices if asked to.

    The client API only changes a password after proving the current one, and
    nobody knows the one an account was registered with.
    """
    if matrix_user_id == get_bot_user_id():
        raise MatrixClientError("Refusing to set the bot's password")
    action = f"set the password of {matrix_user_id}"
    # A user linked to an admin's account before that was refused would take
    # the homeserver over with its password.
    _require_no_homeserver_admin(matrix_user_id, action)
    _admin_call(
        "POST",
        _admin_path("v1/reset_password", matrix_user_id),
        action,
        json={"new_password": password, "logout_devices": logout_devices},
    )


def set_locked(matrix_user_id, locked):
    """Lock or unlock an account.

    A locked account refuses its tokens and every new login, by password,
    single sign-on or the appservice, until it is unlocked. Returns whether
    the homeserver was asked to.
    """
    action = f"{'lock' if locked else 'unlock'} {matrix_user_id}"
    # As in kick_user: a stored ID can map onto the bot, and locking it would
    # stop chat for everyone. Skipped rather than raised, so revocation retries
    # don't spin on it.
    if locked and matrix_user_id == get_bot_user_id():
        logger.warning("Not locking the bot %s", matrix_user_id)
        return False
    # Nor a homeserver admin's account, which a user was linked to before that
    # was refused: deactivating them in Waldur must not lock the admin out.
    # Unlocking one is harmless, and undoes a lock placed before the check.
    if locked:
        try:
            _require_no_homeserver_admin(matrix_user_id, action)
        except MatrixAccountIsHomeserverAdmin:
            logger.warning("Not locking %s, a homeserver admin", matrix_user_id)
            return False
    _admin_call(
        "PUT",
        _admin_path("v2/users", matrix_user_id),
        action,
        json={"locked": locked},
    )
    return True
