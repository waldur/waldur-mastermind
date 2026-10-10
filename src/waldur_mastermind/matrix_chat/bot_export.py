"""History exports, read and decrypted by the bot.

Rooms are encrypted, and the bot is the one member Waldur runs: it holds the
room key of every message sent since it joined, in its crypto store. So the bot
pages through a room's history, decrypts each event with those keys and each
attachment with the key its event carries, and writes the export in the format
exports always had.

An event no key the bot holds decrypts stays in the export, marked as such: its
keys were never shared with the bot, or the bot's store was reset. Each
decrypted event says whether the device that sent it is one its owner's
identity vouches for, as the drawer shows it: the homeserver can put any sender
on an event, but not encrypt it with that sender's keys.

What ``verified`` cannot say:

- It trusts the cross-signing identity the homeserver publishes for the sender
  now. A homeserver that replaced a user's identity with one of its own, and
  signed a device of its own with it, has that device's messages verified.
- It is judged when the export is made, not when the message was sent: a
  device signed or unsigned since then is judged as it is now.

Decrypting has side effects inside nio, as it has during sync: an event whose
key is missing may queue a key request to its sender's device, and every
decrypted message index is remembered in nio's in-memory replay store, which
the bot's sync shares. An event the bot already decrypted when it arrived
decrypts again; another event under the same index is reported as a replay.

Decrypted content and keys are never logged. The locals that hold them are
named so that error reports scrub them (``waldur_core.logging.sentry``).

An export holds little in memory whatever the size of its room: its working
and final files (four at most) keep up to SPOOL_BYTES each in memory before
they spill to disk, so about 4 MiB, plus one chunk being encrypted. The messages
are written, one at a time, to working files encrypted under a key that exists
only in this process's memory (nothing decrypted reaches the disk), and the
final files are written the same way, under the export's own key: what is
stored is what the bot wrote. Only an attachment in flight and the save, which
reads the stored files whole, take much memory, and exports running side by
side take turns at those (see :class:`PriorityLock`).
"""

import asyncio
import contextlib
import dataclasses
import heapq
import itertools
import json
import logging
import re
import tempfile
import time
import zipfile
from urllib.parse import quote

import httpx
from django.utils import timezone
from nio import (
    BadEvent,
    Event,
    MegolmEvent,
    RedactedEvent,
    RoomEncryptedMedia,
    UnknownBadEvent,
)
from nio.exceptions import EncryptionError

from waldur_mastermind.matrix_chat import matrix_client
from waldur_mastermind.matrix_chat.crypto import attachments, export_files

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
PAGE_TIMEOUT_SECONDS = 60
# A page the homeserver rate-limits or fails is asked for once more.
PAGE_ATTEMPTS = 2
PAGE_RETRY_SECONDS = 2
MAX_PAGE_RETRY_SECONDS = 30
# One export may take this long; past it the export fails rather than hold up
# the exports after it. Below what closing a room waits for one.
EXPORT_TIMEOUT_SECONDS = 10 * 60
# A manual or daily export also ends here however long it waited for others.
MAX_EXPORT_SECONDS = 3 * EXPORT_TIMEOUT_SECONDS
# What one export holds in memory and writes into Waldur's database: media
# beyond these limits is left out of the archive and marked in the messages.
MAX_MEDIA_FILE_BYTES = 50 * 1024 * 1024
MAX_MEDIA_TOTAL_BYTES = 200 * 1024 * 1024
MEDIA_TIMEOUT_SECONDS = 120
# Working and final files are encrypted, and kept in memory only up to this
# size, then in temporary files.
SPOOL_BYTES = 1024 * 1024
# Names in the archive, before the file's extension.
MAX_ARCHIVE_NAME = 120
MAX_EXTENSION = 16

# What Waldur can say of a decrypted message's sender's identity, beyond
# ``verified`` (which trusts the master key the homeserver publishes now):
# - escrowed: the recovery key Waldur holds for the user unlocks that master
#   key, so the homeserver cannot have swapped it;
# - pinned: Waldur holds no recovery key, and the master key is the one the bot
#   pinned when it first saw it;
# - changed: the master key is neither: the user reset their identity, or the
#   homeserver replaced it;
# - bot: the bot's own messages;
# - unknown: not a Waldur user, no published master key, nothing pinned yet, or
#   the check failed.
SENDER_IDENTITIES = ("escrowed", "pinned", "changed", "bot", "unknown")
# Carries a decrypted entry's signing key from _message to the identity check.
SIGNING_KEY = "_signing_key"


