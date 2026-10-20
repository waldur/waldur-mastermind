"""The Matrix bot: Waldur's encrypted member of every Waldur room.

One long-running process (the ``matrix_bot`` management command) signs in as the
bot on a device of its own, keeps that device's keys in its crypto store, and:

- syncs, so it can decrypt the commands users send it and answer them, each
  beside the sync loop, which keeps going while one takes long (``!ticket``
  waits for a model to write a draft);
- posts everything Waldur queues in the outbox, encrypted for the room;
- exports rooms' history, decrypted with the room keys it holds;
- shares room keys only with devices their owner's identity vouches for;
- writes rooms' earlier keys into new members' key backups.

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
from django.db import connections
from nio import (
    AsyncClient,
    AsyncClientConfig,
    KeysQueryResponse,
    LocalProtocolError,
    MegolmEvent,
    OlmUnverifiedDeviceError,
    RoomGetStateResponse,
    RoomMessageText,
    RoomPutStateError,
    RoomSendResponse,
    SyncError,
    SyncResponse,
)

from waldur_core.configuration import config
from waldur_mastermind.matrix_chat import (
    bot_export,
    bot_state,
    commands,
    history_filler,
    history_reader,
    matrix_client,
    models,
    recovery_keys,
    tasks,
    tickets,
)
from waldur_mastermind.matrix_chat.crypto import cross_signing, nio_compat
from waldur_mastermind.matrix_chat.crypto.store import PostgresStore

logger = logging.getLogger(__name__)


class _WithholdValidationDetails(logging.Filter):
    """nio logs why an event failed validation with the offending instance, and
    the event may be one the bot just decrypted: a message, or a room key sent
    to it. The record is kept, its details are not."""

    def filter(self, record):
        message = record.getMessage()
        if message.startswith(VALIDATION_MESSAGES):
            record.msg = (
                f"{message.split(':', 1)[0]} (details withheld: they may hold "
                "decrypted content)"
            )
            record.args = ()
        return True


VALIDATION_MESSAGES = (
    "Error validating event",
    "Error validating decrypted Olm event",
    "Error validating response",
)
# The nio loggers that log such details: event parsing, the Olm machine, and
# response parsing.
NIO_VALIDATION_LOGGERS = ("nio.events.misc", "nio.crypto.log", "nio.responses")
for _name in NIO_VALIDATION_LOGGERS:
    logging.getLogger(_name).addFilter(_WithholdValidationDetails())

SYNC_TIMEOUT_MS = 30_000
# How often the bot checks that every Waldur room it is in is encrypted.
ENCRYPTION_CHECK_SECONDS = 3600
# How often the bot looks for a change of homeserver or appservice token.
CONFIG_CHECK_SECONDS = 30
# How long a stopping bot lets the message being sent finish. Bounded: a send
# that hangs must not keep the bot from stopping or from taking new settings.
# Below Kubernetes' default 30 s grace period, so the lease is still released.
SEND_FINISH_SECONDS = 20
# Requests nio retries before giving up, so a send to a homeserver that is
# down fails and is retried from the outbox instead of blocking forever.
MAX_REQUEST_RETRIES = 5
OUTBOX_POLL_SECONDS = 2
EXPORT_POLL_SECONDS = 5
# Exports run side by side, so rooms disabled together are each exported within
# the time disabling one waits. What one export holds in memory beyond its
# messages (an attachment in flight, or its archive while it is saved) is held by
# one export at a time; see MatrixBot._export_memory.
EXPORT_SLOTS = 3
# How often the bot checks that each room describes the commands that work
# there; a room it has just joined or encrypted is checked at once.
COMMANDS_CHECK_SECONDS = 60
# A room whose descriptions could not be written is tried again after this,
# doubling with each failure up to the maximum.
COMMANDS_RETRY_SECONDS = 60
MAX_COMMANDS_RETRY_SECONDS = 6 * 3600
# Keys are queried for this many users at once.
KEYS_QUERY_BATCH = 100
# Commands answered at once; more wait for a slot. Each holds a worker thread.
# Drafts, which wait for the model, have slots of their own, so they never hold
# up the quick commands, sending a draft included.
COMMAND_SLOTS = 4
DRAFT_SLOTS = 2
# Commands waiting or running beyond which new ones are dropped, so a flood of
# them cannot pile up tasks; the room's reply limit refuses most before this.
MAX_PENDING_COMMANDS = 100
# Attempts at posting a draft's preview, which goes out directly, not from the
# outbox: its content holds the conversation's summary.
PREVIEW_ATTEMPTS = 3
PREVIEW_RETRY_SECONDS = 5
# nio loads its store only when it has a store path, which ours doesn't use.
STORE_PATH = "postgres"
# Rooms the bot posts into: active ones, and ones being disabled, whose members
# must read the notice before they are removed.
DELIVERABLE_ROOM_STATES = (models.RoomStates.ACTIVE, models.RoomStates.DISABLING)


class BotError(RuntimeError):
    """The bot cannot run as configured; an operator has to act."""


class LeaseLost(RuntimeError):
    """Another process took over, or the database could not renew the lease."""


class ConfigChanged(RuntimeError):
    """The homeserver or the appservice token changed; the bot must start over."""


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


async def _outcome(job):
    """What the worker ``job`` returned once it is done; None if it failed."""
    try:
        return await job
    except Exception:  # noqa: BLE001 - its own failure, not the stop's
        return None


def _worker(fn):
    """Run ``fn`` in a worker thread with a database connection of its own.

    For work that waits on other services (the model writing a draft, a
    remote helpdesk creating a ticket): in Django's database thread it would
    hold up every database call of the sync loop and the outbox meanwhile.
    """

    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            connections.close_all()

    return sync_to_async(call, thread_sensitive=False)


@dataclasses.dataclass(frozen=True)
class BotSettings:
    """What the bot reads from Constance, read once before its event loop starts:
    Django refuses database access from inside one."""

    homeserver_url: str
    as_token: str
    localpart: str
    user_id: str
    display_name: str

    def identifies_the_same_bot(self, other):
        """Whether ``other`` reaches the same homeserver as the same bot user,
        with the same appservice token: anything else needs a fresh start."""
        return (self.homeserver_url, self.as_token, self.localpart, self.user_id) == (
            other.homeserver_url,
            other.as_token,
            other.localpart,
            other.user_id,
        )

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
        self.settings = settings
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
        # Held while a message is being sent: the bot stops or starts over only
        # between messages, so none is cancelled after the homeserver took it
        # and before it is marked sent, to be sent again later.
        self._sending = asyncio.Lock()
        self.history = history_filler.HistoryFiller(
            self, _db, self._renew_lease, TRANSIENT_ERRORS
        )
        # Running exports, by task.
        self._exports = {}
        # Set when a room may need its command descriptions now.
        self._describe_now = asyncio.Event()
        # Commands being answered, and the slots they take.
        self._commands = set()
        self._command_slots = asyncio.Semaphore(COMMAND_SLOTS)
        self._draft_slots = asyncio.Semaphore(DRAFT_SLOTS)
        # (room, member) of the drafts being made: one at a time for each.
        self._drafting = set()
        # Held by whichever export holds an attachment in memory or saves its
        # files, so exports running side by side never hold more memory at once
        # than one export does alone.
        self._export_memory = bot_export.PriorityLock()

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
                max_timeouts=MAX_REQUEST_RETRIES,
                max_limit_exceeded=MAX_REQUEST_RETRIES,
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
        others = sorted(
            set(own["device_keys"].get(self.user_id) or {}) - {self.identity.device_id}
        )
        if others:
            # Never trusted, so they get no room keys; but anyone who signed in
            # as the bot holds the appservice token, so say so.
            logger.warning(
                "The bot user %s has devices other than the bot's own: %s. Only "
                "the appservice token can create them; rotate it, and delete "
                "the devices with the homeserver's admin tools.",
                self.user_id,
                ", ".join(others),
            )
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
            self._describe_now.set()

    async def sync_forever(self):
        first = True
        failures = 0
        checked_encryption_at = None
        while True:
            # Also before the sync: processing its answer writes to the store.
            await self._renew_lease()
            try:
                response = await self.client.sync(
                    timeout=0 if first else SYNC_TIMEOUT_MS, full_state=first
                )
            except TRANSIENT_ERRORS as error:
                # nio gives up after its bounded retries; an unreachable
                # homeserver is waited out here, not by exiting.
                response = error
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
                elif isinstance(event, RoomMessageText) and (
                    matrix_client.strip_reply_fallback(
                        event.body, event.source.get("content")
                    )
                    .strip()
                    .startswith("!")
                    or commands.invocation_text(
                        event.source.get("content"), self.user_id
                    )
                ):
                    self._start_command(room_id, event)

    # Commands.

    def _start_command(self, room_id, command_event):
        if len(self._commands) >= MAX_PENDING_COMMANDS:
            logger.warning(
                "Matrix bot has too many commands pending; dropping %s in %s",
                command_event.event_id,
                room_id,
            )
            return
        task = asyncio.create_task(
            self._answer(room_id, command_event),
            name=f"command-{command_event.event_id}",
        )
        self._commands.add(task)
        task.add_done_callback(self._commands.discard)

    async def _answer(self, room_id, command_event):
        """Answer one command, beside the sync loop. A failure is the
        command's, not the bot's, and is logged by type: its traceback's
        frames may hold decrypted content."""
        command_content = command_event.source.get("content") or {}
        command = tasks.parse_command(command_event.body, command_content, self.user_id)
        drafting = (room_id, command_event.sender)
        try:
            if tickets.wants_draft(command):
                if drafting in self._drafting:
                    await _worker(tickets.busy)(
                        room_id, command_event.event_id, command
                    )
                    return
                self._drafting.add(drafting)
                try:
                    async with self._draft_slots:
                        await self.draft_ticket(room_id, command_event, command)
                finally:
                    self._drafting.discard(drafting)
            else:
                async with self._command_slots:
                    await _worker(tasks.answer_command)(
                        room_id,
                        command_event.sender,
                        command_event.event_id,
                        command_event.body,
                        command_content,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Matrix bot command %s in %s failed: %s",
                command_event.event_id,
                room_id,
                type(error).__name__,
            )

    async def draft_ticket(self, room_id, command_event, command):
        """Draft a support ticket from the conversation ``command_event`` is in.

        The bot reads and decrypts the context itself: it alone holds the room
        keys, and a summary of the conversation must not pass through the
        broker or the outbox, which keep what they carry in clear.
        """
        # Shielded, here and for the draft: a stop cannot stop a worker
        # thread, which goes on to claim the command or store the draft.
        starting = asyncio.ensure_future(
            _worker(tickets.start)(
                room_id, command_event.sender, command_event.event_id, command
            )
        )
        try:
            request = await asyncio.shield(starting)
        except asyncio.CancelledError:
            request = await _outcome(starting)
            if request is not None:
                await _worker(tickets.fail)(request)
            raise
        if request is None:
            return
        # Nothing is read or asked of the model for a preview that could not
        # be posted: it would go out in clear.
        matrix_room = self.client.rooms.get(room_id)
        if matrix_room is None or not matrix_room.encrypted:
            logger.warning("Matrix bot will not draft a ticket in %s", room_id)
            await _worker(tickets.fail)(request)
            return
        drafting = None
        try:
            reader = history_reader.HistoryReader(
                self.client, self.homeserver_url, self.user_id
            )
            if request.in_thread:
                history_messages = await reader.thread(
                    room_id, request.thread_root, request.context_messages
                )
            else:
                history_messages = await reader.before(
                    room_id,
                    request.reply_to or request.event_id,
                    request.context_messages,
                )
            drafting = asyncio.ensure_future(
                _worker(tickets.draft)(request, history_messages)
            )
            ticket_preview = await asyncio.shield(drafting)
        except (bot_export.HistoryError, *TRANSIENT_ERRORS) as error:
            # A HistoryError says what failed without content; others by type.
            logger.warning(
                "Matrix bot could not draft a ticket in %s: %s",
                room_id,
                error
                if isinstance(error, bot_export.HistoryError)
                else type(error).__name__,
            )
            await _worker(tickets.fail)(request)
            return
        except asyncio.CancelledError:
            # The bot is stopping, and will not see the command again. The
            # outbox tells the requester once the bot is back; a draft the
            # model was writing is stored meanwhile, and dropped, unseen.
            if drafting is not None:
                stored = await _outcome(drafting)
                if stored is not None:
                    await _worker(tickets.discard)(stored.draft_uuid)
            await _worker(tickets.fail)(request)
            raise
        except Exception:
            await _worker(tickets.fail)(request)
            raise
        try:
            event_id = await self._post_preview(room_id, ticket_preview)
        except asyncio.CancelledError:
            await _worker(tickets.discard)(ticket_preview.draft_uuid)
            await _worker(tickets.fail)(request)
            raise
        if event_id is None:
            await _worker(tickets.discard)(ticket_preview.draft_uuid)
            await _worker(tickets.fail)(request)
        else:
            await _worker(tickets.posted)(ticket_preview.draft_uuid, event_id)

    async def _post_preview(self, room_id, preview):
        matrix_room = self.client.rooms.get(room_id)
        if matrix_room is None or not matrix_room.encrypted:
            # nio would send it in clear.
            logger.warning("Matrix bot will not post a ticket draft in %s", room_id)
            return None
        for attempt in range(PREVIEW_ATTEMPTS):
            async with self._sending:
                result = await self._send_content(room_id, preview.content)
            if isinstance(result, RoomSendResponse):
                return result.event_id
            logger.warning(
                "Matrix bot could not post a ticket draft in %s: %s",
                room_id,
                type(result).__name__,
            )
            if attempt + 1 < PREVIEW_ATTEMPTS:
                await asyncio.sleep(PREVIEW_RETRY_SECONDS)
        return None

    async def _join_invited_rooms(self, response):
        for room_id in response.rooms.invite:
            if await _db(_is_waldur_room)(room_id):
                await self.client.join(room_id)
                self._describe_now.set()

    # Command descriptions, for clients' composers.

    async def describe_commands_forever(self):
        """Keep each active room's command descriptions (MSC4391 state events)
        as :mod:`commands` says they should be.

        What the homeserver holds is read once per room and remembered; after
        that a room is written to only when what it should hold changes: a
        new room, or the helpdesk switched on or off, which every check sees
        within ``COMMANDS_CHECK_SECONDS``. A room that fails is left alone for
        a while, longer after each failure.
        """
        await self._synced.wait()
        described = {}
        # Room ID: (when to try again, the wait after the next failure).
        retry = {}
        while True:
            await self._renew_lease()
            # Cleared before the check, not after: a room joined while the
            # check runs sets it again, and is tried at once.
            self._describe_now.clear()
            try:
                wanted = await _db(commands.rooms_to_describe)()
                now = time.monotonic()
                for room_id, descriptions in wanted.items():
                    if described.get(room_id) == descriptions:
                        continue
                    if room_id in retry and retry[room_id][0] > now:
                        continue
                    if await self._describe_commands(room_id, descriptions):
                        described[room_id] = descriptions
                        retry.pop(room_id, None)
                    else:
                        wait = retry.get(room_id, (0, COMMANDS_RETRY_SECONDS))[1]
                        retry[room_id] = (
                            now + wait,
                            min(2 * wait, MAX_COMMANDS_RETRY_SECONDS),
                        )
                for room_id in set(described) - set(wanted):
                    del described[room_id]
                for room_id in set(retry) - set(wanted):
                    del retry[room_id]
            except TRANSIENT_ERRORS as error:
                logger.warning("Matrix bot could not describe its commands (%s)", error)
            try:
                await asyncio.wait_for(
                    self._describe_now.wait(), COMMANDS_CHECK_SECONDS
                )
            except TimeoutError:
                pass
            else:
                # A room just joined or encrypted is tried at once.
                retry.clear()

    async def _describe_commands(self, room_id, descriptions):
        """Write the descriptions that differ from the room's state; whether
        the room now holds them all."""
        # Not self.client.rooms: it holds only the rooms a sync since the
        # process started mentioned. A room the bot has not joined yet fails
        # here, and is tried again at the next check.
        response = await self.client.room_get_state(room_id)
        if not isinstance(response, RoomGetStateResponse):
            logger.warning(
                "Matrix bot could not read the state of %s: %s",
                room_id,
                getattr(response, "message", type(response).__name__),
            )
            return False
        held = {
            event.get("state_key"): event.get("content") or {}
            for event in response.events
            if event.get("type") == commands.COMMAND_DESCRIPTION_EVENT
        }
        written = 0
        for key, content in descriptions.items():
            if held.get(key, {}) == content:
                continue
            put = await self.client.room_put_state(
                room_id, commands.COMMAND_DESCRIPTION_EVENT, content, state_key=key
            )
            if isinstance(put, RoomPutStateError):
                logger.warning(
                    "The homeserver refused the bot's command descriptions in %s: %s",
                    room_id,
                    put.message,
                )
                return False
            written += 1
        if written:
            logger.info("Matrix bot described %d commands in %s", written, room_id)
        return True

    # Outbox.

    async def drain_outbox_forever(self):
        await self._synced.wait()
        while True:
            await self._renew_lease()
            for message in await _db(bot_state.due_messages)():
                async with self._sending:
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
            message.body,
            reply_to=message.reply_to or None,
            thread_root=message.thread_root or None,
        )
        content.update(message.extra_content or {})
        response = await self._send_content(room.room_id, content)
        if isinstance(response, RoomSendResponse):
            await _db(bot_state.mark_sent)(message, response.event_id)
        else:
            await _db(bot_state.mark_failed_attempt)(message, response)

    async def _send_content(self, room_id, content):
        """Send ``content`` into a room, encrypted for the devices judged
        trustworthy: the response, or the error it failed with."""
        try:
            return await self.client.room_send(
                room_id,
                "m.room.message",
                content,
                ignore_unverified_devices=False,
            )
        except OlmUnverifiedDeviceError as error:
            # A device appeared since the last judgement: judge the room's
            # members again, and retry later.
            matrix_room = self.client.rooms.get(room_id)
            users = set(matrix_room.users) if matrix_room else set()
            try:
                await self.mark_trust(users | {error.device.user_id})
            except TRANSIENT_ERRORS:
                pass
            return error
        except (LocalProtocolError, *TRANSIENT_ERRORS) as error:
            return error

    # History for new members.

    async def fill_history_forever(self):
        await self._synced.wait()
        while True:
            await self._renew_lease()
            try:
                await self.history.fill_due()
                await self.history.check_versions()
            except TRANSIENT_ERRORS as error:
                logger.warning("Matrix bot history fill failed (%s); retrying", error)
            except LeaseLost:
                raise
            except Exception:
                # History is a convenience: a fill that fails unexpectedly must
                # not stop the bot from syncing and posting.
                logger.exception("Matrix bot history fill failed; retrying")
            await asyncio.sleep(history_filler.POLL_SECONDS)

    # History exports.

    async def export_forever(self):
        """Run exports in EXPORT_SLOTS slots, each claiming its own export.

        Rooms being disabled get every free slot, so one waits only for the
        exports of rooms disabled before it, never for a whole manual or daily
        export: at most one of those runs at a time, and it gives way when a
        room being disabled finds every slot taken.
        """
        await self._synced.wait()
        # Exports a previous process was in the middle of are not finished by
        # anyone; this process holds the lease, so none of them is running.
        await _db(bot_state.fail_interrupted_exports)()
        running = self._exports
        try:
            while True:
                await self._renew_lease()
                while len(running) < EXPORT_SLOTS:
                    deletions_only = any(
                        export.export_type != models.ExportTypes.ON_DELETION
                        for export in running.values()
                    )
                    export = await _db(bot_state.claim_next_export)(
                        deletions_only=deletions_only
                    )
                    if export is None:
                        break
                    task = asyncio.create_task(
                        self.export(export), name=f"export-{export.uuid}"
                    )
                    running[task] = export
                if not running:
                    await asyncio.sleep(EXPORT_POLL_SECONDS)
                    continue
                done, _ = await asyncio.wait(
                    running,
                    timeout=EXPORT_POLL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    del running[task]
                    task.result()  # export() records failures; this re-raises a lost lease
        finally:
            for task in running:
                task.cancel()
            # Each puts its export back before it ends.
            await asyncio.gather(*running, return_exceptions=True)
            running.clear()

    async def export(self, export):
        """Export one room's history, and record how it went.

        A stop puts the export back, to be done by the next process. Anything
        else that goes wrong fails this export, not the bot, and is reported
        by its type only: its traceback's frames hold decrypted content.
        """
        room_id = export.room.room_id
        try:
            if not room_id:
                raise bot_export.ExportError("The room was never created.")
            export_media = await _db(lambda: config.MATRIX_EXPORT_MEDIA)()
            priority = 0 if export.export_type == models.ExportTypes.ON_DELETION else 1
            exporter = bot_export.RoomExporter(
                self.client,
                self.homeserver_url,
                export_media=export_media,
                should_yield=self._should_yield_to(export),
                memory_lock=self._export_memory,
                priority=priority,
                identity_of=self.sender_identity,
            )
            prepared_export = await exporter.export(room_id, export.room.room_name)
            try:
                await self._renew_lease()
                # Saving reads the stored files whole into memory: one export
                # at a time, and never next to an attachment in flight. A save
                # runs in the database thread, so the lease is not renewed
                # until it ends: a save longer than the lease's lifetime would
                # let a second bot process take over meanwhile, which then
                # fails this export at its start and this save writes nothing.
                async with self._export_memory.hold(priority):
                    saved = await _db(tasks.save_history_export)(
                        export, prepared_export
                    )
            finally:
                prepared_export.close()
            if not saved:
                logger.warning(
                    "History export %s was no longer this process's to write; "
                    "its result is discarded",
                    export.uuid,
                )
        except bot_export.Preempted:
            logger.info(
                "History export %s gives way to a room being closed", export.uuid
            )
            await _db(bot_state.requeue_export)(export)
        except asyncio.CancelledError:
            # Work already handed to a thread (a save, an attachment being
            # written) finishes there after the cancel. A save queued in the
            # database thread runs before this requeue: the export is then
            # completed and the requeue, bound to this claim, leaves it so.
            # After a lost lease the requeue is as harmless: if another process
            # claimed the export since, this claim no longer matches.
            await asyncio.shield(_db(bot_state.requeue_export)(export))
            raise
        except LeaseLost:
            raise
        except bot_export.ExportError as error:
            logger.warning("History export %s failed: %s", export.uuid, error)
            await _db(bot_state.fail_export)(export, str(error))
        except Exception as error:
            logger.error(
                "History export %s of %s failed: %s",
                export.uuid,
                room_id,
                type(error).__name__,
            )
            await _db(bot_state.fail_export)(
                export, f"The export failed ({type(error).__name__})."
            )

    async def sender_identity(self, matrix_user_id):
        """``(label, vouched)`` for a message's sender: one of
        ``bot_export.SENDER_IDENTITIES``, and the devices the identity it was
        judged on signs, from the same ``/keys/query`` answer
        (see ``bot_export.event_identity``)."""
        if matrix_user_id == self.user_id:
            own = self.client.olm.account.identity_keys
            return "bot", {self.client.device_id: (own["ed25519"], own["curve25519"])}
        try:
            profile = await _db(_identity_profile)(matrix_user_id)
            if profile is None:
                return "unknown", {}
            recovery_key, pinned = profile
            del profile  # the recovery key is kept under its own name only
            keys = await self.query_keys([matrix_user_id])
            master_key = (keys.get("master_keys") or {}).get(matrix_user_id)
            published = cross_signing.published_key(
                master_key, matrix_user_id, cross_signing.MASTER
            )
            if published is None:
                return "unknown", {}
            vouched = _vouched_devices(matrix_user_id, keys)
            if recovery_key:
                opens = await self._escrow_unlocks(
                    matrix_user_id, recovery_key, master_key
                )
                del recovery_key  # keep key material out of error reports
                return ("escrowed" if opens else "changed"), vouched
            if pinned:
                return ("pinned" if pinned == published else "changed"), vouched
            return "unknown", {}
        except Exception as error:  # noqa: BLE001 - a label, never a failed export
            logger.warning(
                "Could not check the identity of %s for an export: %s",
                matrix_user_id,
                type(error).__name__,
            )
            return "unknown", {}

    async def _escrow_unlocks(self, matrix_user_id, recovery_key, master_key):
        """Whether the escrowed recovery key opens the user's secret storage and
        the private master key stored there is ``master_key``. Read with the
        bot's own snapshot of the appservice token, as the history filler reads
        it."""
        account_data = self.history.account_data
        default = await account_data(matrix_user_id, "m.secret_storage.default_key")
        key_id = default.get("key") if isinstance(default, dict) else None
        if not isinstance(key_id, str) or not key_id:
            return False
        key_info = await account_data(matrix_user_id, f"m.secret_storage.key.{key_id}")
        master = await account_data(matrix_user_id, "m.cross_signing.master")
        encrypted = master.get("encrypted") if isinstance(master, dict) else None
        stored = encrypted.get(key_id) if isinstance(encrypted, dict) else None
        return recovery_keys.recovery_key_opens(
            recovery_key, key_info
        ) and recovery_keys.unlocks_master_key(recovery_key, stored, master_key)

    def _should_yield_to(self, export):
        """For an export no task waits for: whether a room being closed waits
        for its export and finds every slot taken, so this one's goes to it.
        Disabling a room waits a bounded time, which a long export ahead of it
        would use up."""
        if export.export_type == models.ExportTypes.ON_DELETION:
            return None

        async def should_yield():
            # A slot may free between the two checks, so an export can give way
            # needlessly; it is put back and starts over, which costs time only.
            if len(self._exports) < EXPORT_SLOTS:
                return False
            return await _db(bot_state.deletion_export_waiting)()

        return should_yield

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

    async def _finish_sending(self):
        try:
            await asyncio.wait_for(self._sending.acquire(), SEND_FINISH_SECONDS)
        except TimeoutError:
            logger.warning(
                "The Matrix bot stops while a message is still being sent; it "
                "may be sent again."
            )

    async def watch_config_forever(self):
        """Stop when the homeserver or the appservice token changes, so the bot
        signs in again with what is configured now: a rotated token must not
        stay in use until someone restarts the process."""
        while True:
            await asyncio.sleep(CONFIG_CHECK_SECONDS)
            current = await _db(BotSettings.from_config)()
            if not current.identifies_the_same_bot(self.settings):
                raise ConfigChanged("The Matrix settings changed.")

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
            asyncio.create_task(self.fill_history_forever(), name="history"),
            asyncio.create_task(self.export_forever(), name="exports"),
            asyncio.create_task(self.describe_commands_forever(), name="commands"),
            asyncio.create_task(self.renew_lease_forever(), name="lease"),
            asyncio.create_task(self.watch_config_forever(), name="config"),
            asyncio.create_task(stop.wait(), name="stop"),
        ]
        try:
            done, _ = await asyncio.wait(parts, return_when=asyncio.FIRST_COMPLETED)
            changed = any(
                part.get_name() == "config"
                and not part.cancelled()
                and isinstance(part.exception(), ConfigChanged)
                for part in done
            )
            if stop.is_set() or changed:
                # A requested stop or start-over lets the message being sent
                # finish, so it is not cancelled after the homeserver took it
                # and sent again. A lost lease does not wait: another process
                # owns the store now.
                await self._finish_sending()
            if stop.is_set():
                return  # a stop wins over a settings change seen at once
            for part in done:
                part.result()  # re-raises what stopped it
        finally:
            for part in [*parts, *self._commands]:
                part.cancel()
            await asyncio.gather(*parts, *self._commands, return_exceptions=True)
            try:
                await self.history.close()
            except Exception:  # noqa: BLE001 - the client and store still close
                logger.warning("Closing the history filler failed", exc_info=True)
            await self.client.close()
            # AsyncClient.close() closes only HTTP; the store's connection
            # would otherwise outlive each start-over.
            store = getattr(self.client, "store", None)
            if store is not None:
                store.database.close()


def _vouched_devices(user_id, keys):
    """``{device id: (ed25519, curve25519)}`` of the devices the user's identity
    signs, in a ``/keys/query`` answer."""
    devices = (keys.get("device_keys") or {}).get(user_id) or {}
    vouched = {}
    for device_id, ed25519 in cross_signing.cross_signed_devices(user_id, keys).items():
        curve25519 = (devices[device_id].get("keys") or {}).get(
            f"curve25519:{device_id}"
        )
        if isinstance(curve25519, str):
            vouched[device_id] = (ed25519, curve25519)
    return vouched


def _identity_profile(matrix_user_id):
    """``(recovery key, pinned master key)`` of a Waldur user, or None. The
    recovery key is "" if Waldur holds none it can read."""
    profile = (
        models.MatrixUserProfile.objects.filter(matrix_user_id=matrix_user_id)
        .values_list("recovery_key", "pinned_master_key")
        .first()
    )
    if profile is None:
        return None
    recovery_key, pinned = profile
    return recovery_keys.escrowed(recovery_key), pinned


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
