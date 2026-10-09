"""Fixes to matrix-nio, applied by the Matrix bot before it opens a client.

Each patch fixes a bug in the pinned nio release and has two tests: one that the
patch fixes the bug, and a canary that the bug is still there in unpatched nio.
When a canary starts failing, upstream has fixed the bug and the patch can go.

:func:`apply` refuses any nio but the pinned one, so an upgrade forces a review
of every patch instead of silently patching code that has changed underneath.
"""

import functools
import importlib.metadata
import threading

import vodozemac
from nio import schemas
from nio.crypto import olm_machine, sessions

PINNED_VERSION = "0.26.0"

# What each patch replaced, so the tests can run nio unpatched.
originals = {}

_lock = threading.Lock()
_applied = False


class UnsupportedNioVersion(RuntimeError):
    pass


def _patch_consumed_one_time_key():
    """A redelivered pre-key message must not crash the bot's sync.

    When an Olm pre-key message arrives again after its one-time key was used,
    vodozemac raises ``SessionCreationException``. nio does not catch it, so it
    escapes sync processing; the same message comes back on the next sync and the
    bot crash-loops. Re-raised as ``OlmDecryptionException``, it reaches the
    handler nio already has, which marks the sender's device for unwedging so the
    two sides start a fresh session.

    nio 0.26 creates the session lazily, on its first decrypt, so the exception
    comes out of ``InboundSession.decrypt``.
    """
    original = sessions.InboundSession.decrypt
    originals["InboundSession.decrypt"] = original

    @functools.wraps(original)
    def decrypt(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except vodozemac.SessionCreationException as error:
            raise vodozemac.OlmDecryptionException(str(error)) from error

    sessions.InboundSession.decrypt = decrypt


# Room ids of room version 12 have no server part ("!opaque"), and Tuwunel
# creates v12 rooms by default.
ROOM_ID_PATTERN = r"^!.+$"


def room_patterns(schema):
    """The room-id-keyed sections of the sync schema: invite, join and leave."""
    rooms = schema["properties"]["rooms"]["properties"]
    return [rooms[kind]["patternProperties"] for kind in ("invite", "join", "leave")]


def rename_room_pattern(schema, old, new):
    for patterns in room_patterns(schema):
        if old in patterns:
            patterns[new] = patterns.pop(old)


def _patch_room_version_12_ids():
    """Sync responses must parse for rooms whose id has no server part.

    nio's sync schema keys each room by ``^!.+:.+$``, written before room version
    12 dropped the server part from room ids. A v12 room therefore matches no
    pattern, so none of the schema's defaults are filled in for it, and a section
    the spec lets the server leave out breaks parsing: a left room without
    ``state`` (as Tuwunel sends an incremental sync from 1.9.1) or an invited room
    without ``invite_state``. The whole response fails, and every retry from the
    same ``since`` token fails again, so the bot would stop syncing for good the
    first time it leaves a room.

    The schema dict is patched in place: nio's response parser holds a reference
    to it from import time.
    """
    originals["RoomRegex"] = schemas.RoomRegex
    rename_room_pattern(schemas.Schemas.sync, schemas.RoomRegex, ROOM_ID_PATTERN)


def _patch_save_account_once_the_one_time_key_is_used():
    """A used one-time key must not come back after a restart.

    nio saves the account when it creates an inbound session, but nio 0.26
    creates the vodozemac session lazily, on its first decrypt, which is when the
    one-time key is used up. The saved account still holds the key until the next
    key upload saves it again. A restart in between brings the key back, and a
    replayed pre-key message could then open a second session. The account is
    saved again once the decrypt that created a session is done.
    """
    original_create = olm_machine.Olm._create_inbound_session
    original_decrypt = olm_machine.Olm.decrypt
    originals["Olm._create_inbound_session"] = original_create
    originals["Olm.decrypt"] = original_decrypt

    @functools.wraps(original_create)
    def _create_inbound_session(self, *args, **kwargs):
        session = original_create(self, *args, **kwargs)
        self._waldur_account_changed = True
        return session

    @functools.wraps(original_decrypt)
    def decrypt(self, *args, **kwargs):
        try:
            return original_decrypt(self, *args, **kwargs)
        finally:
            if getattr(self, "_waldur_account_changed", False):
                self._waldur_account_changed = False
                self.save_account()

    olm_machine.Olm._create_inbound_session = _create_inbound_session
    olm_machine.Olm.decrypt = decrypt


def apply():
    """Patch nio. Safe to call more than once; refuses an unexpected nio version."""
    global _applied
    with _lock:
        if _applied:
            return
        version = importlib.metadata.version("matrix-nio")
        if version != PINNED_VERSION:
            raise UnsupportedNioVersion(
                f"The Matrix bot's fixes are written for matrix-nio {PINNED_VERSION}, "
                f"but {version} is installed. Review each fix in nio_compat before "
                "changing the pinned version."
            )
        _patch_consumed_one_time_key()
        _patch_room_version_12_ids()
        _patch_save_account_once_the_one_time_key_is_used()
        _applied = True
