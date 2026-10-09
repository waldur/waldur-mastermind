"""The Matrix bot: Waldur's encrypted member of every Waldur room.

One long-running process (the ``matrix_bot`` management command) signs in as the
bot on a device of its own, keeps that device's keys in its crypto store, and:

- syncs, so it can decrypt the commands users send it and answer them;
- posts everything Waldur queues in the outbox, encrypted for the room;
- shares room keys only with devices their owner's identity vouches for.

The bot's identity is cross-signed: its master key signs its self-signing key,
which signs its device, so members' clients see a device the bot vouches for.
For every other user it marks each device verified if that user's identity
signs it and blacklisted otherwise, and sends with ``ignore_unverified_devices``
off, so nio refuses to send while any device is still unjudged.

This module imports matrix-nio at the top. Only the bot process and the tests
import it; nothing on the API's or Celery's startup path does.
"""

import asyncio
import dataclasses
import logging
import random
import time

import aiohttp
import httpx
import vodozemac
from asgiref.sync import sync_to_async
from constance import config
from django.db import connections
from nio import (
    AsyncClient,
    AsyncClientConfig,
    KeysQueryResponse,
    LocalProtocolError,
    MegolmEvent,
    OlmUnverifiedDeviceError,
    RoomMessageText,
    RoomPutStateError,
    RoomSendResponse,
    SyncError,
    SyncResponse,
)

from waldur_mastermind.matrix_chat import bot_state, matrix_client, models, tasks
from waldur_mastermind.matrix_chat.crypto import cross_signing, nio_compat
from waldur_mastermind.matrix_chat.crypto.store import PostgresStore

logger = logging.getLogger(__name__)

SYNC_TIMEOUT_MS = 30_000
# How often the bot checks that every Waldur room it is in is encrypted.
ENCRYPTION_CHECK_SECONDS = 3600
OUTBOX_POLL_SECONDS = 2
# Keys are queried for this many users at once.
KEYS_QUERY_BATCH = 100
# nio loads its store only when it has a store path, which ours doesn't use.
STORE_PATH = "postgres"
# Rooms the bot posts into: active ones, and ones being disabled, whose members
# must read the notice before they are removed.
DELIVERABLE_ROOM_STATES = (models.RoomStates.ACTIVE, models.RoomStates.DISABLING)


class BotError(RuntimeError):
    """The bot cannot run as configured; an operator has to act."""


class LeaseLost(RuntimeError):
    """Another process took over, or the database could not renew the lease."""


class TransientError(RuntimeError):
    """The homeserver could not answer now; try again later."""


# Failures one cycle of syncing or sending survives: the bot backs off and
# tries again instead of exiting.
TRANSIENT_ERRORS = (
    TransientError,
    httpx.HTTPError,
    aiohttp.ClientError,
    asyncio.TimeoutError,
    OSError,
)


def _drop_broken_connections():
    # A long-running process keeps its connection; Django reconnects on the
    # next query once a broken one is closed. close_old_connections() would also
    # close healthy ones whenever CONN_MAX_AGE is 0, so on every call.
    for connection in connections.all(initialized_only=True):
        if connection.connection is not None and not connection.is_usable():
            connection.close()


def _db(fn):
    """Run ``fn`` in Django's database thread, on a live connection."""

    def call(*args, **kwargs):
        _drop_broken_connections()
        return fn(*args, **kwargs)

    return sync_to_async(call, thread_sensitive=True)


@dataclasses.dataclass(frozen=True)
class BotSettings:
    """What the bot reads from Constance, read once before its event loop starts:
    Django refuses database access from inside one."""

    homeserver_url: str
    as_token: str
    localpart: str
    user_id: str
    display_name: str

    @classmethod
    def from_config(cls):
        return cls(
            homeserver_url=config.MATRIX_HOMESERVER_URL,
            as_token=config.MATRIX_APPSERVICE_AS_TOKEN,
            localpart=config.MATRIX_APPSERVICE_SENDER_LOCALPART or "waldur-bot",
            user_id=matrix_client.get_bot_user_id(),
            display_name=matrix_client.get_bot_display_name(),
        )


