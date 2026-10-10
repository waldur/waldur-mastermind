"""The bot's writer of room history into members' key backups.

Runs inside the bot process, which alone holds the rooms' Megolm sessions. For
each membership due a fill (see :mod:`history_backfill`) it:

1. reads the member's current backup through the appservice, as the member;
2. writes only into a backup it can tie to the member (see
   :mod:`crypto.key_backup`), and drops the fill otherwise;
3. checks, right before writing a room's keys, that Waldur still counts the user
   a member and that the homeserver has them joined;
4. writes the sessions the bot holds for the room, from their first known index,
   only those sent by a device of a past or present member (or the bot's own),
   skipping those the backup already holds, so a member's own backed-up keys are
   never replaced.

It also looks for members whose backup version changed (a reset or a new
backup made in another client) and asks for a fill into the new one.

Nothing here logs key material: only room and user ids and counts.
"""

import logging
import time
from urllib.parse import quote

import httpx

from waldur_mastermind.matrix_chat import history_backfill
from waldur_mastermind.matrix_chat.crypto import cross_signing, key_backup

logger = logging.getLogger(__name__)

# Users whose due fills are written per round, so one round stays short.
USERS_PER_ROUND = 10
# Sessions written per request.
SESSIONS_PER_REQUEST = 100
POLL_SECONDS = 5
# How often a pass compares every member's backup version with the one last
# handled. A pass checks this many users per round, between rounds of fills.
VERSION_CHECK_SECONDS = 6 * 3600
VERSION_CHECK_BATCH = 50


class FillError(RuntimeError):
    """The homeserver could not answer now; the fill is tried again later."""


def _path(*parts):
    return "/".join(quote(part, safe="") for part in parts)


