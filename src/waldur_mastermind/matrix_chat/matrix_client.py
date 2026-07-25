from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx
from constance import config
from markdown_it import MarkdownIt

from waldur_auth_social.models import IdentityProvider
from waldur_core.core.clean_html import clean_html
from waldur_core.permissions.enums import RoleEnum
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
        RoomSendError,
        RoomSendResponse,
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


class MatrixClientError(Exception):
    pass


def get_bot_display_name():
    # Derived from SITE_NAME so whitelabel deployments don't surface "Waldur"
    # in Matrix clients.
    return f"{config.SITE_NAME} Bot"


def is_enabled():
    return bool(
        config.MATRIX_ENABLED
        and config.MATRIX_HOMESERVER_URL
        and config.MATRIX_APPSERVICE_AS_TOKEN
    )


def get_bot_user_id():
    """Return the bot's full Matrix user ID."""
    localpart = config.MATRIX_APPSERVICE_SENDER_LOCALPART or "waldur-bot"
    return f"@{localpart}:{config.MATRIX_HOMESERVER_DOMAIN}"


def _get_access_token():
    """Return the appservice access token."""
    return config.MATRIX_APPSERVICE_AS_TOKEN


def _get_client_params():
    """Read config values synchronously (safe from sync context) and return them."""
    return config.MATRIX_HOMESERVER_URL, get_bot_user_id(), _get_access_token()


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
    as_token = _get_access_token()
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
            invite=[],
            initial_state=[
                {
                    "type": "m.room.join_rules",
                    "content": {"join_rule": "invite" if is_private else "public"},
                },
                {
                    "type": "m.room.history_visibility",
                    "content": {"history_visibility": "shared"},
                },
            ],
            power_level_override={
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
                    "org.matrix.msc3401.call.member": 0,
                },
            },
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
                    invite=[],
                    initial_state=[
                        {
                            "type": "m.room.join_rules",
                            "content": {
                                "join_rule": "invite" if is_private else "public"
                            },
                        },
                        {
                            "type": "m.room.history_visibility",
                            "content": {"history_visibility": "shared"},
                        },
                    ],
                    power_level_override={
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
                            "org.matrix.msc3401.call.member": 0,
                        },
                    },
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


async def _join_room_as_self_async(homeserver_url, room_id, access_token):
    url = f"{homeserver_url}/_matrix/client/v3/join/{room_id}"
    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            url,
            json={},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 200:
            return True
        data = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        )
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


def join_room_as_self(room_id, access_token):
    """Join a Matrix room using the user's own access token (accepts their pending invite)."""
    homeserver_url = config.MATRIX_HOMESERVER_URL
    return _run_async(_join_room_as_self_async(homeserver_url, room_id, access_token))


async def _leave_room_as_self_async(homeserver_url, room_id, access_token):
    url = f"{homeserver_url}/_matrix/client/v3/rooms/{room_id}/leave"
    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            url,
            json={},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 200:
            return True
        data = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        )
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


def leave_room_as_self(room_id, access_token):
    """Leave a Matrix room using the user's own access token."""
    homeserver_url = config.MATRIX_HOMESERVER_URL
    return _run_async(_leave_room_as_self_async(homeserver_url, room_id, access_token))


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
    """Set a user's power level in a Matrix room."""
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


async def _send_message_async(
    homeserver_url, bot_user_id, access_token, room_id, body, msgtype="m.text"
):
    _load_nio()
    client = _make_client(homeserver_url, bot_user_id, access_token)
    try:
        response = await client.room_send(
            room_id,
            "m.room.message",
            build_text_content(body, msgtype),
        )
        if isinstance(response, RoomSendError):
            raise MatrixClientError(
                f"Failed to send message to {room_id}: {response.message}"
            )
        if isinstance(response, RoomSendResponse):
            return response.event_id
        raise MatrixClientError(f"Unexpected response: {response}")
    finally:
        await client.close()


