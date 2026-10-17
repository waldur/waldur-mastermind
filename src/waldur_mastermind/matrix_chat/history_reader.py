"""Recent messages of a room, read and decrypted by the bot, for its commands.

A command that works on the conversation it is used in (``!ticket``) needs the
messages around it: those of its thread, or those before the message it
replies to. Rooms are encrypted and only the bot holds their keys, so this runs
in the bot process, with the pager history exports use, and what it reads
stays in memory.

Only text messages are read; commands, the bot's own messages, and anything
no key the bot holds decrypts are left out. An edit replaces what it edits,
when its sender sent the original.

Decrypted content is never logged. The locals that hold it are named so that
error reports scrub them (``waldur_core.logging.sentry``).
"""

import dataclasses
import logging

from nio import Event, MegolmEvent, RoomMessageText
from nio.exceptions import EncryptionError

from waldur_mastermind.matrix_chat import bot_export, matrix_client

logger = logging.getLogger(__name__)

# One message is cut to this many characters; a draft needs the gist.
MAX_MESSAGE_CHARS = 2000
# Pages read at most to find the messages before an event.
MAX_PAGES = 5
MAX_THREAD_MESSAGES = 100


@dataclasses.dataclass(frozen=True)
class HistoryMessage:
    event_id: str
    sender: str
    # Out of its repr: error reports show the repr of frame locals.
    body: str = dataclasses.field(repr=False)
    timestamp: int = 0


class HistoryReader:
    """Reads one room's recent text messages with the bot's token and keys."""

    def __init__(self, client, homeserver_url, bot_user_id):
        self.client = client
        self.homeserver_url = homeserver_url
        self.bot_user_id = bot_user_id

    async def thread(self, room_id, root_id, limit):
        """The thread's root and up to ``limit`` of its latest messages,
        oldest first."""
        path = bot_export.room_path(room_id, "relations", root_id, "m.thread")
        history_events = []
        start = ""
        while len(history_events) < min(limit, MAX_THREAD_MESSAGES):
            params = {"dir": "b", "limit": bot_export.PAGE_SIZE}
            if start:
                params["from"] = start
            # Relations are served under v1 only.
            history_page = await bot_export.read_json(
                self.client, self.homeserver_url, path, params, version="v1"
            )
            history_chunk = history_page.get("chunk")
            if not isinstance(history_chunk, list) or not history_chunk:
                break
            history_events.extend(history_chunk)
            start = history_page.get("next_batch")
            if not isinstance(start, str) or not start:
                break
        history_root = await bot_export.read_json(
            self.client,
            self.homeserver_url,
            bot_export.room_path(room_id, "event", root_id),
            None,
        )
        return self._messages(room_id, [history_root], 1) + self._messages(
            room_id, list(reversed(history_events)), limit
        )

    async def before(self, room_id, event_id, limit):
        """Up to ``limit`` messages ending with ``event_id``, oldest first."""
        # How the homeserver splits the limit between the events before and
        # after is up to it; both are taken, and ``start`` pages on from the
        # earliest of those before.
        history_context = await bot_export.read_json(
            self.client,
            self.homeserver_url,
            bot_export.room_path(room_id, "context", event_id),
            {"limit": min(2 * limit, bot_export.PAGE_SIZE)},
        )
        history_event = history_context.get("event")
        history_events = [history_event] if isinstance(history_event, dict) else []
        history_chunk = history_context.get("events_before")
        if isinstance(history_chunk, list):
            history_events.extend(history_chunk)
        start = history_context.get("start")
        pages = 0
        while (
            isinstance(start, str)
            and start
            and pages < MAX_PAGES
            and len(history_events) < limit * 3
        ):
            history_chunk, end = await bot_export.read_page(
                self.client, self.homeserver_url, room_id, start
            )
            pages += 1
            history_events.extend(history_chunk)
            if not history_chunk or not end or end == start:
                break
            start = end
        # Newest first so far; edits come after what they edit.
        return self._messages(room_id, list(reversed(history_events)), limit)

    def _messages(self, room_id, history_events, limit):
        """The text messages among ``history_events`` (oldest first), with
        edits applied, the latest ``limit`` of them."""
        history_messages = {}
        for history_event in history_events:
            decrypted = self._decrypt(history_event, room_id)
            if decrypted is None or decrypted.sender == self.bot_user_id:
                continue
            decrypted_content = decrypted.source.get("content") or {}
            relates_to = decrypted_content.get("m.relates_to")
            if isinstance(relates_to, dict) and relates_to.get("rel_type") == (
                "m.replace"
            ):
                edited_content = decrypted_content.get("m.new_content")
                history_message = history_messages.get(relates_to.get("event_id"))
                if (
                    history_message is not None
                    and history_message.sender == decrypted.sender
                    and isinstance(edited_content, dict)
                    and isinstance(edited_content.get("body"), str)
                ):
                    history_messages[history_message.event_id] = dataclasses.replace(
                        history_message,
                        body=edited_content["body"][:MAX_MESSAGE_CHARS],
                    )
                continue
            plaintext = matrix_client.strip_reply_fallback(
                decrypted.body, decrypted_content
            ).strip()
            if not plaintext or plaintext.startswith("!"):
                continue
            history_messages[decrypted.event_id] = HistoryMessage(
                event_id=decrypted.event_id,
                sender=decrypted.sender,
                body=plaintext[:MAX_MESSAGE_CHARS],
                timestamp=decrypted.server_timestamp,
            )
        return list(history_messages.values())[-limit:] if limit else []

    def _decrypt(self, history_event, room_id):
        """The text message ``history_event`` is, decrypted; None for anything
        else."""
        if not isinstance(history_event, dict):
            return None
        decrypted = Event.parse_event(history_event)
        if isinstance(decrypted, MegolmEvent):
            try:
                decrypted = self.client.olm.decrypt_megolm_event(decrypted, room_id)
            except EncryptionError:
                return None
        if isinstance(decrypted, RoomMessageText):
            return decrypted
        return None
