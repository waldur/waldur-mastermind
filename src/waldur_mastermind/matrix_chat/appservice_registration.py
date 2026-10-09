"""Appservice registration: descriptor construction and homeserver enrolment.

Two concerns live here, both about the handshake that makes Waldur's
Application Service known to a homeserver.

**The descriptor** — the registration YAML holding the `as_token`/`hs_token`
pair and the user namespaces the bot claims. It is built in three places
(the Setup wizard, the `generate_appservice_registration` command, and the
enrolment below), so it is defined once here.

**Enrolment on Conduit-family homeservers** (Tuwunel, Conduit, Conduwuit).
Those register appservices at runtime through an admin-room command rather
than a config file, which until now meant an operator hand-typing
`!admin appservices register` into a Matrix client. The sequence is:

1. act as a homeserver admin: an admin token, or the bootstrap admin. The
   bootstrap admin is created through the shared-secret admin registration
   API (`/_synapse/admin/v1/register`, which Tuwunel serves too), keyed with
   the registration token, which the deployment also sets as the
   homeserver's `registration_shared_secret`. That makes it an admin however
   many users the homeserver already has. Once it exists, the registration
   answers M_USER_IN_USE and it signs in with the deployment's password. Its
   session is signed out when enrolment ends; an admin token is the caller's
   and is left alone;
2. find the admin room among that user's joined rooms;
3. send the `!admin appservices register` command with the descriptor as a
   fenced YAML block, then read the admin bot's reply back to confirm;
4. make the bot a homeserver admin through the admin API
   (`PUT /_synapse/admin/v2/users/<bot>` with `admin: true`) unless it is one.
   Waldur calls that API as its bot, and this is the one run that holds an
   admin session. A failure is reported, not raised: chat works without it.

A re-run against an appservice whose token already works reads the
homeserver's copy with `show-config` and replaces it when it differs from
the descriptor, as long as it can sign in without creating anyone.

Synapse is out of scope: it loads appservices from `app_service_config_files`
and has no remote registration API, so config-first is the only route there.

Nothing in this module logs token material — failures quote homeserver error
codes and reply text, never the descriptor.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import hashlib
import hmac
import logging
import re
import secrets
import time
from urllib.parse import quote

import httpx
import yaml
from constance import config
from rest_framework.exceptions import ValidationError

from waldur_core.core.constance_backend import UNDECRYPTABLE_PREFIX
from waldur_mastermind.matrix_chat import models, serializers

logger = logging.getLogger(__name__)

APPSERVICE_ID = "waldur"

# The bootstrap user only exists to hold admin rights on a fresh homeserver.
# It is never used by the running integration — the bot logs in with as_token.
BOOTSTRAP_LOCALPART = "waldur-bootstrap"
BOOTSTRAP_DEVICE_PREFIX = "WALDUR-BOOTSTRAP"


def _bootstrap_device_id() -> str:
    """A device of its own for every bootstrap login.

    Signing out removes the session's device, and with it every token on that
    device. On a shared device, a run that finished would cut off a concurrent
    run still working, possibly between its unregister and register.
    """
    return f"{BOOTSTRAP_DEVICE_PREFIX}-{secrets.token_hex(4).upper()}"


SHARED_SECRET_REGISTER_PATH = "/_synapse/admin/v1/register"
SHARED_SECRET_HINT = (
    "The homeserver's registration_shared_secret must equal "
    "MATRIX_USER_REGISTRATION_SECRET; restart the homeserver after changing it."
)

# Conduit-family homeservers alias their admin room this way. Resolving the
# alias is more reliable than guessing when the bootstrap user has somehow
# joined more than one room.
ADMIN_ROOM_ALIAS_LOCALPART = "admins"

HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)

# The admin bot answers asynchronously: the PUT returns as soon as the command
# event is accepted, and the reply lands as a separate event a moment later.
REPLY_POLL_ATTEMPTS = 15
REPLY_POLL_INTERVAL = 1.0

# The id must end where Waldur's does: "waldur-staging" or "waldur.old" is
# another appservice, while a sentence-final "waldur." is still ours.
SUCCESS_PATTERN = re.compile(
    rf"registered with ID:?\s*[\"'`]?{re.escape(APPSERVICE_ID)}(?![\w-])(?!\.\w)",
    re.IGNORECASE,
)
UNREGISTERED_PATTERN = re.compile(r"appservice unregistered", re.IGNORECASE)
ALREADY_REGISTERED_PATTERN = re.compile(
    r"already (exists|registered)|duplicate", re.IGNORECASE
)


class AppserviceRegistrationError(Exception):
    """Enrolment could not be completed. The message is operator-facing."""


class BootstrapUserInUse(AppserviceRegistrationError):
    """The bootstrap user already exists, so registering it again failed."""


class PasswordLoginDisabled(AppserviceRegistrationError):
    """The homeserver offers no password login, so the bootstrap admin cannot
    sign in (Tuwunel's `login_with_password = false`)."""


class AppservicePingError(AppserviceRegistrationError):
    """The homeserver answered the ping with an error."""

    def __init__(self, message, errcode="", appservice_status=None):
        super().__init__(message)
        self.errcode = errcode
        self.appservice_status = appservice_status

    @property
    def refused_by_appservice(self) -> bool:
        """Whether whatever answers at the registered URL turned the ping away.

        Tuwunel reports the appservice's own answer as M_BAD_STATUS with its
        `status`. A 4xx is Waldur rejecting the hs_token (403) or a URL that
        does not lead to Waldur's ping endpoint (404, 405), which a new
        registration fixes. A 5xx is Waldur or its ingress being down, which it
        does not.
        """
        return (
            self.errcode == "M_BAD_STATUS"
            and isinstance(self.appservice_status, int)
            and 400 <= self.appservice_status < 500
        )


class AppservicePingTimeout(AppserviceRegistrationError):
    """The homeserver did not answer a ping in time.

    It holds the ping open while it calls Waldur, and Tuwunel 1.9.0 tries for
    35 s, longer than Waldur waits. So a timeout usually means the homeserver
    cannot reach Waldur, not that Waldur cannot reach the homeserver.
    """

    def __init__(self, message=""):
        super().__init__(
            message
            or "The homeserver did not answer the ping in time. It may still be "
            "trying to reach Waldur: check that the appservice URL is reachable "
            "from the homeserver."
        )


def build_registration(*, url, as_token, hs_token, sender_localpart, homeserver_domain):
    """Build the appservice registration a homeserver installs.

    Every registration Waldur hands out comes from here, so the Setup endpoint
    and the generate_appservice_registration command cannot declare different
    namespaces.

    Raises ValidationError when the localpart or domain would widen a namespace
    regex beyond Waldur's own identities.
    """
    serializers.validate_sender_localpart(sender_localpart)
    serializers.validate_homeserver_domain(homeserver_domain)

    return {
        "id": APPSERVICE_ID,
        "url": url,
        "as_token": as_token,
        "hs_token": hs_token,
        "sender_localpart": sender_localpart,
        "namespaces": {
            "users": [
                # Claim the bot identity exclusively so it cannot be
                # registered through normal client signup on the local
                # homeserver. The bot always lives on
                # MATRIX_HOMESERVER_DOMAIN, so the regex is scoped.
                {
                    "exclusive": True,
                    "regex": f"@{sender_localpart}:{homeserver_domain}",
                },
                {
                    "exclusive": False,
                    "regex": f"@.*:{homeserver_domain}",
                },
            ],
            "rooms": [],
            # An appservice may only create aliases inside its declared
            # namespace. Left empty, the homeserver answers M_EXCLUSIVE to
            # every alias request, room creation catches that and silently
            # creates the room without one, and the "Open in Matrix client"
            # link — which renders only when an alias exists — never
            # appears. Claimed exclusively for the same reason as the bot
            # user: nobody else should be able to take an address Waldur
            # hands out. `[^:]+` rather than `.*` so the wildcard cannot
            # swallow the separator and reach another domain, and the
            # domain is escaped so its dots are literal.
            "aliases": [
                {
                    "exclusive": True,
                    "regex": f"#{models.ROOM_ALIAS_PREFIX}[^:]+:{re.escape(homeserver_domain)}",
                },
            ],
        },
    }


def refuse_undecryptable(secrets_by_name: dict) -> None:
    """Refuse secrets that are the Constance backend's undecryptable stand-in.

    A secret stored under another FIELD_ENCRYPTION_KEY reads as a random
    stand-in rather than as empty. Enrolling with it would unregister a live
    appservice and register values nobody holds, or key the bootstrap admin's
    registration with a secret the homeserver does not share.
    """
    names = [
        name
        for name, value in secrets_by_name.items()
        if isinstance(value, str) and value.startswith(UNDECRYPTABLE_PREFIX)
    ]
    if names:
        raise AppserviceRegistrationError(
            f"{', '.join(names)} cannot be decrypted with any configured "
            "FIELD_ENCRYPTION_KEY; nothing was changed. Restore the key the "
            "setting was stored with, or set the setting again."
        )


def build_registration_from_config(url: str, as_token="", hs_token="") -> dict:
    """Build the descriptor from Constance, with optional token overrides."""
    as_token = as_token or config.MATRIX_APPSERVICE_AS_TOKEN
    hs_token = hs_token or config.MATRIX_APPSERVICE_HS_TOKEN
    refuse_undecryptable(
        {
            "MATRIX_APPSERVICE_AS_TOKEN": as_token,
            "MATRIX_APPSERVICE_HS_TOKEN": hs_token,
        }
    )
    sender_localpart = config.MATRIX_APPSERVICE_SENDER_LOCALPART or "waldur-bot"
    homeserver_domain = config.MATRIX_HOMESERVER_DOMAIN

    missing = []
    if not as_token:
        missing.append("as_token (pass --as-token or set MATRIX_APPSERVICE_AS_TOKEN)")
    if not hs_token:
        missing.append("hs_token (pass --hs-token or set MATRIX_APPSERVICE_HS_TOKEN)")
    if not homeserver_domain:
        missing.append("MATRIX_HOMESERVER_DOMAIN (required for users namespace regex)")
    if missing:
        raise AppserviceRegistrationError(
            "Missing prerequisites: " + "; ".join(missing)
        )

    try:
        return build_registration(
            url=url.rstrip("/"),
            as_token=as_token,
            hs_token=hs_token,
            sender_localpart=sender_localpart,
            homeserver_domain=homeserver_domain,
        )
    except ValidationError as exc:
        raise AppserviceRegistrationError(str(exc.detail))


# Homeserver messages quote what they failed on: Tuwunel names the URL it could
# not call, hs_token query included, and an admin bot may echo the descriptor
# as YAML or JSON, possibly escaped inside another JSON string. These messages
# reach deployment logs and the diagnostics page.
_TOKEN_PATTERNS = (
    (re.compile(r"(access_token=)[^&\s\"'\\]+"), r"\1<redacted>"),
    # A quoted value ends at its quote, so the rest of the JSON survives; an
    # unquoted one runs to the next whitespace.
    (
        re.compile(
            r"""(\b(?:as|hs)_token\\?["']?\s*[:=]\s*(\\?["'])?)(?(2)[^"'\\]+|\S+)"""
        ),
        r"\1<redacted>",
    ),
)


def _redact(text: str) -> str:
    for pattern, replacement in _TOKEN_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _payload(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _errcode(response: httpx.Response) -> str:
    return _payload(response).get("errcode") or ""


def _describe_response(response: httpx.Response) -> str:
    payload = _payload(response)
    if payload:
        errcode = payload.get("errcode") or ""
        # Tuwunel repeats the errcode at the start of the message.
        message = (payload.get("error") or "").removeprefix(f"{errcode}: ")
        detail = " ".join(part for part in (errcode, message) if part)
    else:
        detail = response.text[:200]
    return _redact(f"HTTP {response.status_code} {detail}".strip())


def _describe_status(response: httpx.Response) -> str:
    """Status and errcode only, for answers to requests carrying a password.

    Tuwunel 1.9.0 quotes the whole request body, password included, when it
    refuses a login type it does not offer.
    """
    return f"HTTP {response.status_code} {_errcode(response)}".strip()


def _describe_exception(exc: Exception) -> str:
    return _redact(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)


def _request(
    client: httpx.Client, method: str, url: str, context: str, **kwargs
) -> httpx.Response:
    """Send a request; a transport failure becomes an operator-facing error."""
    try:
        return client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise AppserviceRegistrationError(
            f"{context}: could not reach the homeserver ({_describe_exception(exc)})"
        ) from exc


def _raise_for_matrix_error(response: httpx.Response, context: str):
    if response.is_success:
        return
    raise AppserviceRegistrationError(f"{context}: {_describe_response(response)}")


def register_bootstrap_admin(
    client: httpx.Client, shared_secret: str, localpart: str, password: str
) -> str:
    """Create the bootstrap user as a homeserver admin and return its token.

    Ordinary sign-up makes an admin of the first account at most, and only
    with grant_admin_to_first_user, which the deployments turn off. It is also
    refused for a name on forbidden_usernames, where the deployments put this
    one. The shared-secret admin API is subject to neither.

    The password is the deployment's, never a throwaway: registering an
    existing user fails, so every later run has to sign in with it.
    """
    context = (
        f"Could not create the bootstrap admin @{localpart} through the "
        "homeserver's shared-secret registration"
    )
    response = _request(client, "GET", SHARED_SECRET_REGISTER_PATH, context)
    if not response.is_success:
        raise AppserviceRegistrationError(
            f"{context}: {_describe_response(response)}. {SHARED_SECRET_HINT} "
            "MATRIX_HOMESERVER_URL must reach its /_synapse/admin API."
        )
    nonce = _payload(response).get("nonce") or ""
    mac = hmac.new(
        shared_secret.encode(),
        b"\0".join(part.encode() for part in (nonce, localpart, password, "admin")),
        hashlib.sha1,
    ).hexdigest()
    response = _request(
        client,
        "POST",
        SHARED_SECRET_REGISTER_PATH,
        context,
        json={
            "nonce": nonce,
            "username": localpart,
            "password": password,
            "admin": True,
            "mac": mac,
        },
    )
    if _errcode(response) == "M_USER_IN_USE":
        raise BootstrapUserInUse(f"Bootstrap user @{localpart} already exists.")
    if not response.is_success:
        # A wrong key is a 403; the MAC is checked before the name.
        hint = f" {SHARED_SECRET_HINT}" if response.status_code == 403 else ""
        raise AppserviceRegistrationError(
            f"{context}: {_describe_status(response)}.{hint}"
        )
    access_token = _payload(response).get("access_token")
    if not access_token:
        raise AppserviceRegistrationError(
            f"{context}: the homeserver created it but returned no access token."
        )
    return access_token


def login_bootstrap_user(client: httpx.Client, localpart: str, password: str):
    """Log the bootstrap user in and return its access token, or None.

    None covers both "no such user" and "wrong password": the homeserver
    answers M_FORBIDDEN to each. A refused password login raises
    PasswordLoginDisabled instead, so it is not blamed on the password.
    """
    context = "Bootstrap user login failed"
    # Asked first, so the password never goes to a homeserver that refuses a
    # password login: Tuwunel 1.9.0 quotes the refused body back, password
    # included.
    response = _request(client, "GET", "/_matrix/client/v3/login", context)
    flows = _payload(response).get("flows") if response.is_success else None
    if isinstance(flows, list) and not any(
        isinstance(flow, dict) and flow.get("type") == "m.login.password"
        for flow in flows
    ):
        raise PasswordLoginDisabled(
            "Password login is disabled on the homeserver (login_with_password "
            f"= false), so the command cannot sign in as @{localpart}. Set "
            "MATRIX_ADMIN_TOKEN to a homeserver admin's access token."
        )
    response = _request(
        client,
        "POST",
        "/_matrix/client/v3/login",
        context,
        json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": localpart},
            "password": password,
            # Signed out at the end of the run, which removes the device again.
            "device_id": _bootstrap_device_id(),
            "initial_device_display_name": "Waldur bootstrap",
        },
    )
    if response.status_code == 403:
        return None
    if not response.is_success:
        raise AppserviceRegistrationError(f"{context}: {_describe_status(response)}")
    return _payload(response).get("access_token")


def _sign_out(client: httpx.Client, access_token: str):
    """Sign a bootstrap admin session out; its token would never expire."""
    context = "could not sign the bootstrap admin out, so its session stays valid"
    try:
        response = _request(
            client,
            "POST",
            "/_matrix/client/v3/logout",
            context,
            headers={"Authorization": f"Bearer {access_token}"},
            json={},
        )
    except AppserviceRegistrationError as exc:
        logger.warning("Matrix appservice enrolment %s", exc)
        return
    # Already ended, e.g. an admin removed the device: nothing left to sign out.
    if not response.is_success and _errcode(response) != "M_UNKNOWN_TOKEN":
        logger.warning(
            "Matrix appservice enrolment %s: %s", context, _describe_response(response)
        )


@contextlib.contextmanager
def _bootstrap_sessions(client: httpx.Client):
    """Collect the bootstrap admin sessions enrolment opens; sign each out."""
    sessions: list[str] = []
    try:
        yield sessions
    finally:
        for access_token in sessions:
            _sign_out(client, access_token)


def find_admin_room(
    client: httpx.Client, access_token: str, homeserver_domain: str, user_id: str
) -> str:
    """Locate the homeserver's admin room for the authenticated user."""
    headers = {"Authorization": f"Bearer {access_token}"}

    alias = f"#{ADMIN_ROOM_ALIAS_LOCALPART}:{homeserver_domain}"
    response = _request(
        client,
        "GET",
        f"/_matrix/client/v3/directory/room/{quote(alias, safe='')}",
        "Could not resolve the admin room",
        headers=headers,
    )
    alias_room = _payload(response).get("room_id") if response.is_success else None

    context = "Could not list joined rooms"
    response = _request(
        client, "GET", "/_matrix/client/v3/joined_rooms", context, headers=headers
    )
    _raise_for_matrix_error(response, context)
    joined = _payload(response).get("joined_rooms") or []

    not_admin = (
        "it is not a homeserver admin and cannot register appservices. Make it "
        f"one from the admin room (!admin users make-user-admin {user_id}), or "
        "set MATRIX_ADMIN_TOKEN to an admin's access token."
    )
    if alias_room:
        # Anyone can resolve the alias; only the admin is in the room.
        if alias_room in joined:
            return alias_room
        raise AppserviceRegistrationError(
            f"{user_id} is not in the admin room ({alias}), so {not_admin}"
        )
    if len(joined) == 1:
        return joined[0]
    if not joined:
        raise AppserviceRegistrationError(
            f"{user_id} has joined no rooms, so {not_admin}"
        )
    raise AppserviceRegistrationError(
        f"Cannot identify the admin room: {user_id} is in {len(joined)} rooms and "
        f"{alias} did not resolve. Pass --admin-room explicitly."
    )


def send_admin_command(
    client: httpx.Client, access_token: str, room_id: str, body: str
) -> str:
    """Send a message to the admin room and return the sent event id."""
    txn_id = f"waldur-{secrets.token_hex(8)}"
    context = "Could not send the admin-room command"
    response = _request(
        client,
        "PUT",
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
        f"/send/m.room.message/{txn_id}",
        context,
        headers={"Authorization": f"Bearer {access_token}"},
        json={"msgtype": "m.text", "body": body},
    )
    _raise_for_matrix_error(response, context)
    return _payload(response).get("event_id", "")


def _replied_to(event: dict) -> str:
    relates_to = (event.get("content") or {}).get("m.relates_to") or {}
    return (relates_to.get("m.in_reply_to") or {}).get("event_id") or ""


def wait_for_admin_reply(
    client: httpx.Client,
    access_token: str,
    room_id: str,
    sender: str,
    sent_event_id: str,
    attempts=None,
    interval=None,
) -> str:
    """Poll the admin room for the first reply that follows our command.

    Returns the reply body. The caller classifies it — the admin bot answers
    both success and failure as an ordinary message, so an HTTP 200 on the
    send says nothing about whether registration worked.

    The scan is anchored on `sent_event_id` rather than simply taking the
    newest message from another sender: the admin room already contains the
    homeserver's own welcome messages, and reading one of those before the
    reply arrives would report a bogus failure.
    """
    # Resolved at call time rather than as default arguments so tests (and any
    # caller tuning the wait) can patch the module constants.
    attempts = REPLY_POLL_ATTEMPTS if attempts is None else attempts
    interval = REPLY_POLL_INTERVAL if interval is None else interval

    headers = {"Authorization": f"Bearer {access_token}"}
    for attempt in range(attempts):
        if attempt:
            time.sleep(interval)
        # Raised at once rather than retried: a homeserver that has gone away
        # would otherwise cost every remaining attempt its connect timeout.
        response = _request(
            client,
            "GET",
            f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages",
            "Sent the admin-room command, but lost the homeserver while waiting "
            "for the admin bot's reply, so it may or may not have taken effect",
            headers=headers,
            params={"dir": "b", "limit": 20},
        )
        if not response.is_success:
            continue

        # dir=b returns newest first, so everything before our own command
        # event is a candidate reply.
        newer = []
        anchored = False
        for event in _payload(response).get("chunk") or []:
            if sent_event_id and event.get("event_id") == sent_event_id:
                anchored = True
                break
            newer.append(event)
        if sent_event_id and not anchored:
            # Our command has scrolled out of the window, or has not landed
            # yet — either way this page cannot be trusted. Try again.
            continue

        replies = [
            event
            for event in reversed(newer)
            if event.get("type") == "m.room.message"
            and event.get("sender") != sender
            and (event.get("content") or {}).get("body")
        ]
        # Tuwunel's admin bot marks the command each reply answers. Once any
        # reply carries that mark, only the one naming our command counts: the
        # others answer a concurrent run. Without marks, the first one wins.
        threaded = [event for event in replies if _replied_to(event)]
        if threaded and sent_event_id:
            replies = [
                event for event in threaded if _replied_to(event) == sent_event_id
            ]
        if replies:
            return replies[0]["content"]["body"]
    raise AppserviceRegistrationError(
        "The homeserver's admin bot did not reply within "
        f"{int(attempts * interval)}s, so the command may or may not have taken "
        "effect — check the admin room manually before retrying."
    )


class TokenState(enum.Enum):
    """What the homeserver says about Waldur's as_token."""

    LIVE = "live"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


# The only answers that say for certain the homeserver will not let this token
# act as Waldur's bot. Tuwunel answers M_EXCLUSIVE when the token is known but
# its namespace does not cover the bot, as after a sender_localpart change.
_REJECTING_ANSWERS = {(401, "M_UNKNOWN_TOKEN"), (403, "M_EXCLUSIVE")}


def bot_user_id_for(registration: dict, homeserver_domain: str) -> str:
    return f"@{registration['sender_localpart']}:{homeserver_domain}"


def appservice_token_state(
    client: httpx.Client, registration: dict, homeserver_domain: str
) -> tuple[TokenState, str]:
    """Report whether the homeserver honours this exact as_token, with a reason.

    Asking the homeserver to identify the bot user while presenting the
    appservice token answers the only question that matters — can Waldur act as
    its bot right now — without needing any admin access at all. That makes the
    whole enrolment re-runnable, which the deployment paths depend on: both the
    Helm wiring Job and `docker compose up` execute on every upgrade.

    REJECTED is reserved for a definite answer, because the caller unregisters
    the existing appservice on the strength of it. A timeout, a proxy's 5xx or
    rate limiting says nothing about the registration, so those are UNKNOWN.
    """
    user_id = bot_user_id_for(registration, homeserver_domain)
    try:
        response = client.get(
            "/_matrix/client/v3/account/whoami",
            params={"user_id": user_id},
            headers={"Authorization": f"Bearer {registration['as_token']}"},
        )
    except httpx.HTTPError as exc:
        return TokenState.UNKNOWN, _describe_exception(exc)

    if response.status_code == 200:
        identified = _payload(response).get("user_id")
        if not identified:
            return TokenState.UNKNOWN, "HTTP 200 without a whoami answer"
        if identified == user_id:
            return TokenState.LIVE, ""
        # The token works, but for somebody else: not this bot's.
        return TokenState.REJECTED, f"the token identifies {identified}"

    if (response.status_code, _errcode(response)) in _REJECTING_ANSWERS:
        return TokenState.REJECTED, _describe_response(response)
    return TokenState.UNKNOWN, _describe_response(response)


def ping_appservice(homeserver_url: str, as_token: str, timeout=HTTP_TIMEOUT) -> int:
    """Have the homeserver call Waldur's ping endpoint; return the round trip in ms.

    The homeserver answers by calling Waldur with the hs_token (MSC2659), so a
    success proves both tokens and that the homeserver can reach Waldur at the
    registered URL, which nothing else checks.
    """
    try:
        response = httpx.post(
            f"{homeserver_url.rstrip('/')}/_matrix/client/v1/appservice/"
            f"{APPSERVICE_ID}/ping",
            json={"transaction_id": f"waldur-{secrets.token_hex(8)}"},
            headers={"Authorization": f"Bearer {as_token}"},
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise AppservicePingTimeout() from exc
    except httpx.HTTPError as exc:
        raise AppserviceRegistrationError(
            f"Could not reach the homeserver: {_describe_exception(exc)}"
        ) from exc
    if not response.is_success:
        raise AppservicePingError(
            f"Appservice ping failed: {_describe_response(response)}",
            errcode=_errcode(response),
            appservice_status=_payload(response).get("status"),
        )
    return _payload(response).get("duration_ms")


SHOW_CONFIG_COMMAND = f"!admin appservices show-config {APPSERVICE_ID}"
_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_COMPARED_FIELDS = ("url", "as_token", "hs_token", "sender_localpart")
_NAMESPACE_KINDS = ("users", "aliases", "rooms")


def parse_shown_registration(reply: str) -> dict:
    """Read the registration out of the admin bot's show-config reply.

    The registration carries both tokens, so an error never quotes it, only
    a reply that turned out not to be one ("Appservice does not exist.").
    """
    match = _CODE_BLOCK.search(reply)
    if match:
        try:
            shown = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            raise AppserviceRegistrationError(
                f"could not parse the homeserver's registration of '{APPSERVICE_ID}'"
            )
        if isinstance(shown, dict):
            return shown
    text = (match.group(1) if match else reply).strip()
    raise AppserviceRegistrationError(
        f"the homeserver did not show its registration of '{APPSERVICE_ID}': "
        f"{_redact(text[:200])}"
    )


def registration_differences(shown: dict, expected: dict) -> list[str]:
    """Name the fields in which the homeserver's registration differs from Waldur's.

    Only the fields Waldur sets are compared: Tuwunel adds keys of its own and
    leaves out empty namespace lists, so a missing list counts as empty. Names
    only, never values, since two of the fields are tokens.
    """
    differences = [
        field
        for field in _COMPARED_FIELDS
        if _comparable(field, shown.get(field))
        != _comparable(field, expected.get(field))
    ]
    shown_namespaces = shown.get("namespaces")
    if not isinstance(shown_namespaces, dict):
        shown_namespaces = {}
    for kind in _NAMESPACE_KINDS:
        if _namespace(shown_namespaces.get(kind)) != _namespace(
            expected["namespaces"].get(kind)
        ):
            differences.append(f"namespaces.{kind}")
    return differences


def _comparable(field: str, value) -> str:
    value = "" if value is None else str(value)
    return value.rstrip("/") if field == "url" else value


def _namespace(entries) -> list:
    return sorted(
        (bool(entry.get("exclusive")), str(entry.get("regex")))
        for entry in entries or []
        if isinstance(entry, dict)
    )


def _ping_error(homeserver_url: str, as_token: str):
    """Ping the appservice; return the failure, or None when it answered."""
    try:
        ping_appservice(homeserver_url, as_token)
    except AppserviceRegistrationError as exc:
        return exc
    return None


def _refused_by_appservice(ping_error) -> bool:
    return (
        isinstance(ping_error, AppservicePingError) and ping_error.refused_by_appservice
    )


@dataclasses.dataclass
class Enrolment:
    """What register_appservice_on_homeserver did, and how the final ping went.

    A failed ping is reported rather than raised: on a fresh install the API
    may still be starting when the registration runs.
    """

    status: str
    ping_error: AppserviceRegistrationError | None = None
    # Why a live registration could not be compared with Waldur's descriptor.
    unchecked: str = ""
    bot_made_admin: bool = False
    # Why the bot is still not a homeserver admin.
    bot_admin_error: str = ""


def _replace_registration(admin_command, register_body: str) -> str:
    """Swap the homeserver's registration of the id for Waldur's.

    Registering the id again keeps the old registration (Tuwunel answers
    "Duplicate id"), so the old one has to go first. The deployment owns the
    tokens, so its copy wins.

    From the unregister on, the homeserver may hold no registration at all, so
    every failure says so. A re-run recovers either way: with nothing
    registered the token is rejected and the command registers afresh.
    """
    try:
        reply = admin_command(f"!admin appservices unregister {APPSERVICE_ID}")
    except AppserviceRegistrationError as exc:
        raise AppserviceRegistrationError(
            f"{exc}. The old registration may already be removed; re-running "
            "the command registers the appservice again either way."
        ) from exc
    if not UNREGISTERED_PATTERN.search(reply):
        raise AppserviceRegistrationError(
            f"The homeserver holds a registration for '{APPSERVICE_ID}' that "
            "differs from Waldur's and refused to unregister it: "
            f"{_redact(reply.strip()[:500])}"
        )

    gone = (
        "The old registration was removed, so the homeserver delivers no events "
        "to Waldur until the appservice is registered again"
    )
    try:
        reply = admin_command(register_body)
    except AppserviceRegistrationError as exc:
        raise AppserviceRegistrationError(
            f"{gone}, but registering it failed: {exc}. Re-running the command "
            "registers it again."
        ) from exc
    if not SUCCESS_PATTERN.search(reply):
        raise AppserviceRegistrationError(
            f"{gone}, but the homeserver rejected the new registration: "
            f"{_redact(reply.strip()[:500])}. Fix the cause and re-run the command."
        )
    return "replaced"


def _admin_user(
    client: httpx.Client,
    homeserver_domain: str,
    admin_token: str,
    registration_token: str,
    bootstrap_localpart: str,
    bootstrap_password: str,
    create: bool,
) -> tuple[str, str]:
    """Return the access token and user id to send admin commands as.

    With `create` false nothing is registered. That is for looking only, and
    a bootstrap user registered just to look would be a stray account on any
    homeserver that already has its admin.
    """
    if admin_token:
        context = "Could not identify the admin user"
        response = _request(
            client,
            "GET",
            "/_matrix/client/v3/account/whoami",
            context,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        _raise_for_matrix_error(response, context)
        return admin_token, _payload(response).get("user_id", "")

    if not bootstrap_password:
        raise AppserviceRegistrationError(
            "neither MATRIX_ADMIN_TOKEN nor MATRIX_BOOTSTRAP_PASSWORD is set"
        )
    user_id = f"@{bootstrap_localpart}:{homeserver_domain}"
    if create and registration_token:
        try:
            access_token = register_bootstrap_admin(
                client, registration_token, bootstrap_localpart, bootstrap_password
            )
            return access_token, user_id
        except BootstrapUserInUse:
            # An earlier run created it, or a concurrent first install did,
            # with the same password.
            pass
    access_token = login_bootstrap_user(client, bootstrap_localpart, bootstrap_password)
    if access_token is not None:
        return access_token, user_id
    if not create:
        raise AppserviceRegistrationError(
            f"could not log in as @{bootstrap_localpart} with MATRIX_BOOTSTRAP_PASSWORD"
        )
    if not registration_token:
        raise AppserviceRegistrationError(
            f"Could not log in as @{bootstrap_localpart} with "
            "MATRIX_BOOTSTRAP_PASSWORD, and there is no registration "
            "token to create it with."
        )
    raise BootstrapUserInUse(
        f"Bootstrap user @{bootstrap_localpart} exists, but the homeserver rejects "
        "MATRIX_BOOTSTRAP_PASSWORD for it. It must be the password the "
        "deployment set when the user was first registered; otherwise set "
        "MATRIX_ADMIN_TOKEN to an admin's access token."
    )


@dataclasses.dataclass
class _AdminRoom:
    client: httpx.Client
    # Sentry sends frame locals as their repr, and this one is an admin's.
    access_token: str = dataclasses.field(repr=False)
    user_id: str
    room_id: str

    def command(self, body: str) -> str:
        """Send an admin command and return the admin bot's reply."""
        sent_event_id = send_admin_command(
            self.client, self.access_token, self.room_id, body
        )
        return wait_for_admin_reply(
            self.client, self.access_token, self.room_id, self.user_id, sent_event_id
        )


def _user_admin_path(user_id: str) -> str:
    # safe="": a "/" in a localpart would otherwise address another endpoint.
    return f"/_synapse/admin/v2/users/{quote(user_id, safe='')}"


def bot_admin_state(
    client: httpx.Client, registration: dict, homeserver_domain: str
) -> tuple[bool | None, str]:
    """Whether the homeserver's admin API answers the bot, with a reason when unsure.

    The bot asks about itself, so the homeserver turning it away (403
    M_FORBIDDEN) answers it too. None says nothing about it: a proxy that
    blocks the API or answers with its own page, a homeserver without one, or
    no answer at all.
    """
    bot_user_id = bot_user_id_for(registration, homeserver_domain)
    try:
        response = client.get(
            _user_admin_path(bot_user_id),
            headers={"Authorization": f"Bearer {registration['as_token']}"},
        )
    except httpx.HTTPError as exc:
        return None, _describe_exception(exc)
    if response.status_code == 403 and _errcode(response) == "M_FORBIDDEN":
        return False, ""
    admin = _payload(response).get("admin")
    if response.status_code == 200 and isinstance(admin, bool):
        return admin, ""
    return None, _describe_response(response)


def make_bot_admin(
    client: httpx.Client,
    registration: dict,
    homeserver_domain: str,
    admin: _AdminRoom | None,
) -> tuple[bool, str]:
    """Make the bot a homeserver admin unless it is one.

    Returns whether it was made one now, and why it still is not one.
    """
    bot_user_id = bot_user_id_for(registration, homeserver_domain)
    is_admin, detail = bot_admin_state(client, registration, homeserver_domain)
    if is_admin:
        return False, ""
    if is_admin is None:
        problem = (
            f"Waldur could not check whether {bot_user_id} is a homeserver "
            f"admin ({detail})"
        )
    else:
        problem = f"{bot_user_id} is not a homeserver admin"
    if admin is None:
        return False, f"{problem}, and this run could not act as one"

    try:
        response = _request(
            client,
            "PUT",
            _user_admin_path(bot_user_id),
            "making it one failed",
            headers={"Authorization": f"Bearer {admin.access_token}"},
            json={"admin": True},
        )
    except AppserviceRegistrationError as exc:
        failure = str(exc)
    else:
        # The homeserver answers with the account as it now stands.
        if response.is_success and _payload(response).get("admin") is True:
            logger.info("Made %s a homeserver admin", bot_user_id)
            return True, ""
        # Named, so a refusal reads as being about the admin, not the bot.
        failure = (
            f"{admin.user_id} could not make it one: {_describe_response(response)}"
        )
    # The command appends what to do about it.
    return False, f"{problem}; {failure}".rstrip(".")


def _require_admin_credentials(admin_token: str, bootstrap_password: str, why=""):
    if admin_token or bootstrap_password:
        return
    raise AppserviceRegistrationError(
        f"{why}Registering the appservice needs a homeserver admin: set "
        "MATRIX_ADMIN_TOKEN to an admin's access token, or "
        "MATRIX_BOOTSTRAP_PASSWORD so Waldur can create its bootstrap admin "
        "and sign in as it on later runs. Without a known password no "
        "bootstrap admin is created, since nobody could sign in as it again. "
        "With password login off on the homeserver, later runs need "
        "MATRIX_ADMIN_TOKEN."
    )


def _register(admin_command, register_body: str, token_state) -> str:
    reply = admin_command(register_body)
    if SUCCESS_PATTERN.search(reply):
        return "registered"
    if not ALREADY_REGISTERED_PATTERN.search(reply):
        raise AppserviceRegistrationError(
            "Homeserver rejected the appservice registration: "
            f"{_redact(reply.strip()[:500])}"
        )
    if _refuse_unless_definite(*token_state()) is TokenState.LIVE:
        # Another run registered these very tokens in the meantime.
        return "already-registered"
    return _replace_registration(admin_command, register_body)


def _refuse_unless_definite(state: TokenState, detail: str) -> TokenState:
    if state is TokenState.UNKNOWN:
        raise AppserviceRegistrationError(
            "Waldur could not determine whether the homeserver accepts the token "
            f"({detail}); nothing was changed. Re-run the command once the "
            "homeserver answers."
        )
    return state


def register_appservice_on_homeserver(
    homeserver_url: str,
    homeserver_domain: str,
    registration: dict,
    registration_token="",
    admin_token="",
    admin_room="",
    bootstrap_localpart=BOOTSTRAP_LOCALPART,
    bootstrap_password="",
) -> Enrolment:
    """Enrol the appservice on a Conduit-family homeserver, then ping it.

    The status is ``"already-registered"`` when the homeserver already holds
    Waldur's registration and reaches Waldur with it, ``"registered"`` on a
    fresh registration, and ``"replaced"`` when a registration with other
    tokens, another URL or other namespaces had to be swapped for this one.
    """
    # The descriptor carries live tokens; it is only ever serialised into the
    # request body, never into a log line or the command's stdout.
    descriptor_yaml = yaml.dump(registration, default_flow_style=False)
    register_body = f"!admin appservices register\n```yaml\n{descriptor_yaml}```"

    with (
        httpx.Client(
            base_url=homeserver_url.rstrip("/"), timeout=HTTP_TIMEOUT
        ) as client,
        _bootstrap_sessions(client) as bootstrap_sessions,
    ):

        def token_state():
            return appservice_token_state(client, registration, homeserver_domain)

        def grant_bot_admin(admin):
            return make_bot_admin(client, registration, homeserver_domain, admin)

        def open_admin_room(create):
            access_token, user_id = _admin_user(
                client,
                homeserver_domain,
                admin_token,
                registration_token,
                bootstrap_localpart,
                bootstrap_password,
                create,
            )
            if not admin_token:
                # Before anything else can fail, so the session is signed out.
                bootstrap_sessions.append(access_token)
            room_id = admin_room or find_admin_room(
                client, access_token, homeserver_domain, user_id
            )
            return _AdminRoom(client, access_token, user_id, room_id)

        live = _refuse_unless_definite(*token_state()) is TokenState.LIVE
        if live:
            # The as_token proves only that the bot works. The homeserver may
            # still hold an old hs_token, another URL or namespaces from before
            # the alias claim, and only its own copy of the registration shows
            # that.
            ping_error = _ping_error(homeserver_url, registration["as_token"])
            refused = _refused_by_appservice(ping_error)
            admin = None
            try:
                admin = open_admin_room(create=False)
                differences = registration_differences(
                    parse_shown_registration(admin.command(SHOW_CONFIG_COMMAND)),
                    registration,
                )
            except AppserviceRegistrationError as exc:
                if not refused:
                    # The bot works and Waldur answers. Not being able to check
                    # the rest is worth a warning, not a failed deploy.
                    logger.warning(
                        "Matrix appservice %s is live; could not compare it with "
                        "the expected descriptor: %s",
                        APPSERVICE_ID,
                        exc,
                    )
                    granted, bot_admin_error = grant_bot_admin(admin)
                    return Enrolment(
                        "already-registered",
                        ping_error,
                        str(exc),
                        bot_made_admin=granted,
                        bot_admin_error=bot_admin_error,
                    )
                # Waldur turns the homeserver's calls away, so the registration
                # has to be replaced even without seeing it.
                logger.warning(
                    "Matrix appservice %s is live, but the homeserver's ping of "
                    "Waldur failed (%s); replacing the registration",
                    APPSERVICE_ID,
                    ping_error,
                )
                _require_admin_credentials(
                    admin_token,
                    bootstrap_password,
                    f"Waldur turns the homeserver's ping away ({ping_error}), so "
                    "the registration must be replaced. ",
                )
                admin = admin or open_admin_room(create=True)
            else:
                if not differences and refused:
                    # Registering the very same descriptor again cannot help.
                    raise AppserviceRegistrationError(
                        "The homeserver's registration already matches Waldur's, "
                        "but its ping is turned away at "
                        f"{registration['url']}: {ping_error}. That URL does not "
                        "lead to this Waldur, or Waldur checks another hs_token "
                        "than the one registered. Nothing was changed."
                    )
                if not differences:
                    logger.info(
                        "Matrix appservice %s is already live on the homeserver",
                        APPSERVICE_ID,
                    )
                    granted, bot_admin_error = grant_bot_admin(admin)
                    return Enrolment(
                        "already-registered",
                        ping_error,
                        bot_made_admin=granted,
                        bot_admin_error=bot_admin_error,
                    )
                logger.warning(
                    "Matrix appservice %s is registered with a different %s; "
                    "replacing the registration",
                    APPSERVICE_ID,
                    ", ".join(differences),
                )
            status = _replace_registration(admin.command, register_body)
        else:
            _require_admin_credentials(admin_token, bootstrap_password)
            admin = open_admin_room(create=True)
            status = _register(admin.command, register_body, token_state)

        # The admin bot's reply is about the id, not the tokens. Only a
        # working as_token means Waldur can act as its bot.
        state, detail = token_state()
        if state is TokenState.UNKNOWN:
            raise AppserviceRegistrationError(
                f"The homeserver confirmed the registration, but Waldur could not "
                f"confirm that it accepts the as_token ({detail}). Re-run the "
                "command to check."
            )
        if state is TokenState.REJECTED:
            raise AppserviceRegistrationError(
                f"The homeserver has an appservice registered as '{APPSERVICE_ID}' "
                "but rejects the as_token Waldur holds, so the tokens in Waldur and "
                "on the homeserver differ. Chat stays broken until they match."
            )
        # After the token check: the probe acts as the bot, which works only
        # once the homeserver accepts the as_token.
        granted, bot_admin_error = grant_bot_admin(admin)

    ping_error = _ping_error(homeserver_url, registration["as_token"])
    if live and _refused_by_appservice(ping_error):
        # Replacing it changed nothing, so another run would only replace it
        # again. This needs the operator.
        raise AppserviceRegistrationError(
            "Replaced the registration, but the homeserver's ping is still "
            f"turned away at {registration['url']}: {ping_error}. That URL does "
            "not lead to this Waldur, or the hs_token registered (--hs-token) is "
            "not the one Waldur checks (MATRIX_APPSERVICE_HS_TOKEN). Events are "
            "not delivered until both match."
        )
    logger.info("Matrix appservice %s is %s on homeserver", APPSERVICE_ID, status)
    return Enrolment(
        status,
        ping_error,
        bot_made_admin=granted,
        bot_admin_error=bot_admin_error,
    )
