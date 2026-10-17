import asyncio
import threading
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from nio import RoomMessageText, RoomSendResponse

from waldur_mastermind.matrix_chat import bot, tasks, tickets

BOT_USER = "@waldur-bot:matrix.example.com"
ROOM_ID = "!room:matrix.example.com"


def _bot():
    instance = bot.MatrixBot(
        "holder",
        bot.BotSettings(
            homeserver_url="https://matrix.example.com",
            as_token="as-token",
            localpart="waldur-bot",
            user_id=BOT_USER,
            display_name="Waldur Bot",
        ),
    )
    instance.client = mock.Mock()
    instance.client.rooms = {ROOM_ID: mock.Mock(encrypted=True, users={})}
    return instance


def _event(body, event_id="$cmd", relates_to=None, sender="@member:matrix.example.com"):
    content = {"msgtype": "m.text", "body": body}
    if relates_to:
        content["m.relates_to"] = relates_to
    return RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 0,
            "content": content,
        }
    )


def _sync_response(*events):
    room = mock.Mock()
    room.timeline.events = list(events)
    response = mock.Mock()
    response.rooms.join = {ROOM_ID: room}
    return response


class DispatchTest(SimpleTestCase):
    def test_the_sync_loop_does_not_wait_for_a_command(self):
        instance = _bot()

        async def run():
            release = asyncio.Event()
            answered = []

            async def answer(room_id, event):
                await release.wait()
                answered.append(event.event_id)

            with mock.patch.object(instance, "_answer", answer):
                await instance._handle_timeline(_sync_response(_event("!ticket")))
                # The sync step is over while the command still runs.
                self.assertEqual(answered, [])
                self.assertEqual(len(instance._commands), 1)
                release.set()
                await asyncio.gather(*instance._commands)
            return answered

        self.assertEqual(async_to_sync(run)(), ["$cmd"])

    def test_a_reply_with_a_fallback_is_answered(self):
        instance = _bot()
        event = _event(
            "> <@other:matrix.example.com> it broke\n\n!ticket",
            relates_to={"m.in_reply_to": {"event_id": "$quoted"}},
        )
        with mock.patch.object(instance, "_answer", mock.AsyncMock()) as answer:

            async def run():
                await instance._handle_timeline(_sync_response(event))
                await asyncio.gather(*instance._commands)

            async_to_sync(run)()
        answer.assert_awaited_once_with(ROOM_ID, event)

    def test_drafts_do_not_hold_up_quick_commands(self):
        instance = _bot()

        async def run():
            release = asyncio.Event()
            answered = []

            async def slow_draft(room_id, command_event, command):
                await release.wait()

            def answer(room_id, sender, event_id, body, content):
                answered.append(event_id)

            with (
                mock.patch.object(instance, "draft_ticket", slow_draft),
                mock.patch.object(tasks, "answer_command", answer),
            ):
                drafts = [
                    asyncio.create_task(
                        instance._answer(
                            ROOM_ID,
                            _event("!ticket", f"$d{i}", sender=f"@m{i}:x"),
                        )
                    )
                    for i in range(bot.DRAFT_SLOTS + 2)
                ]
                await asyncio.sleep(0)
                await instance._answer(ROOM_ID, _event("!help", "$help"))
                release.set()
                await asyncio.gather(*drafts)
            return answered

        self.assertEqual(async_to_sync(run)(), ["$help"])

    def test_one_draft_at_a_time_per_member_and_room(self):
        instance = _bot()

        async def run():
            release = asyncio.Event()
            started = []

            async def slow_draft(room_id, command_event, command):
                started.append(command_event.event_id)
                await release.wait()

            with (
                mock.patch.object(instance, "draft_ticket", slow_draft),
                mock.patch.object(tickets, "busy") as busy,
            ):
                first = asyncio.create_task(
                    instance._answer(ROOM_ID, _event("!ticket", "$one"))
                )
                await asyncio.sleep(0)
                await instance._answer(ROOM_ID, _event("!ticket", "$two"))
                release.set()
                await first
                await instance._answer(ROOM_ID, _event("!ticket", "$three"))
            return started, busy

        started, busy = async_to_sync(run)()
        self.assertEqual(started, ["$one", "$three"])
        # The one in between is told why it waits.
        busy.assert_called_once()
        self.assertEqual(busy.call_args.args[:2], (ROOM_ID, "$two"))

    def test_a_flood_of_commands_is_dropped(self):
        instance = _bot()
        instance._commands = set(range(bot.MAX_PENDING_COMMANDS))
        with mock.patch.object(bot.asyncio, "create_task") as create_task:
            instance._start_command(ROOM_ID, _event("!help"))
        create_task.assert_not_called()

    def test_other_commands_are_answered_from_a_worker(self):
        instance = _bot()
        with mock.patch.object(tasks, "answer_command") as answer:
            async_to_sync(instance._answer)(ROOM_ID, _event("!ticket send"))
        args = answer.call_args.args
        self.assertEqual(
            args[:4], (ROOM_ID, "@member:matrix.example.com", "$cmd", "!ticket send")
        )

    def test_a_failing_command_does_not_stop_the_bot(self):
        instance = _bot()
        with mock.patch.object(tasks, "answer_command", side_effect=RuntimeError()):
            async_to_sync(instance._answer)(ROOM_ID, _event("!status"))