def event_identity(identity, device_id, curve25519, ed25519):
    """The ``sender_identity`` of one message.

    ``identity`` is ``(label, vouched)`` for its sender: the label their
    identity earns, and the devices that same identity signs, as
    ``{device id: (ed25519, curve25519)}``. The label holds only for a message
    from one of those devices: the homeserver could otherwise pair a device of
    its own, which it has nio trust, with the user's genuine identity.
    """
    label, vouched = identity
    if label == "unknown":
        return label
    if ed25519 and vouched.get(device_id) == (ed25519, curve25519):
        return label
    return "changed"


# How nio says a Megolm message index was used by another event already.
REPLAY_ERROR = "Duplicate message index"

MEDIA_TOO_LARGE = "too large to export"
MEDIA_LIMIT_REACHED = "export size limit reached"
MEDIA_DOWNLOAD_FAILED = "download failed"
MEDIA_UNDECRYPTABLE = "could not be decrypted"


class PriorityLock:
    """A lock that goes to the waiter of the lowest priority number first, in
    the order they asked within one priority.

    Exports running side by side take turns with it at what takes memory, an
    attachment in flight or a save; the export of a room being closed, which a
    task waits for, goes before a manual or daily one.
    """

    def __init__(self):
        self._held = False
        self._waiters = []
        self._order = itertools.count()

    @contextlib.asynccontextmanager
    async def hold(self, priority):
        if not self._held and not self._waiters:
            self._held = True
        else:
            granted = asyncio.get_running_loop().create_future()
            heapq.heappush(self._waiters, (priority, next(self._order), granted))
            try:
                await granted
            except asyncio.CancelledError:
                if granted.done() and not granted.cancelled():
                    # Handed over just as the waiter was cancelled: pass it on.
                    self._release()
                raise
        try:
            yield
        finally:
            self._release()

    def _release(self):
        while self._waiters:
            _, _, granted = heapq.heappop(self._waiters)
            if not granted.done():
                granted.set_result(None)  # held on, by the next waiter
                return
        self._held = False


@dataclasses.dataclass
class ExportFiles:
    """An export's files, encrypted under ``data_key``, ready to be stored."""

    data_key: bytes = dataclasses.field(repr=False)
    messages: object
    media: object
    message_count: int
    media_count: int

    def close(self):
        self.messages.close()
        if self.media is not None:
            self.media.close()


def _spool():
    return tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)


def _read_lines(spool_key, source):
    source.seek(0)
    for export_line in export_files.decrypt_lines(
        spool_key, export_files.KIND_SPOOL, source
    ):
        yield json.loads(export_line)


def _indented(value, prefix):
    return json.dumps(value, indent=2, default=str).replace("\n", "\n" + prefix)


def write_export_document(writer, header, export_messages):
    """Write the export's JSON as ``json.dumps(..., indent=2, default=str)``
    would write ``header`` with a ``messages`` list added last, one message at
    a time."""
    writer.write(b"{\n")
    for key, value in header.items():
        writer.write(f"  {json.dumps(key)}: {_indented(value, '  ')},\n".encode())
    writer.write(b'  "messages": [')
    first = True
    for export_message in export_messages:
        writer.write(b"\n    " if first else b",\n    ")
        writer.write(_indented(export_message, "    ").encode())
        first = False
    writer.write(b"]\n}" if first else b"\n  ]\n}")


class ExportError(RuntimeError):
    """The export cannot be completed; the message says why, without content."""


class Preempted(Exception):
    """An export that must not wait is waiting for this one: stop and give way."""


class MediaUnavailable(Exception):
    """One attachment cannot be exported; the export goes on without it."""