class HistoryFiller:
    """Fills key backups for ``bot``. ``db`` runs a function in Django's database
    thread, ``renew_lease`` stops the round once the bot lost its lease, and
    ``transient_errors`` are the failures a fill survives to be retried."""

    def __init__(self, bot, db, renew_lease, transient_errors):
        self.bot = bot
        self.db = db
        self.renew_lease = renew_lease
        self.transient_errors = transient_errors
        self._http = None
        # The version check pass under way: the last user checked, or None
        # between passes; and when the last pass started.
        self._check_after = None
        self._check_started_at = None

    async def close(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # Homeserver calls.

    async def _request(self, method, path, token, params=None, json=None):
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30)
        response = await self._http.request(
            method,
            f"{self.bot.homeserver_url}/_matrix/client/v3/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        return response.status_code, data if isinstance(data, dict) else {}

    async def _as_user(self, method, path, matrix_user_id, params=None, json=None):
        """A call made as the member, through the appservice: no device, no token."""
        return await self._request(
            method,
            path,
            self.bot.as_token,
            params={**(params or {}), "user_id": matrix_user_id},
            json=json,
        )

    async def current_backup(self, matrix_user_id):
        """The member's current ``room_keys/version`` answer, or None if none."""
        status, data = await self._as_user("GET", "room_keys/version", matrix_user_id)
        if status == 404:
            return None
        if status != 200:
            raise FillError(f"Reading the key backup failed: {status}")
        return data

    async def _account_data(self, matrix_user_id, kind):
        status, data = await self._as_user(
            "GET",
            _path("user", matrix_user_id, "account_data", kind),
            matrix_user_id,
        )
        if status == 404:
            return None
        if status != 200:
            raise FillError(f"Reading account data failed: {status}")
        return data

    async def is_trusted(self, user_id, matrix_user_id, version_info):
        """Whether the backup is the member's own, as :mod:`key_backup` defines it.

        None when it can't be told yet: Waldur holds a recovery key, but the
        member's secret storage holds no backup key it opens. The drawer escrows
        the key before it writes secret storage, so a fill asked for at escrow
        can see this; such a refusal is not recorded against the backup.
        """
        public_key = key_backup.backup_public_key(version_info)
        if public_key is None:
            return False
        recovery_key = await self.db(history_backfill.recovery_key)(user_id)
        if recovery_key:
            # No fallback: a homeserver that withholds the secret must not get
            # to vouch for the backup with keys of its own instead.
            stored = await self._key_in_secret_storage(recovery_key, matrix_user_id)
            del recovery_key  # keep key material out of error reports' frame locals
            if stored is None:
                return None
            return stored == public_key
        keys = await self.bot.query_keys([matrix_user_id])
        master = cross_signing.published_key(
            (keys.get("master_keys") or {}).get(matrix_user_id),
            matrix_user_id,
            cross_signing.MASTER,
        )
        if master is None:
            return False
        pinned = await self.db(history_backfill.pin_master_key)(user_id, master)
        if pinned != master:
            logger.warning(
                "The master cross-signing key of %s is not the one pinned for "
                "them; no room history is written until Waldur holds their "
                "recovery key.",
                matrix_user_id,
            )
            return False
        return key_backup.signed_by_owner(version_info, matrix_user_id, keys)

    async def _key_in_secret_storage(self, recovery_key, matrix_user_id):
        secret = await self._account_data(matrix_user_id, key_backup.BACKUP_SECRET)
        descriptions = {}
        for key_id in key_backup.secret_key_ids(secret):
            descriptions[key_id] = await self._account_data(
                matrix_user_id, key_backup.SECRET_STORAGE_KEY_PREFIX + key_id
            )
        return key_backup.key_in_secret_storage(recovery_key, secret, descriptions)

    async def is_joined(self, room_id, matrix_user_id):
        """Whether the homeserver has the user joined to the room now."""
        status, data = await self._request(
            "GET",
            _path("rooms", room_id, "state", "m.room.member", matrix_user_id),
            self.bot.client.access_token,
        )
        if status == 404:
            return False
        if status != 200:
            raise FillError(f"Reading the membership failed: {status}")
        return data.get("membership") == "join"

    async def _backed_up_sessions(self, room_id, matrix_user_id, version):
        status, data = await self._as_user(
            "GET",
            _path("room_keys", "keys", room_id),
            matrix_user_id,
            params={"version": version},
        )
        if status == 404:
            return set()
        if status != 200:
            raise FillError(f"Reading the backed-up keys failed: {status}")
        sessions = data.get("sessions")
        return set(sessions) if isinstance(sessions, dict) else set()

    async def _room_senders(self, room_id):
        """``(curve25519, ed25519)`` of every device that may have sent in the
        room: the bot's own, and those of its past and present members."""
        members = await self.db(history_backfill.room_member_ids)(room_id)
        matrix_room = self.bot.client.rooms.get(room_id)
        if matrix_room is not None:
            members |= set(matrix_room.users)
        own = self.bot.client.olm.account.identity_keys
        senders = {(own["curve25519"], own["ed25519"])}
        # Deleted devices too: they sent before they were deleted.
        for user_id, devices in self.bot.client.device_store.items():
            if user_id in members:
                senders |= {(d.curve25519, d.ed25519) for d in devices.values()}
        return senders

    def sessions_of(self, room_id, senders):
        """The room's Megolm sessions the bot holds, in a stable order.

        Only sessions of a device known to have been in the room: nio takes a
        room key for any room id from any device, so a stranger could otherwise
        have the bot pass its keys off as the room's history.
        """
        by_sender = self.bot.client.olm.inbound_group_store[room_id]
        sessions = [
            s
            for by_id in by_sender.values()
            for s in by_id.values()
            if (s.sender_key, s.ed25519) in senders
        ]
        return sorted(sessions, key=lambda s: (s.sender_key, s.id))

    async def write_room(self, room_id, matrix_user_id, version, public_key):
        """Write the room's sessions the backup lacks; returns how many."""
        held = await self._backed_up_sessions(room_id, matrix_user_id, version)
        senders = await self._room_senders(room_id)
        missing = [s for s in self.sessions_of(room_id, senders) if s.id not in held]
        for start in range(0, len(missing), SESSIONS_PER_REQUEST):
            batch = missing[start : start + SESSIONS_PER_REQUEST]
            status, data = await self._as_user(
                "PUT",
                _path("room_keys", "keys", room_id),
                matrix_user_id,
                params={"version": version},
                json={
                    "sessions": {
                        s.id: key_backup.session_backup(s, public_key) for s in batch
                    }
                },
            )
            if status != 200:
                raise FillError(
                    f"Writing keys failed: {status} {data.get('errcode', '')}"
                )
        return len(missing)

    # Rounds.

    async def fill_user(self, user_id):
        fills = await self.db(history_backfill.due_fills)(user_id)
        if not fills:
            return
        matrix_user_id = fills[0][2]
        try:
            version_info = await self.current_backup(matrix_user_id)
            trusted = (
                await self.is_trusted(user_id, matrix_user_id, version_info)
                if version_info is not None
                else False
            )
        except (FillError, *self.transient_errors) as error:
            logger.warning(
                "Could not read the key backup of %s: %s", matrix_user_id, error
            )
            for member_pk, _, _, due_at in fills:
                await self.db(history_backfill.mark_retry)(member_pk, due_at)
            return
        version = str((version_info or {}).get("version") or "")
        if version_info is None or not trusted:
            if trusted is None:
                # Not recorded: the same backup may check out once the
                # member's secret storage is written.
                version = ""
            elif version_info is not None:
                logger.warning(
                    "Not writing room history into the key backup of %s: it is "
                    "not tied to the user's recovery key or identity.",
                    matrix_user_id,
                )
            for member_pk, _, _, due_at in fills:
                await self.db(history_backfill.mark_dropped)(member_pk, due_at, version)
            return
        public_key = key_backup.backup_public_key(version_info)
        for member_pk, room_id, _, due_at in fills:
            await self.fill_room(
                member_pk, room_id, matrix_user_id, due_at, version, public_key
            )

    async def fill_room(
        self, member_pk, room_id, matrix_user_id, due_at, version, public_key
    ):
        # Decided now, not when the fill was asked for: a member removed since
        # gets nothing.
        if not await self.db(history_backfill.is_current_member)(member_pk):
            await self.db(history_backfill.mark_dropped)(member_pk, due_at)
            return
        try:
            if not await self.is_joined(room_id, matrix_user_id):
                # Waldur added them, but the join has not reached the homeserver.
                await self.db(history_backfill.mark_retry)(member_pk, due_at)
                return
            count = await self.write_room(room_id, matrix_user_id, version, public_key)
        except (FillError, *self.transient_errors) as error:
            logger.warning(
                "Could not write the history of %s for %s: %s",
                room_id,
                matrix_user_id,
                error,
            )
            await self.db(history_backfill.mark_retry)(member_pk, due_at)
            return
        await self.db(history_backfill.mark_filled)(member_pk, due_at, version)
        if count:
            logger.info(
                "Wrote %s room keys of %s into the key backup of %s",
                count,
                room_id,
                matrix_user_id,
            )

    async def fill_due(self):
        for user_id in await self.db(history_backfill.due_users)(USERS_PER_ROUND):
            await self.renew_lease()
            try:
                await self.fill_user(user_id)
            except Exception:
                # Retried with backoff like any failed fill, so one user's
                # broken backup or session can't hold up everyone else's.
                logger.exception("Filling the key backup of user %s failed", user_id)
                for member_pk, _, _, due_at in await self.db(
                    history_backfill.due_fills
                )(user_id):
                    await self.db(history_backfill.mark_retry)(member_pk, due_at)

    async def check_versions(self):
        """Take the next step of the version check: ask for a fill wherever a
        member's backup is not the one last handled.

        A pass walks every member a batch per call, so a large deployment's
        pass is spread over many rounds instead of holding up the fills; a new
        pass starts once the last one is VERSION_CHECK_SECONDS old.
        """
        if self._check_after is None:
            if (
                self._check_started_at is not None
                and time.monotonic() - self._check_started_at < VERSION_CHECK_SECONDS
            ):
                return
            self._check_after = 0
            self._check_started_at = time.monotonic()
        batch = await self.db(history_backfill.members_to_check)(
            self._check_after, VERSION_CHECK_BATCH
        )
        if not batch:
            self._check_after = None
            return
        for user_id, matrix_user_id in batch:
            self._check_after = user_id
            try:
                version_info = await self.current_backup(matrix_user_id)
            except (FillError, *self.transient_errors) as error:
                logger.warning(
                    "Could not read the key backup of %s: %s", matrix_user_id, error
                )
                continue
            version = (version_info or {}).get("version")
            if version:
                await self.db(history_backfill.request_if_version_changed)(
                    user_id, str(version)
                )