class MatrixBot:
    def __init__(self, holder, settings):
        self.holder = holder
        self.homeserver_url = settings.homeserver_url
        self.as_token = settings.as_token
        self.localpart = settings.localpart
        self.user_id = settings.user_id
        self.display_name = settings.display_name
        self.client = None
        self.identity = None
        # The first sync after a fresh store replays recent history; its
        # commands were answered by whoever ran then, or are too old to answer.
        self._answer_commands = False
        # Set once the first sync has filled the client's rooms; nothing can be
        # sent before.
        self._synced = asyncio.Event()

    # Homeserver calls the bot makes itself: nio 0.26 has no cross-signing.

    async def _call(self, method, path, token, json=None):
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.request(
                method,
                f"{self.homeserver_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=json,
            )
        try:
            data = response.json()
        except ValueError:
            data = {}
        return response.status_code, data if isinstance(data, dict) else {}

    async def _access_token(self):
        """The bot's token: the one it holds while the homeserver accepts it, else
        a new sign-in on the bot's device, kept for the next start."""
        token = self.identity.access_token
        # A token is only ever sent to the homeserver that issued it.
        if token and self.identity.access_token_homeserver == self.homeserver_url:
            status, data = await self._call(
                "GET", "/_matrix/client/v3/account/whoami", token
            )
            if (
                status == 200
                and data.get("user_id") == self.user_id
                and data.get("device_id") == self.identity.device_id
            ):
                return token
            # Replaced only when it is not the bot's device's or the homeserver
            # no longer knows it: another sign-in leaves one more live token.
            unknown = status == 401 and data.get("errcode") == "M_UNKNOWN_TOKEN"
            if status != 200 and not unknown:
                raise TransientError(f"Checking the bot's token failed: {status}")
        token = await self._login()
        await _db(bot_state.save_access_token)(
            self.identity, token, self.homeserver_url
        )
        return token

    async def _login(self):
        status, data = await self._call(
            "POST",
            "/_matrix/client/v3/login",
            self.as_token,
            {
                "type": "m.login.application_service",
                "identifier": {"type": "m.id.user", "user": self.localpart},
                "device_id": self.identity.device_id,
                "initial_device_display_name": self.display_name,
            },
        )
        if status != 200 or not data.get("access_token"):
            raise BotError(
                f"The homeserver refused to sign the bot in ({status} "
                f"{data.get('errcode', '')}). Check the appservice registration."
            )
        if data.get("device_id") != self.identity.device_id:
            raise BotError(
                f"The homeserver signed the bot in on device {data.get('device_id')}, "
                f"not {self.identity.device_id}."
            )
        return data["access_token"]

    async def query_keys(self, user_ids):
        """The raw ``/keys/query`` answer for ``user_ids``, merged across batches."""
        merged = {"device_keys": {}, "master_keys": {}, "self_signing_keys": {}}
        users = sorted(user_ids)
        for start in range(0, len(users), KEYS_QUERY_BATCH):
            batch = users[start : start + KEYS_QUERY_BATCH]
            status, data = await self._call(
                "POST",
                "/_matrix/client/v3/keys/query",
                self.client.access_token,
                {"device_keys": {user: [] for user in batch}},
            )
            if status != 200:
                raise TransientError(
                    f"Querying keys failed: {status} {data.get('errcode')}"
                )
            for section in merged:
                merged[section].update(data.get(section) or {})
        return merged

    # Start-up.

    async def start(self):
        nio_compat.apply()
        self.identity = await _db(bot_state.get_or_create_identity)(self.user_id)
        access_token = await self._access_token()
        self.client = AsyncClient(
            self.homeserver_url,
            self.user_id,
            device_id=self.identity.device_id,
            store_path=STORE_PATH,
            config=AsyncClientConfig(
                store=PostgresStore,
                encryption_enabled=True,
                store_sync_tokens=True,
                pickle_key=self.identity.pickle_key,
            ),
        )
        # Opens the store and loads the Olm account: a wrong pickle key fails
        # here, before the bot does anything with the store.
        try:
            self.client.restore_login(
                self.user_id, self.identity.device_id, access_token
            )
        except (vodozemac.PickleException, vodozemac.LibolmPickleException) as error:
            raise BotError(
                "The bot's crypto store cannot be read with the bot's pickle key. "
                "The store is left as it is: resetting it would discard the bot's "
                "identity and every room key it holds. Restore the pickle key, or "
                "the FIELD_ENCRYPTION_KEY that decrypts it."
            ) from error
        if self.client.olm is None:
            raise BotError("The bot's crypto store did not open.")
        self._judge_after_every_keys_query()
        self._answer_commands = self.client.loaded_sync_token is not None
        if self.client.should_upload_keys:
            await self.client.keys_upload()
        await self.ensure_cross_signed()
        logger.info(
            "Matrix bot %s running on device %s", self.user_id, self.identity.device_id
        )

    async def ensure_cross_signed(self):
        """Publish the bot's cross-signing keys once, and sign its device."""
        seeds = await _db(bot_state.load_cross_signing_seeds)(self.identity)
        own = await self.query_keys([self.user_id])
        published_master = cross_signing.published_key(
            own["master_keys"].get(self.user_id), self.user_id, cross_signing.MASTER
        )
        if seeds:
            master = cross_signing.SigningKey(seeds[cross_signing.MASTER])
            self_signing = cross_signing.SigningKey(seeds[cross_signing.SELF_SIGNING])
            del seeds  # keep raw key material out of error reports' frame locals
        elif published_master:
            raise BotError(
                f"{self.user_id} already has cross-signing keys that Waldur does not "
                "hold. They have to be reset on the homeserver before the bot can "
                "vouch for its device."
            )
        else:
            master = cross_signing.SigningKey.generate()
            self_signing = cross_signing.SigningKey.generate()
            # Saved before publishing: keys published but lost could not be
            # replaced without the bot's password, which it has none of.
            await _db(bot_state.save_cross_signing_seeds)(
                self.identity,
                {
                    cross_signing.MASTER: master.seed,
                    cross_signing.SELF_SIGNING: self_signing.seed,
                },
            )

        if published_master is None:
            await self._publish_cross_signing_keys(master, self_signing)
            own = await self.query_keys([self.user_id])
        elif published_master != master.public_key:
            raise BotError(
                f"The cross-signing master key published for {self.user_id} is not "
                "the one Waldur holds for the bot."
            )
        elif (
            cross_signing.self_signing_key(self.user_id, own) != self_signing.public_key
        ):
            raise BotError(
                f"The self-signing key published for {self.user_id} is not the one "
                "Waldur holds for the bot."
            )

        # What the bot vouches for is its own device as its Olm account defines
        # it, never what the homeserver lists: signing that would let a
        # homeserver slip in keys of its own under the bot's device id.
        device = self._own_device_keys()
        published = (own["device_keys"].get(self.user_id) or {}).get(
            self.identity.device_id
        )
        if not isinstance(published, dict) or published.get("keys") != device["keys"]:
            raise BotError(
                "The homeserver lists other keys for the bot's device than the "
                "bot holds."
            )
        if not cross_signing.has_signature(
            {**device, "signatures": published.get("signatures")},
            self.user_id,
            self_signing.public_key,
        ):
            await self._sign_device(device, self_signing)

    def _own_device_keys(self):
        """The bot's device keys as nio uploads them, unsigned."""
        identity_keys = self.client.olm.account.identity_keys
        device_id = self.identity.device_id
        return {
            "algorithms": list(self.client.olm._algorithms),
            "device_id": device_id,
            "user_id": self.user_id,
            "keys": {
                f"curve25519:{device_id}": identity_keys["curve25519"],
                f"ed25519:{device_id}": identity_keys["ed25519"],
            },
        }

    async def _publish_cross_signing_keys(self, master, self_signing):
        status, data = await self._call(
            "POST",
            "/_matrix/client/v3/keys/device_signing/upload",
            self.client.access_token,
            {
                "master_key": cross_signing.cross_signing_key(
                    self.user_id, cross_signing.MASTER, master
                ),
                "self_signing_key": cross_signing.cross_signing_key(
                    self.user_id,
                    cross_signing.SELF_SIGNING,
                    self_signing,
                    signed_by=master,
                ),
            },
        )
        if status != 200:
            raise BotError(
                f"The homeserver refused the bot's cross-signing keys ({status} "
                f"{data.get('errcode', '')}). A homeserver that asks for "
                "interactive authentication on the first upload does not support "
                "cross-signing for appservice users."
            )

    async def _sign_device(self, device, self_signing):
        signed = self_signing.sign(device, self.user_id)
        status, data = await self._call(
            "POST",
            "/_matrix/client/v3/keys/signatures/upload",
            self.client.access_token,
            {self.user_id: {self.identity.device_id: signed}},
        )
        if status != 200 or data.get("failures"):
            raise BotError(f"Signing the bot's device failed: {status} {data}")

    # Trust.

    async def mark_trust(self, user_ids):
        """Verify the devices each user's identity signs; blacklist the rest.

        The bot itself has one device. Any other device of the bot user is
        blacklisted whatever signs it: trusting one would have nio forward it
        every room key the bot holds on request.
        """
        user_ids = set(user_ids)
        if not user_ids:
            return
        others = user_ids - {self.user_id}
        keys = await self.query_keys(others) if others else {}
        for user_id in user_ids:
            if user_id == self.user_id:
                signed = {}
            else:
                signed = cross_signing.cross_signed_devices(user_id, keys)
            for device in self.client.device_store.active_user_devices(user_id):
                if user_id == self.user_id and device.id == self.identity.device_id:
                    continue
                if signed.get(device.id) == device.ed25519:
                    if not device.verified:
                        self.client.verify_device(device)
                elif not device.blacklisted:
                    self.client.blacklist_device(device)

    def _distrust(self, user_ids):
        for user_id in user_ids:
            for device in self.client.device_store.active_user_devices(user_id):
                if user_id == self.user_id and device.id == self.identity.device_id:
                    continue
                if not device.blacklisted:
                    self.client.blacklist_device(device)

    def _judge_after_every_keys_query(self):
        """Re-judge every user a keys query reports on, whoever made the query.

        nio queries keys inside room_send as well as between syncs, and a device
        whose signature was withdrawn must lose trust on the next answer, not on
        the next restart.
        """
        original = self.client.keys_query

        async def keys_query(*args, **kwargs):
            response = await original(*args, **kwargs)
            if isinstance(response, KeysQueryResponse):
                users = set(response.device_keys.keys())
                try:
                    await self.mark_trust(users)
                except TRANSIENT_ERRORS:
                    # nio considers these users queried now. Fail closed until
                    # a judgement succeeds: blacklist their devices and ask nio
                    # to query them again.
                    self._distrust(users)
                    self.client.olm.users_for_key_query.update(users)
                    raise
            return response

        self.client.keys_query = keys_query

    async def _keys_housekeeping(self):
        """What nio's sync_forever does between syncs."""
        if self.client.should_upload_keys:
            await self.client.keys_upload()
        if self.client.should_query_keys:
            await self.client.keys_query()
        if self.client.should_claim_keys:
            await self.client.keys_claim(self.client.get_users_for_key_claiming())
        await self.client.send_to_device_messages()

    # Sync and commands.

    async def ensure_rooms_encrypted(self):
        """Turn encryption on in every active Waldur room the bot is in.

        Rooms are created encrypted; this covers rooms created before that, and
        a room whose encryption the bot does not see in its sync.
        """
        for room_id in await _db(_active_room_ids)():
            room = self.client.rooms.get(room_id)
            if room is not None and not room.encrypted:
                await self._turn_on_encryption(room_id)

    async def _turn_on_encryption(self, room_id):
        response = await self.client.room_put_state(
            room_id,
            "m.room.encryption",
            matrix_client.ENCRYPTION_STATE["content"],
        )
        if isinstance(response, RoomPutStateError):
            logger.warning(
                "The homeserver refused to turn on encryption in %s: %s",
                room_id,
                response.message,
            )
        else:
            logger.info("Turned on encryption in %s", room_id)

    async def sync_forever(self):
        first = True
        failures = 0
        checked_encryption_at = None
        while True:
            # Also before the sync: processing its answer writes to the store.
            await self._renew_lease()
            response = await self.client.sync(
                timeout=0 if first else SYNC_TIMEOUT_MS, full_state=first
            )
            if isinstance(response, SyncError) or not isinstance(
                response, SyncResponse
            ):
                failures += 1
                delay = min(60, 2**failures) + random.random()
                logger.warning("Matrix bot sync failed (%s); retrying", response)
                await asyncio.sleep(delay)
                continue
            await self._renew_lease()
            try:
                if first:
                    await self.mark_trust(self.client.device_store.users)
                await self._keys_housekeeping()
                if self._answer_commands:
                    await self._handle_timeline(response)
                await self._join_invited_rooms(response)
                if (
                    checked_encryption_at is None
                    or time.monotonic() - checked_encryption_at
                    > ENCRYPTION_CHECK_SECONDS
                ):
                    await self.ensure_rooms_encrypted()
                    checked_encryption_at = time.monotonic()
            except TRANSIENT_ERRORS as error:
                failures += 1
                logger.warning("Matrix bot sync step failed (%s); retrying", error)
                await asyncio.sleep(min(60, 2**failures) + random.random())
                continue
            failures = 0
            self._answer_commands = True
            first = False
            self._synced.set()

    async def _handle_timeline(self, response):
        for room_id, room in response.rooms.join.items():
            for event in room.timeline.events:
                if event.sender == self.user_id:
                    continue
                if isinstance(event, MegolmEvent):
                    logger.warning(
                        "Matrix bot cannot decrypt %s in %s from %s (session %s)",
                        event.event_id,
                        room_id,
                        event.sender,
                        event.session_id,
                    )
                elif isinstance(
                    event, RoomMessageText
                ) and event.body.strip().startswith("!"):
                    await _db(tasks.answer_command)(
                        room_id, event.sender, event.event_id, event.body
                    )

    async def _join_invited_rooms(self, response):
        for room_id in response.rooms.invite:
            if await _db(_is_waldur_room)(room_id):
                await self.client.join(room_id)

    # Outbox.

    async def drain_outbox_forever(self):
        await self._synced.wait()
        while True:
            await self._renew_lease()
            for message in await _db(bot_state.due_messages)():
                await self.send(message)
            await asyncio.sleep(OUTBOX_POLL_SECONDS)

    async def send(self, message):
        room = message.room
        if room.state not in DELIVERABLE_ROOM_STATES or not room.room_id:
            await _db(bot_state.drop_undeliverable)(
                message, f"Room is {room.state}, not active."
            )
            return
        # Every Waldur room is encrypted. A room the bot's sync shows without
        # encryption (an older room, or a homeserver that leaves the state out)
        # gets it turned on, and the message waits: nio would send it in clear.
        matrix_room = self.client.rooms.get(room.room_id)
        if matrix_room is not None and not matrix_room.encrypted:
            try:
                await self._turn_on_encryption(room.room_id)
            except TRANSIENT_ERRORS:
                pass
            await _db(bot_state.mark_failed_attempt)(
                message, "The room is not encrypted yet."
            )
            return
        content = matrix_client.build_text_content(
            message.body, reply_to=message.reply_to or None
        )
        try:
            response = await self.client.room_send(
                room.room_id,
                "m.room.message",
                content,
                ignore_unverified_devices=False,
            )
        except OlmUnverifiedDeviceError as error:
            # A device appeared since the last judgement: judge the room's
            # members again, and retry later.
            matrix_room = self.client.rooms.get(room.room_id)
            users = set(matrix_room.users) if matrix_room else set()
            try:
                await self.mark_trust(users | {error.device.user_id})
            except TRANSIENT_ERRORS:
                pass
            await _db(bot_state.mark_failed_attempt)(message, error)
            return
        except (LocalProtocolError, *TRANSIENT_ERRORS) as error:
            await _db(bot_state.mark_failed_attempt)(message, error)
            return
        if isinstance(response, RoomSendResponse):
            await _db(bot_state.mark_sent)(message, response.event_id)
        else:
            await _db(bot_state.mark_failed_attempt)(message, response)

    # Lease.

    async def _renew_lease(self):
        """Renew the lease, or stop: a process that lost it must not touch the store.

        Called on a timer and before each round of work, so a process that was
        paused past its lease stops before it writes to the store again.
        """
        try:
            renewed = await _db(bot_state.renew_lease)(self.user_id, self.holder)
        except Exception as error:
            raise LeaseLost(f"Could not renew the bot's lease: {error}") from error
        if not renewed:
            raise LeaseLost("Another Matrix bot process took over the lease.")

    async def renew_lease_forever(self):
        while True:
            await asyncio.sleep(bot_state.LEASE_RENEW_INTERVAL.total_seconds())
            await self._renew_lease()

    async def run(self, stop):
        """Run until ``stop`` is set or a part fails; then shut everything down."""
        await self.start()
        parts = [
            asyncio.create_task(self.sync_forever(), name="sync"),
            asyncio.create_task(self.drain_outbox_forever(), name="outbox"),
            asyncio.create_task(self.renew_lease_forever(), name="lease"),
            asyncio.create_task(stop.wait(), name="stop"),
        ]
        try:
            done, _ = await asyncio.wait(parts, return_when=asyncio.FIRST_COMPLETED)
            for part in done:
                if part.get_name() != "stop":
                    part.result()  # re-raises what stopped it
        finally:
            for part in parts:
                part.cancel()
            await asyncio.gather(*parts, return_exceptions=True)
            await self.client.close()


def _active_room_ids():
    return list(
        models.MatrixRoom.objects.filter(state=models.RoomStates.ACTIVE)
        .exclude(room_id=None)
        .values_list("room_id", flat=True)
    )


def _is_waldur_room(room_id):
    return models.MatrixRoom.objects.filter(
        room_id=room_id, state=models.RoomStates.ACTIVE
    ).exists()