REQUEST = tickets.TicketRequest(
    room_pk=1,
    room_id=ROOM_ID,
    requester_pk=2,
    requester_matrix_id="@member:matrix.example.com",
    event_id="$cmd",
    thread_root="$cmd",
    reply_to="$quoted",
    in_thread=False,
    args="",
    context_messages=20,
)
PREVIEW = tickets.Preview(draft_uuid="d" * 32, content={"body": "draft"})


@mock.patch.object(bot.history_reader, "HistoryReader")
@mock.patch.object(tickets, "posted")
@mock.patch.object(tickets, "discard")
@mock.patch.object(tickets, "fail")
@mock.patch.object(tickets, "draft", return_value=PREVIEW)
@mock.patch.object(tickets, "start", return_value=REQUEST)
class DraftTicketTest(SimpleTestCase):
    def _run(self, instance, command=None):
        command = command or tasks.parse_command("!ticket")
        async_to_sync(instance.draft_ticket)(ROOM_ID, _event("!ticket"), command)

    def _reader(self, reader_class, messages=()):
        reader = reader_class.return_value
        reader.before = mock.AsyncMock(return_value=list(messages))
        reader.thread = mock.AsyncMock(return_value=list(messages))
        return reader

    def test_reads_before_the_replied_message_and_posts_the_preview(
        self, start, draft, fail, discard, posted, reader_class
    ):
        reader = self._reader(reader_class, ["m"])
        instance = _bot()
        instance._send_content = mock.AsyncMock(
            return_value=RoomSendResponse("$preview", ROOM_ID)
        )
        self._run(instance)
        reader.before.assert_awaited_once_with(ROOM_ID, "$quoted", 20)
        draft.assert_called_once_with(REQUEST, ["m"])
        instance._send_content.assert_awaited_once_with(ROOM_ID, PREVIEW.content)
        posted.assert_called_once_with(PREVIEW.draft_uuid, "$preview")
        fail.assert_not_called()

    def test_reads_the_thread_in_a_thread(
        self, start, draft, fail, discard, posted, reader_class
    ):
        start.return_value = tickets.TicketRequest(
            **{**REQUEST.__dict__, "in_thread": True, "thread_root": "$root"}
        )
        reader = self._reader(reader_class)
        instance = _bot()
        instance._send_content = mock.AsyncMock(
            return_value=RoomSendResponse("$preview", ROOM_ID)
        )
        self._run(instance)
        reader.thread.assert_awaited_once_with(ROOM_ID, "$root", 20)

    def test_nothing_is_read_for_a_refused_command(
        self, start, draft, fail, discard, posted, reader_class
    ):
        start.return_value = None
        self._run(_bot())
        reader_class.assert_not_called()
        draft.assert_not_called()

    def test_a_preview_that_cannot_be_posted_drops_the_draft(
        self, start, draft, fail, discard, posted, reader_class
    ):
        self._reader(reader_class)
        instance = _bot()
        instance._send_content = mock.AsyncMock(return_value=RuntimeError())
        with mock.patch.object(bot, "PREVIEW_RETRY_SECONDS", 0):
            self._run(instance)
        self.assertEqual(instance._send_content.await_count, bot.PREVIEW_ATTEMPTS)
        discard.assert_called_once_with(PREVIEW.draft_uuid)
        fail.assert_called_once_with(REQUEST)

    def test_nothing_is_posted_in_clear(
        self, start, draft, fail, discard, posted, reader_class
    ):
        self._reader(reader_class)
        instance = _bot()
        instance.client.rooms = {ROOM_ID: mock.Mock(encrypted=False)}
        instance._send_content = mock.AsyncMock()
        self._run(instance)
        instance._send_content.assert_not_awaited()
        # Nothing is read, and the model is not asked, for a draft that could
        # not be posted.
        reader_class.assert_not_called()
        draft.assert_not_called()
        fail.assert_called_once_with(REQUEST)

    def _stop_while(self, instance, blocked):
        """Cancel ``draft_ticket``, as stopping the bot does, once it waits in
        ``blocked``, which this replaces; the cancellation must come out."""

        async def run():
            waiting = asyncio.Event()

            async def block(*args, **kwargs):
                waiting.set()
                await asyncio.Event().wait()

            blocked(block)
            task = asyncio.create_task(
                instance.draft_ticket(
                    ROOM_ID, _event("!ticket"), tasks.parse_command("!ticket")
                )
            )
            await waiting.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        async_to_sync(run)()

    def test_a_stop_while_reading_tells_the_requester(
        self, start, draft, fail, discard, posted, reader_class
    ):
        reader = self._reader(reader_class)
        self._stop_while(_bot(), lambda block: setattr(reader, "before", block))
        draft.assert_not_called()
        fail.assert_called_once_with(REQUEST)

    def test_a_stop_while_posting_drops_the_draft(
        self, start, draft, fail, discard, posted, reader_class
    ):
        self._reader(reader_class)
        instance = _bot()
        self._stop_while(
            instance, lambda block: setattr(instance, "_send_content", block)
        )
        discard.assert_called_once_with(PREVIEW.draft_uuid)
        fail.assert_called_once_with(REQUEST)
        posted.assert_not_called()

    def _stop_in_worker(self, instance, worker_mock, result):
        """Cancel ``draft_ticket`` while ``worker_mock`` runs in its worker
        thread, which goes on and returns ``result`` after the cancel."""
        started, release = threading.Event(), threading.Event()

        def slow(*args):
            started.set()
            release.wait(5)
            return result

        worker_mock.side_effect = slow

        async def run():
            task = asyncio.create_task(
                instance.draft_ticket(
                    ROOM_ID, _event("!ticket"), tasks.parse_command("!ticket")
                )
            )
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        async_to_sync(run)()

    def test_a_stop_while_the_model_drafts_drops_the_draft(
        self, start, draft, fail, discard, posted, reader_class
    ):
        # The thread asking the model cannot be stopped, and stores the
        # draft after the stop: it must not be left pending, unseen.
        self._reader(reader_class)
        self._stop_in_worker(_bot(), draft, PREVIEW)
        discard.assert_called_once_with(PREVIEW.draft_uuid)
        fail.assert_called_once_with(REQUEST)
        posted.assert_not_called()

    def test_a_stop_while_the_command_is_claimed_tells_the_requester(
        self, start, draft, fail, discard, posted, reader_class
    ):
        self._stop_in_worker(_bot(), start, REQUEST)
        reader_class.assert_not_called()
        fail.assert_called_once_with(REQUEST)

    def test_unreadable_history_tells_the_requester(
        self, start, draft, fail, discard, posted, reader_class
    ):
        reader = self._reader(reader_class)
        reader.before.side_effect = bot.bot_export.HistoryError("500")
        self._run(_bot())
        draft.assert_not_called()
        fail.assert_called_once_with(REQUEST)