def _retry_delay(details, headers):
    """How long to wait before asking again: what the homeserver asks for, in
    ``retry_after_ms`` or a ``Retry-After`` of seconds, within reason."""
    retry_after_ms = details.get("retry_after_ms")
    if (
        isinstance(retry_after_ms, int)
        and not isinstance(retry_after_ms, bool)
        and retry_after_ms > 0
    ):
        return min(retry_after_ms / 1000, MAX_PAGE_RETRY_SECONDS)
    retry_after = headers.get("retry-after", "")
    # ASCII digits only, and few of them: int() refuses very long strings, and
    # isdigit() alone takes digits of other scripts.
    if retry_after.isascii() and retry_after.isdecimal() and len(retry_after) <= 6:
        seconds = int(retry_after)
        if seconds > 0:
            return min(seconds, MAX_PAGE_RETRY_SECONDS)
    return PAGE_RETRY_SECONDS


def archive_name(event_id, filename):
    """A safe name in the archive: the event, then the file's name, with its
    extension kept and the rest cut to MAX_ARCHIVE_NAME characters."""
    safe_event_id = re.sub(r"[^\w\-.]", "_", event_id)
    safe_filename = re.sub(r"[^\w\-.]", "_", filename or "file")
    stem, dot, extension = safe_filename.rpartition(".")
    if not dot or not stem or len(extension) > MAX_EXTENSION:
        stem, extension = safe_filename, ""
    name = f"{safe_event_id}_{stem}"[:MAX_ARCHIVE_NAME]
    return f"{name}.{extension}" if extension else name


def _relation(content):
    """What a message relates to: the event it replaces or annotates, and the
    one it replies to. Without it an edit would read as a new message."""
    relates_to = content.get("m.relates_to") if isinstance(content, dict) else None
    if not isinstance(relates_to, dict):
        return None
    relation = {}
    if isinstance(relates_to.get("rel_type"), str):
        relation["rel_type"] = relates_to["rel_type"]
    if isinstance(relates_to.get("event_id"), str):
        relation["event_id"] = relates_to["event_id"]
    reply = relates_to.get("m.in_reply_to")
    if isinstance(reply, dict) and isinstance(reply.get("event_id"), str):
        relation["in_reply_to"] = reply["event_id"]
    return relation or None