def send_message(room_id, body, msgtype="m.text"):
    """Send a text message to a Matrix room as the bot. Returns the event_id."""
    homeserver_url, bot_user_id, access_token = _get_client_params()
    return _run_async(
        _send_message_async(
            homeserver_url, bot_user_id, access_token, room_id, body, msgtype
        )
    )


async def _send_reply_async(
    homeserver_url, bot_user_id, access_token, room_id, event_id, body
):
    _load_nio()
    client = _make_client(homeserver_url, bot_user_id, access_token)
    try:
        content = build_text_content(body, reply_to=event_id)
        response = await client.room_send(room_id, "m.room.message", content)
        if isinstance(response, RoomSendError):
            raise MatrixClientError(
                f"Failed to send reply to {room_id}: {response.message}"
            )
        if isinstance(response, RoomSendResponse):
            return response.event_id
        raise MatrixClientError(f"Unexpected response: {response}")
    finally:
        await client.close()


def send_reply(room_id, event_id, body):
    """Send a reply to a specific event in a Matrix room. Returns the event_id."""
    homeserver_url, bot_user_id, access_token = _get_client_params()
    return _run_async(
        _send_reply_async(
            homeserver_url, bot_user_id, access_token, room_id, event_id, body
        )
    )


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


def _derive_password(secret, user_uuid):
    """Derive a deterministic password from a shared secret and user UUID using HMAC-SHA256."""
    return hmac.new(
        secret.encode(), str(user_uuid).encode(), hashlib.sha256
    ).hexdigest()


async def _register_user_async(
    homeserver_url, bot_user_id, username, password, as_token, registration_secret=""
):
    """Register a Matrix user via the standard CS API.

    Tries multiple registration strategies in order:
    1. m.login.registration_token — sets password on all homeservers (Tuwunel, Synapse).
    2. m.login.application_service — appservice namespace registration (Synapse stores
       password, Tuwunel does not).
    3. m.login.dummy — open registration fallback.

    Returns the registration response dict on success, or None if the user
    already exists.
    """
    url = f"{homeserver_url}/_matrix/client/v3/register"

    async with httpx.AsyncClient() as http_client:
        # Strategy 1: registration_token flow (two-step UIA).
        # This sets the password reliably on all homeservers.
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
        url, json={"username": username, "password": password}
    )
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
        },
    )
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
        },
        headers={"Authorization": f"Bearer {as_token}"},
    )
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
        url, json={"username": username, "password": password}
    )
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
        },
    )
    if response.status_code == 200:
        return response.json()
    data = _parse_json_response(response)
    if data.get("errcode") == "M_USER_IN_USE":
        return {}
    return None


def _parse_json_response(response):
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return {}


def ensure_user_exists(waldur_user):
    """
    Ensure a Matrix user account exists via the standard Matrix registration API.
    Uses POST /_matrix/client/v3/register which works with any Matrix homeserver.
    Returns the matrix_user_id.
    """

    # Check if we already have a profile
    try:
        profile = MatrixUserProfile.objects.get(user=waldur_user)
        if profile.provisioned:
            return profile.matrix_user_id
    except MatrixUserProfile.DoesNotExist:
        profile = None

    matrix_user_id = generate_matrix_user_id(waldur_user)

    # Extract localpart from the full matrix user ID (@localpart:domain)
    localpart = matrix_user_id.split(":")[0].lstrip("@")

    # Derive a deterministic password from the shared secret and user UUID
    secret = config.MATRIX_USER_REGISTRATION_SECRET
    if not secret:
        raise MatrixClientError(
            "MATRIX_USER_REGISTRATION_SECRET must be configured for user provisioning"
        )
    password = _derive_password(secret, waldur_user.uuid)

    homeserver_url, bot_user_id, _ = _get_client_params()
    as_token = config.MATRIX_APPSERVICE_AS_TOKEN
    if not as_token:
        raise MatrixClientError(
            "MATRIX_APPSERVICE_AS_TOKEN must be configured for user provisioning"
        )
    result = _run_async(
        _register_user_async(
            homeserver_url, bot_user_id, localpart, password, as_token, secret
        )
    )

    # Create or update profile. get_or_create guards against a concurrent
    # provisioning task creating the same OneToOne profile between the earlier
    # lookup and now, which would otherwise raise IntegrityError.
    if profile is None:
        profile, _ = MatrixUserProfile.objects.get_or_create(
            user=waldur_user,
            defaults={"matrix_user_id": matrix_user_id},
        )

    # Save access_token from successful registration
    if result and result.get("access_token"):
        profile.access_token = result["access_token"]
        profile.save(update_fields=["access_token"])

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
    as_token = config.MATRIX_APPSERVICE_AS_TOKEN
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

    # Sanitize localpart: Matrix allows [a-z0-9._=\-/]
    localpart = localpart.lower()
    localpart = "".join(c if c.isalnum() or c in "._=-/" else "_" for c in localpart)

    return f"@{localpart}:{domain}"