class RoomExporter:
    """Reads one room's history with the bot's access token and room keys.

    ``should_yield``, if given, is awaited between pages and attachments; when
    it returns true the export stops with :class:`Preempted`.
    """

    def __init__(
        self,
        client,
        homeserver_url,
        *,
        export_media,
        should_yield=None,
        memory_lock=None,
        priority=0,
        identity_of=None,
        now=time.monotonic,
    ):
        self.client = client
        self.homeserver_url = homeserver_url
        self.export_media = export_media
        self._should_yield = should_yield
        # Held while an attachment is in memory, shared by exports running side
        # by side.
        self._memory_lock = memory_lock or PriorityLock()
        # How far Waldur can vouch for a sender's identity (see
        # SENDER_IDENTITIES), asked once per sender and export.
        self._identity_of = identity_of
        self._identities = {}
        self._priority = priority
        self._now = now
        self._deadline = now() + EXPORT_TIMEOUT_SECONDS
        # Waits for the shared lock move the deadline above; an export of a
        # room being closed is bounded by the disable task's own wait, any
        # other export by this, waits included.
        self._hard_deadline = (
            now() + MAX_EXPORT_SECONDS if priority != 0 else float("inf")
        )

    async def _checkpoint(self):
        if self._now() > self._hard_deadline:
            raise ExportError(
                f"The export took longer than {MAX_EXPORT_SECONDS} seconds, "
                "waiting for other exports included."
            )
        if self._now() > self._deadline:
            raise ExportError(
                f"The export took longer than {EXPORT_TIMEOUT_SECONDS} seconds."
            )
        if self._should_yield is not None and await self._should_yield():
            raise Preempted()

    async def _sender_identity(self, sender):
        if sender not in self._identities:
            self._identities[sender] = await self._identity_of(sender)
        return self._identities[sender]

    @contextlib.asynccontextmanager
    async def _holding_memory(self):
        """Hold the shared memory lock. Time spent waiting for it is another
        export's work, so it is not counted against this export's deadline."""
        asked_at = self._now()
        async with self._memory_lock.hold(self._priority):
            self._deadline += self._now() - asked_at
            yield

    def _auth(self):
        return {"Authorization": f"Bearer {self.client.access_token}"}

    async def export(self, room_id, room_name=""):
        """The export's files, encrypted under a new key of their own.

        The caller closes them. Messages are newest first, as before.
        """
        # Encrypts the working files; never stored, gone with this export.
        spool_key = export_files.new_data_key()
        read = _spool()
        processed = None
        media = None
        try:
            message_count = await self._read_history(room_id, spool_key, read)
            media_count = 0
            data_key = export_files.new_data_key()
            if self.export_media:
                processed = _spool()
                media = _spool()
                media_count = await self._collect_media(
                    spool_key, read, processed, data_key, media
                )
                if not media_count:
                    media.close()
                    media = None
            messages = _spool()
            try:
                writer = export_files.EncryptingWriter(
                    data_key, export_files.KIND_MESSAGES, messages
                )
                header = {
                    "room_id": room_id,
                    "room_name": room_name,
                    "exported_at": timezone.now().isoformat(),
                    "message_count": message_count,
                    "media_count": media_count,
                }
                await asyncio.to_thread(
                    write_export_document,
                    writer,
                    header,
                    (
                        export_record["m"]
                        for export_record in _read_lines(spool_key, processed or read)
                    ),
                )
                writer.close()
                messages.seek(0)
            except BaseException:
                messages.close()
                raise
            if media is not None:
                media.seek(0)
            return ExportFiles(data_key, messages, media, message_count, media_count)
        except BaseException:
            if media is not None:
                media.close()
            raise
        finally:
            read.close()
            if processed is not None:
                processed.close()

    async def _page(self, room_id, start):
        """One page of ``/messages``, newest first: ``(events, next token)``.

        Read here rather than through nio, which decrypts what it receives and
        leaves only the result: the export needs the encrypted event too, for
        the device that sent it and for an event whose payload is malformed.
        """
        params = {"dir": "b", "limit": PAGE_SIZE}
        if start:
            params["from"] = start
        url = (
            f"{self.homeserver_url}/_matrix/client/v3/rooms/"
            f"{quote(room_id, safe='')}/messages"
        )
        for attempt in range(PAGE_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=PAGE_TIMEOUT_SECONDS) as http:
                    page_response = await http.get(
                        url, params=params, headers=self._auth()
                    )
            except httpx.HTTPError as error:
                raise ExportError(
                    f"The homeserver could not be reached ({type(error).__name__})."
                ) from None
            try:
                export_page = page_response.json()
            except ValueError:
                export_page = None
            details = export_page if isinstance(export_page, dict) else {}
            if page_response.status_code == 200 and export_page is details:
                break
            retry = (
                page_response.status_code == 429
                or details.get("errcode") == "M_LIMIT_EXCEEDED"
                or page_response.status_code >= 500
            )
            if retry and attempt + 1 < PAGE_ATTEMPTS:
                await asyncio.sleep(_retry_delay(details, page_response.headers))
                continue
            # The homeserver's own message is left out: it is not ours to
            # store, and the status and code say what went wrong.
            errcode = details.get("errcode")
            code = errcode if isinstance(errcode, str) and errcode.isascii() else ""
            raise ExportError(
                f"Reading the room's history failed: {page_response.status_code} "
                f"{code[:64]}".strip()
            )
        chunk = export_page.get("chunk")
        if not isinstance(chunk, list):
            raise ExportError("The homeserver's answer has no events.")
        end = export_page.get("end")
        return chunk, end if isinstance(end, str) else None

    # Each chunk of a working file is encrypted on the event loop as it fills:
    # AES-GCM over 64 KiB takes microseconds, less than reading the page did.

    async def _read_history(self, room_id, spool_key, destination):
        """Read every page into ``destination``, as encrypted JSON lines of the
        message and, for encrypted media, its ``file`` object (key included,
        which never goes into the export); the number of messages."""
        writer = export_files.EncryptingWriter(
            spool_key, export_files.KIND_SPOOL, destination
        )
        count = 0
        start = ""
        while True:
            await self._checkpoint()
            chunk, end = await self._page(room_id, start)
            for export_event in chunk:
                export_message, encrypted_file = self._message(export_event, room_id)
                signing_key = export_message.pop(SIGNING_KEY, None)
                if "verified" in export_message and self._identity_of is not None:
                    export_message["sender_identity"] = event_identity(
                        await self._sender_identity(export_message["sender"]),
                        export_message["device_id"],
                        export_message["sender_key"],
                        signing_key,
                    )
                writer.write(
                    json.dumps(
                        {"m": export_message, "f": encrypted_file}, default=str
                    ).encode()
                    + b"\n"
                )
                count += 1
            if not chunk or not end or end == start:
                break
            start = end
        writer.close()
        return count

    def _message(self, export_event, room_id):
        """The export's entry for one raw event, and the ``file`` object of its
        encrypted media, if any."""
        if not isinstance(export_event, dict):
            export_event = {}
        # Whether it was sent encrypted is what the homeserver stored, whatever
        # nio makes of it: a redacted encrypted event is still one.
        encrypted = export_event.get("type") == "m.room.encrypted"
        received = Event.parse_event(export_event)
        if isinstance(received, BadEvent | UnknownBadEvent):
            return self._bad_event(export_event, encrypted), None
        if not isinstance(received, MegolmEvent):
            # Sent in clear (in an encrypted room, by the homeserver or a
            # client that ignores the room's encryption), or redacted.
            export_message = matrix_client._build_event_message(received)
            if not isinstance(received, RedactedEvent):
                self._with_relation(export_message, export_event)
            export_message["encrypted"] = encrypted
            return export_message, None
        try:
            decrypted = self.client.olm.decrypt_megolm_event(received, room_id)
        except EncryptionError as error:
            export_message = matrix_client._build_event_message(received)
            export_message["encrypted"] = True
            if str(error).startswith(REPLAY_ERROR):
                # Not a missing key: another event already used this index.
                del export_message["undecryptable"]
                export_message["replay"] = True
            return export_message, None
        if isinstance(decrypted, BadEvent | UnknownBadEvent):
            return self._bad_event(export_event, encrypted=True), None
        export_message = self._with_relation(
            matrix_client._build_event_message(decrypted), decrypted.source
        )
        export_message.update(
            {
                "encrypted": True,
                "verified": bool(decrypted.verified),
                "device_id": received.device_id,
                "sender_key": received.sender_key,
            }
        )
        # The signing key of the device that shared the room key, as the Olm
        # session it came over vouches for it: what ties this message to a
        # device, and so to an identity that signs that device. Not exported.
        session = self.client.olm.inbound_group_store.get(
            room_id, received.sender_key, received.session_id
        )
        # A key forwarded to the bot, or restored from a backup, says only
        # what its forwarder claims of the sender: nothing to vouch with.
        export_message[SIGNING_KEY] = (
            None if session is None or session.forwarding_chain else session.ed25519
        )
        encrypted_file = None
        if isinstance(decrypted, RoomEncryptedMedia):
            encrypted_file = decrypted.source.get("content", {}).get("file")
        return export_message, encrypted_file

    @staticmethod
    def _with_relation(export_message, source):
        # A reaction already carries what it annotates.
        if export_message.get("type") != "m.reaction":
            relation = _relation(source.get("content"))
            if relation:
                export_message["relation"] = relation
        return export_message

    @staticmethod
    def _bad_event(export_event, encrypted):
        """A marker for an event that cannot be read as what it says it is: its
        payload, decrypted or not, is malformed."""
        return {
            "event_id": str(export_event.get("event_id", "")),
            "sender": str(export_event.get("sender", "")),
            "timestamp": export_event.get("origin_server_ts"),
            "type": str(export_event.get("type", "")),
            "bad_event": True,
            "encrypted": encrypted,
        }

    async def _collect_media(self, spool_key, source, destination, data_key, media):
        """Download and decrypt each message's attachment into the media
        archive, encrypted under ``data_key`` into ``media``, and write the
        messages, marked with where their attachment went, to
        ``destination``; the number of attachments archived."""
        processed = export_files.EncryptingWriter(
            spool_key, export_files.KIND_SPOOL, destination
        )
        archive_file = export_files.EncryptingWriter(
            data_key, export_files.KIND_MEDIA, media
        )
        media_count = 0
        total = 0
        with zipfile.ZipFile(archive_file, "w", zipfile.ZIP_DEFLATED) as archive:
            for export_record in _read_lines(spool_key, source):
                export_message = export_record["m"]
                if export_message.get("has_media") and export_message.get("media_url"):
                    added = await self._archive_media(
                        archive, export_message, export_record["f"], total
                    )
                    if added is not None:
                        total += added
                        media_count += 1
                processed.write(
                    json.dumps({"m": export_message}, default=str).encode() + b"\n"
                )
        archive_file.close()
        processed.close()
        return media_count

    async def _archive_media(self, archive, export_message, encrypted_file, total):
        """Add the message's attachment to the archive and mark the message;
        the attachment's size, or None if it is not exported."""
        await self._checkpoint()
        size_limit = min(MAX_MEDIA_FILE_BYTES, MAX_MEDIA_TOTAL_BYTES - total)
        if size_limit <= 0:
            export_message["media_error"] = MEDIA_LIMIT_REACHED
            return None
        try:
            async with self._holding_memory():
                plaintext = await self._media(
                    export_message, encrypted_file, size_limit
                )
                name = archive_name(
                    export_message["event_id"], export_message.get("body")
                )
                await asyncio.to_thread(archive.writestr, name, plaintext)
                size = len(plaintext)
                del plaintext
        except MediaUnavailable as error:
            reason = str(error)
            if reason == MEDIA_TOO_LARGE and size_limit < MAX_MEDIA_FILE_BYTES:
                reason = MEDIA_LIMIT_REACHED
            export_message["media_error"] = reason
            logger.warning(
                "Media of event %s not exported: %s", export_message["event_id"], reason
            )
            return None
        export_message["media_path"] = name
        return size

    async def _media(self, export_message, encrypted_file, size_limit):
        """The plaintext of a message's attachment."""
        if not export_message.get("media_encrypted"):
            return await self._download(export_message["media_url"], size_limit)
        parsed = attachments.parse_encrypted_file(encrypted_file)
        if parsed is None or parsed.url != export_message["media_url"]:
            raise MediaUnavailable(MEDIA_UNDECRYPTABLE)
        ciphertext = await self._download(parsed.url, size_limit)
        try:
            return await asyncio.to_thread(
                attachments.decrypt_attachment, ciphertext, parsed
            )
        except attachments.AttachmentError:
            raise MediaUnavailable(MEDIA_UNDECRYPTABLE) from None

    async def _download(self, mxc, size_limit):
        """Download from the homeserver's media repository, at most ``size_limit``
        bytes: a larger file is not read past the limit."""
        parts = attachments.parse_mxc(mxc)
        if parts is None:
            raise MediaUnavailable(MEDIA_DOWNLOAD_FAILED)
        server_name, media_id = parts
        url = (
            f"{self.homeserver_url}/_matrix/client/v1/media/download/"
            f"{quote(server_name, safe='')}/{quote(media_id, safe='')}"
        )
        received = bytearray()
        try:
            async with asyncio.timeout(MEDIA_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(timeout=30) as http:
                    async with http.stream(
                        "GET", url, headers=self._auth()
                    ) as media_response:
                        if media_response.status_code != 200:
                            raise MediaUnavailable(MEDIA_DOWNLOAD_FAILED)
                        declared = media_response.headers.get("content-length", "")
                        if declared.isdigit() and int(declared) > size_limit:
                            raise MediaUnavailable(MEDIA_TOO_LARGE)
                        async for chunk in media_response.aiter_bytes():
                            received += chunk
                            if len(received) > size_limit:
                                raise MediaUnavailable(MEDIA_TOO_LARGE)
        except (httpx.HTTPError, TimeoutError):
            raise MediaUnavailable(MEDIA_DOWNLOAD_FAILED) from None
        # Bytes-like is enough for hashing, decrypting and the archive.
        return received