def get_power_level_for_scope(user, scope):
    """
    Determine the Matrix power level for a user in a given scope.

    Returns:
        100 for bot account
        50 for Customer Owner or Project Admin
        0 for all other members
    """
    if f"@{user.username}:{config.MATRIX_HOMESERVER_DOMAIN}" == get_bot_user_id():
        return 100

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

        # Check if user is customer owner
        is_owner = UserRole.objects.filter(
            user=user,
            scope=scope.customer,
            role__name=RoleEnum.CUSTOMER_OWNER,
            is_active=True,
        ).exists()
        if is_owner:
            return 50

    return 0


async def _login_as_user_async(homeserver_url, as_token, matrix_user_id):
    """Log in as a Matrix user via appservice login and return an access_token.

    Uses POST /_matrix/client/v3/login with m.login.application_service
    auth type, which is part of the standard Matrix spec.
    """
    url = f"{homeserver_url}/_matrix/client/v3/login"

    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            url,
            json={
                "type": "m.login.application_service",
                "identifier": {
                    "type": "m.id.user",
                    "user": matrix_user_id,
                },
            },
            headers={"Authorization": f"Bearer {as_token}"},
        )
        if response.status_code == 200:
            return response.json().get("access_token")
        raise MatrixClientError(
            f"Failed to login as {matrix_user_id}: "
            f"{response.status_code} {response.text}"
        )


def _generate_login_token(matrix_user_id):
    """Generate an access token for a Matrix user via appservice login."""
    as_token = config.MATRIX_APPSERVICE_AS_TOKEN
    if not as_token:
        raise MatrixClientError(
            "MATRIX_APPSERVICE_AS_TOKEN must be configured for token login"
        )
    homeserver_url = config.MATRIX_HOMESERVER_URL
    return _run_async(_login_as_user_async(homeserver_url, as_token, matrix_user_id))


async def _login_with_password_async(homeserver_url, matrix_user_id, password):
    """Log in as a Matrix user with password and return an access_token."""
    url = f"{homeserver_url}/_matrix/client/v3/login"

    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            url,
            json={
                "type": "m.login.password",
                "identifier": {
                    "type": "m.id.user",
                    "user": matrix_user_id,
                },
                "password": password,
            },
        )
        if response.status_code == 200:
            return response.json().get("access_token")
        raise MatrixClientError(
            f"Failed to login as {matrix_user_id}: "
            f"{response.status_code} {response.text}"
        )


def get_access_token_for_user(waldur_user):
    """Obtain a Matrix access_token for a Waldur user.

    Tries strategies in order:
    0. Stored token from registration
    1. Appservice login
    2. Password login
    3. Re-provision (delete profile + re-register to get a fresh token)
    """
    try:
        profile = MatrixUserProfile.objects.get(user=waldur_user, provisioned=True)
    except MatrixUserProfile.DoesNotExist:
        ensure_user_exists(waldur_user)
        profile = MatrixUserProfile.objects.get(user=waldur_user, provisioned=True)

    # 0. Use stored access_token from registration
    if profile.access_token:
        return profile.access_token

    matrix_user_id = profile.matrix_user_id
    homeserver_url = config.MATRIX_HOMESERVER_URL
    errors = []

    # 1. Appservice login (user must be in the AS namespace)
    as_token = config.MATRIX_APPSERVICE_AS_TOKEN
    if as_token:
        try:
            return _run_async(
                _login_as_user_async(homeserver_url, as_token, matrix_user_id)
            )
        except MatrixClientError as e:
            errors.append(f"appservice login: {e}")
            logger.debug("Appservice login failed for %s: %s", matrix_user_id, e)

    # 2. Password login
    secret = config.MATRIX_USER_REGISTRATION_SECRET
    if secret:
        password = _derive_password(secret, waldur_user.uuid)
        try:
            return _run_async(
                _login_with_password_async(homeserver_url, matrix_user_id, password)
            )
        except MatrixClientError as e:
            errors.append(f"password login: {e}")
            logger.debug("Password login failed for %s: %s", matrix_user_id, e)

    # 3. Re-provision: delete the stale profile and re-register.
    #    This handles pre-existing users whose password doesn't match.
    logger.info(
        "All login strategies failed for %s, attempting re-provision",
        matrix_user_id,
    )
    profile.delete()
    ensure_user_exists(waldur_user)
    profile = MatrixUserProfile.objects.get(user=waldur_user, provisioned=True)
    if profile.access_token:
        return profile.access_token

    raise MatrixClientError(
        f"Cannot obtain access token for {matrix_user_id}. "
        f"Tried: {'; '.join(errors)}; re-provision (no token returned)"
    )


def get_user_matrix_credentials(waldur_user):
    """Return Matrix login credentials for a Waldur user based on configured login method."""

    method = config.MATRIX_LOGIN_METHOD

    try:
        profile = MatrixUserProfile.objects.get(user=waldur_user, provisioned=True)
    except MatrixUserProfile.DoesNotExist:
        # Provision user on-demand so they can access credentials immediately
        ensure_user_exists(waldur_user)
        try:
            profile = MatrixUserProfile.objects.get(user=waldur_user, provisioned=True)
        except MatrixUserProfile.DoesNotExist:
            raise MatrixClientError("User has not been provisioned on Matrix yet")

    if method == "password":
        secret = config.MATRIX_USER_REGISTRATION_SECRET
        if not secret:
            raise MatrixClientError("MATRIX_USER_REGISTRATION_SECRET not configured")
        password = _derive_password(secret, waldur_user.uuid)
        return {
            "method": "password",
            "homeserver_url": get_public_homeserver_url(),
            "matrix_user_id": profile.matrix_user_id,
            "password": password,
        }
    elif method == "token":
        token = _generate_login_token(profile.matrix_user_id)
        return {
            "method": "token",
            "homeserver_url": get_public_homeserver_url(),
            "matrix_user_id": profile.matrix_user_id,
            "login_token": token,
        }
    elif method == "oidc":
        try:
            idp = IdentityProvider.objects.get(is_active=True)
            oidc_provider_url = idp.auth_url
        except IdentityProvider.DoesNotExist:
            raise MatrixClientError("No active identity provider configured")
        except IdentityProvider.MultipleObjectsReturned:
            # If multiple active IDPs, fall back to the constance setting
            oidc_provider_url = config.MATRIX_OIDC_PROVIDER_URL
            if not oidc_provider_url:
                raise MatrixClientError(
                    "Multiple active identity providers found. "
                    "Set MATRIX_OIDC_PROVIDER_URL to specify which one to use."
                )
        return {
            "method": "oidc",
            "homeserver_url": get_public_homeserver_url(),
            "matrix_user_id": profile.matrix_user_id,
            "oidc_provider_url": oidc_provider_url,
        }
    else:
        raise MatrixClientError(f"Unknown login method: {method}")
