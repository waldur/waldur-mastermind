import asyncio
import base64
import contextlib
import json
import logging
import types
import zipfile
from io import BytesIO
from unittest import mock

import httpx
from asgiref.sync import async_to_sync, sync_to_async
from django.test import TestCase, override_settings
from nio import Event
from nio.exceptions import EncryptionError

from waldur_mastermind.matrix_chat import bot, bot_export, bot_state, models, tasks
from waldur_mastermind.matrix_chat.crypto import cross_signing, export_files
from waldur_mastermind.matrix_chat.tests import fixtures
from waldur_mastermind.matrix_chat.tests import test_bot as bot_test
from waldur_mastermind.matrix_chat.tests import test_recovery_keys as recovery_vectors
from waldur_mastermind.matrix_chat.tests.test_attachments import encrypt
from waldur_mastermind.matrix_chat.tests.test_bot import _settings
from waldur_mastermind.matrix_chat.tests.test_cross_signing import Identity

ROOM_ID = "!test_room:matrix.example.com"
HOMESERVER = "https://matrix.example.com"


def raw(event_id, content, event_type="m.room.message", sender=None):
    return {
        "event_id": event_id,
        "sender": sender or "@alice:matrix.example.com",
        "origin_server_ts": 1234567890,
        "type": event_type,
        "content": content,
    }


def megolm(event_id, session_id="session-1"):
    return raw(
        event_id,
        {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "AwgAEnA",
            "sender_key": "sender-curve-key",
            "session_id": session_id,
            "device_id": "ALICEDEVICE",
        },
        event_type="m.room.encrypted",
    )


ALICE_ED25519 = "alice-device-ed25519"


class FakeOlm:
    """Decrypts the events it was given the plaintext of, as nio would."""

    def __init__(self):
        self.plaintexts = {}
        # The room key's sender, as the Olm session that brought it vouches.
        self.inbound_group_store = mock.Mock()
        self.inbound_group_store.get.return_value = mock.Mock(
            ed25519=ALICE_ED25519, forwarding_chain=[]
        )

    def add(self, event_id, content, verified=True, event_type="m.room.message"):
        self.plaintexts[event_id] = (event_type, content, verified)

    def decrypt_megolm_event(self, event, room_id):
        assert room_id == ROOM_ID
        if event.event_id not in self.plaintexts:
            raise EncryptionError("no session")
        event_type, content, verified = self.plaintexts[event.event_id]
        decrypted = Event.parse_decrypted_event(
            {
                "event_id": event.event_id,
                "sender": event.sender,
                "origin_server_ts": event.server_timestamp,
                "type": event_type,
                "content": content,
            }
        )
        decrypted.verified = verified
        return decrypted


OLM = FakeOlm()


def encrypted(event_id, content, verified=True, event_type="m.room.message"):
    OLM.add(event_id, content, verified, event_type)
    return megolm(event_id)


def text(event_id, body, **kwargs):
    return encrypted(event_id, {"msgtype": "m.text", "body": body}, **kwargs)


def image(event_id, url="mxc://matrix.example.com/plain"):
    return raw(
        event_id,
        {
            "msgtype": "m.image",
            "body": "photo.jpg",
            "url": url,
            "info": {"mimetype": "image/jpeg"},
        },
    )


def encrypted_image(event_id, encrypted_file, body="photo.jpg"):
    return encrypted(
        event_id,
        {
            "msgtype": "m.image",
            "body": body,
            "file": encrypted_file,
            "info": {
                "mimetype": "image/jpeg",
                "thumbnail_file": {**encrypted_file, "url": "mxc://hs.example/t"},
            },
        },
    )


def undecryptable(event_id):
    OLM.plaintexts.pop(event_id, None)
    return megolm(event_id, session_id="unknown-session")


def fake_client(*chunks):
    """A bot client whose /messages pages are ``chunks``, then an empty page."""
    client = mock.Mock(access_token="bot-token", olm=OLM)
    answers = [(list(chunk), f"t{i + 1}") for i, chunk in enumerate(chunks)]
    answers.append(([], None))
    client.export_pages = mock.AsyncMock(side_effect=answers)
    return client


def failing_client(error):
    client = mock.Mock(access_token="bot-token", olm=OLM)
    client.export_pages = mock.AsyncMock(side_effect=error)
    return client


def exporter_for(client, media=None, **kwargs):
    exporter = bot_export.RoomExporter(client, HOMESERVER, **kwargs)
    exporter._page = client.export_pages
    media = media or {}

    async def download(mxc, size_limit):
        if mxc not in media:
            raise bot_export.MediaUnavailable(bot_export.MEDIA_DOWNLOAD_FAILED)
        if len(media[mxc]) > size_limit:
            raise bot_export.MediaUnavailable(bot_export.MEDIA_TOO_LARGE)
        return media[mxc]

    exporter._download = download
    return exporter


def opened(files):
    """``(messages, media archive or None, media count)`` of an export's
    files, decrypted, as the old tests read them."""
    with files.messages as messages:
        document = json.loads(
            b"".join(
                export_files.decrypt_chunks(
                    files.data_key, export_files.KIND_MESSAGES, messages
                )
            )
        )
    media_zip = None
    if files.media is not None:
        with files.media as media:
            media_zip = BytesIO(
                b"".join(
                    export_files.decrypt_chunks(
                        files.data_key, export_files.KIND_MEDIA, media
                    )
                )
            )
    assert document["message_count"] == files.message_count
    assert document["media_count"] == files.media_count
    return document["messages"], media_zip, files.media_count


def run_exporter(client, media=None, export_media=True):
    exporter = exporter_for(client, media, export_media=export_media)
    return opened(async_to_sync(exporter.export)(ROOM_ID))


def stored_plaintext(export, field, kind):
    """A stored export file, decrypted."""
    data_key = base64.b64decode(export.data_key)
    with getattr(export, field).open("rb") as stored:
        return b"".join(export_files.decrypt_chunks(data_key, kind, stored))


def zip_contents(media_zip):
    with zipfile.ZipFile(media_zip) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class RoomExporterTest(TestCase):
    def test_reads_every_page_newest_first(self):
        client = fake_client([text("$2", "second")], [text("$1", "first")])
        messages, media_zip, media_count = run_exporter(client)
        self.assertEqual([m["body"] for m in messages], ["second", "first"])
        self.assertIsNone(media_zip)
        self.assertEqual(media_count, 0)
        starts = [c.args[1] for c in client.export_pages.call_args_list]
        self.assertEqual(starts, ["", "t1", "t2"])

    def test_an_undecryptable_event_is_marked_not_dropped(self):
        client = fake_client([text("$2", "readable"), undecryptable("$1")])
        messages, _, _ = run_exporter(client)
        self.assertEqual(len(messages), 2)
        self.assertEqual(
            messages[1],
            {
                "event_id": "$1",
                "sender": "@alice:matrix.example.com",
                "timestamp": 1234567890,
                "type": "m.room.encrypted",
                "undecryptable": True,
                "session_id": "unknown-session",
                "device_id": "ALICEDEVICE",
                "encrypted": True,
            },
        )

    def test_a_decrypted_event_says_whether_its_sender_is_verified(self):
        client = fake_client(
            [text("$2", "vouched for"), text("$1", "unverified", verified=False)]
        )
        messages, _, _ = run_exporter(client)
        for message, verified in zip(messages, (True, False)):
            self.assertTrue(message["encrypted"])
            self.assertIs(message["verified"], verified)
            self.assertEqual(message["device_id"], "ALICEDEVICE")
            self.assertEqual(message["sender_key"], "sender-curve-key")

    def test_an_event_sent_in_clear_is_marked(self):
        client = fake_client(
            [raw("$1", {"msgtype": "m.text", "body": "injected"}, sender="@bob:x")]
        )
        messages, _, _ = run_exporter(client)
        self.assertIs(messages[0]["encrypted"], False)
        self.assertNotIn("verified", messages[0])

    def test_a_redacted_encrypted_event_stays_marked_encrypted(self):
        redacted = raw("$gone", {}, event_type="m.room.encrypted")
        redacted["unsigned"] = {
            "redacted_because": {
                "event_id": "$redaction",
                "sender": "@bob:matrix.example.com",
                "origin_server_ts": 1234567899,
                "type": "m.room.redaction",
                "redacts": "$gone",
                "content": {"reason": "spam"},
            }
        }
        messages, _, _ = run_exporter(fake_client([redacted]))
        self.assertEqual(messages[0]["type"], "m.room.redacted")
        self.assertIs(messages[0]["encrypted"], True)

    def test_a_malformed_event_is_marked(self):
        OLM.add("$bad", {"msgtype": "m.image", "body": "no url"})
        client = fake_client(
            [megolm("$bad"), raw("$clear", {"msgtype": "m.file", "body": "x"})]
        )
        messages, _, _ = run_exporter(client)
        self.assertEqual(
            [(m["event_id"], m["bad_event"], m["encrypted"]) for m in messages],
            [("$bad", True, True), ("$clear", True, False)],
        )
        self.assertNotIn("body", messages[0])

    def test_edits_and_replies_keep_their_relation(self):
        client = fake_client(
            [
                encrypted(
                    "$edit",
                    {
                        "msgtype": "m.text",
                        "body": "* fixed",
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$1"},
                    },
                ),
                encrypted(
                    "$reply",
                    {
                        "msgtype": "m.text",
                        "body": "answer",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$1"}},
                    },
                ),
            ]
        )
        messages, _, _ = run_exporter(client)
        self.assertEqual(
            messages[0]["relation"], {"rel_type": "m.replace", "event_id": "$1"}
        )
        self.assertEqual(messages[1]["relation"], {"in_reply_to": "$1"})

    def test_encrypted_media_is_decrypted_into_the_archive(self):
        ciphertext, encrypted_file = encrypt(b"the photo")
        client = fake_client(
            [encrypted_image("$img", encrypted_file), text("$t", "hi")]
        )
        messages, media_zip, media_count = run_exporter(
            client, {encrypted_file["url"]: ciphertext}
        )
        self.assertEqual(media_count, 1)
        self.assertEqual(zip_contents(media_zip), {"_img_photo.jpg": b"the photo"})
        self.assertEqual(messages[0]["media_path"], "_img_photo.jpg")
        self.assertTrue(messages[0]["media_encrypted"])
        self.assertEqual(messages[0]["media_url"], encrypted_file["url"])
        # No key of the file or its thumbnail goes into the messages.
        dumped = json.dumps(messages)
        self.assertNotIn(encrypted_file["key"]["k"], dumped)
        self.assertNotIn("thumbnail_file", dumped)

    def test_an_altered_file_is_marked_not_exported(self):
        ciphertext, encrypted_file = encrypt(b"the photo")
        client = fake_client([encrypted_image("$img", encrypted_file)])
        messages, media_zip, media_count = run_exporter(
            client, {encrypted_file["url"]: b"X" + ciphertext[1:]}
        )
        self.assertEqual(media_count, 0)
        self.assertIsNone(media_zip)
        self.assertEqual(messages[0]["media_error"], bot_export.MEDIA_UNDECRYPTABLE)
        self.assertNotIn("media_path", messages[0])

    def test_a_malformed_file_object_is_marked(self):
        _, encrypted_file = encrypt(b"the photo")
        bad = {**encrypted_file, "v": "v1"}
        client = fake_client([encrypted_image("$img", bad)])
        messages, _, media_count = run_exporter(client, {bad["url"]: b"x"})
        self.assertEqual(media_count, 0)
        self.assertEqual(messages[0]["media_error"], bot_export.MEDIA_UNDECRYPTABLE)

    def test_unencrypted_media_is_exported_as_it_is(self):
        client = fake_client([image("$img")])
        messages, media_zip, media_count = run_exporter(
            client, {"mxc://matrix.example.com/plain": b"jpeg"}
        )
        self.assertEqual(media_count, 1)
        self.assertEqual(zip_contents(media_zip), {"_img_photo.jpg": b"jpeg"})
        self.assertNotIn("media_encrypted", messages[0])

    def test_a_failed_download_does_not_fail_the_export(self):
        client = fake_client([image("$img", "mxc://matrix.example.com/broken")])
        messages, media_zip, media_count = run_exporter(client)
        self.assertEqual(media_count, 0)
        self.assertIsNone(media_zip)
        self.assertEqual(messages[0]["media_error"], bot_export.MEDIA_DOWNLOAD_FAILED)

    def test_media_is_skipped_when_disabled(self):
        client = fake_client([image("$img")])
        messages, media_zip, media_count = run_exporter(client, export_media=False)
        self.assertEqual(media_count, 0)
        self.assertIsNone(media_zip)
        self.assertNotIn("media_error", messages[0])

    def test_media_past_the_limits_is_marked(self):
        client = fake_client(
            [image("$big", "mxc://hs.example/big")],
            [image("$a", "mxc://hs.example/a"), image("$b", "mxc://hs.example/b")],
        )
        with (
            mock.patch.object(bot_export, "MAX_MEDIA_FILE_BYTES", 10),
            mock.patch.object(bot_export, "MAX_MEDIA_TOTAL_BYTES", 15),
        ):
            messages, _, media_count = run_exporter(
                client,
                {
                    "mxc://hs.example/big": b"x" * 11,
                    "mxc://hs.example/a": b"x" * 8,
                    "mxc://hs.example/b": b"x" * 8,
                },
            )
        self.assertEqual(media_count, 1)
        errors = {m["event_id"]: m.get("media_error") for m in messages}
        self.assertEqual(
            errors,
            {
                "$big": bot_export.MEDIA_TOO_LARGE,
                "$a": None,
                "$b": bot_export.MEDIA_LIMIT_REACHED,
            },
        )

    def test_a_homeserver_error_fails_the_export(self):
        client = failing_client(bot_export.ExportError("403 M_FORBIDDEN Not in room"))
        with self.assertRaisesRegex(bot_export.ExportError, "Not in room"):
            run_exporter(client)

    def test_an_export_gives_way_when_asked(self):
        client = fake_client([text("$2", "two")], [text("$1", "one")])
        answers = iter([False, True])
        exporter = exporter_for(
            client,
            export_media=False,
            should_yield=mock.AsyncMock(side_effect=lambda: next(answers)),
        )
        with self.assertRaises(bot_export.Preempted):
            async_to_sync(exporter.export)(ROOM_ID)
        self.assertEqual(client.export_pages.await_count, 1)

    def test_an_export_past_its_time_fails(self):
        client = fake_client([text("$1", "one")], [text("$0", "zero")])
        clock = iter([0, 1, bot_export.EXPORT_TIMEOUT_SECONDS + 1])
        exporter = exporter_for(client, export_media=False, now=lambda: next(clock))
        with self.assertRaises(bot_export.ExportError):
            async_to_sync(exporter.export)(ROOM_ID)


class DownloadTest(TestCase):
    def _download(self, handler, size_limit=100):
        real_client = httpx.AsyncClient
        seen = []

        def record(request):
            seen.append(request)
            return handler(request)

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(record), **kwargs)

        exporter = bot_export.RoomExporter(
            mock.Mock(access_token="bot-token"), HOMESERVER, export_media=True
        )
        with mock.patch.object(bot_export.httpx, "AsyncClient", client):
            result = async_to_sync(exporter._download)(
                "mxc://matrix.example.com/abc", size_limit
            )
        return result, seen

    def test_downloads_through_authenticated_media(self):
        body, seen = self._download(lambda request: httpx.Response(200, content=b"x"))
        self.assertEqual(body, b"x")
        self.assertEqual(
            str(seen[0].url),
            f"{HOMESERVER}/_matrix/client/v1/media/download/matrix.example.com/abc",
        )
        self.assertEqual(seen[0].headers["authorization"], "Bearer bot-token")

    def test_stops_reading_past_the_limit(self):
        with self.assertRaisesRegex(bot_export.MediaUnavailable, "too large"):
            self._download(
                lambda request: httpx.Response(200, content=b"x" * 101), size_limit=100
            )

    def test_an_error_status_is_a_failed_download(self):
        with self.assertRaisesRegex(bot_export.MediaUnavailable, "download failed"):
            self._download(lambda request: httpx.Response(404))

    def test_a_connection_error_is_a_failed_download(self):
        def fail(request):
            raise httpx.ConnectError("unreachable")

        with self.assertRaisesRegex(bot_export.MediaUnavailable, "download failed"):
            self._download(fail)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class BotExportTest(TestCase):
    def setUp(self):
        self.room = fixtures.MatrixChatFixture().matrix_room
        self.export = models.MatrixHistoryExport.objects.create(
            room=self.room, export_type=models.ExportTypes.MANUAL
        )
        self.bot = bot.MatrixBot("holder", _settings())
        self.bot._renew_lease = mock.AsyncMock()

    def _run(self, client, media=None, export_media=True):
        self.bot.client = client
        claimed = bot_state.claim_next_export()
        exporter_class = bot_export.RoomExporter

        def exporter(*args, **kwargs):
            instance = exporter_class(*args, **kwargs)
            instance._page = client.export_pages
            instance._download = mock.AsyncMock(
                side_effect=lambda mxc, limit: (media or {})[mxc]
            )
            return instance

        with (
            mock.patch.object(bot_export, "RoomExporter", exporter),
            mock.patch("waldur_mastermind.matrix_chat.bot.config") as config,
        ):
            config.MATRIX_EXPORT_MEDIA = export_media
            async_to_sync(self.bot.export)(claimed)
        self.export.refresh_from_db()

    def test_writes_the_export_in_its_format(self):
        ciphertext, encrypted_file = encrypt(b"the photo")
        self._run(
            fake_client(
                [encrypted_image("$img", encrypted_file), text("$t", "hello")],
                [undecryptable("$old")],
            ),
            {encrypted_file["url"]: ciphertext},
        )
        self.assertEqual(self.export.state, models.ExportStates.COMPLETED)
        self.assertEqual(self.export.message_count, 3)
        self.assertEqual(self.export.media_count, 1)
        self.assertIsNotNone(self.export.completed_at)
        data = json.loads(
            stored_plaintext(self.export, "export_file", export_files.KIND_MESSAGES)
        )
        self.assertEqual(
            set(data),
            {
                "room_id",
                "room_name",
                "exported_at",
                "message_count",
                "media_count",
                "messages",
            },
        )
        self.assertEqual(data["room_id"], ROOM_ID)
        self.assertEqual(data["message_count"], 3)
        self.assertEqual(
            [m["event_id"] for m in data["messages"]], ["$img", "$t", "$old"]
        )
        self.assertTrue(data["messages"][2]["undecryptable"])
        media = stored_plaintext(self.export, "media_file", export_files.KIND_MEDIA)
        with zipfile.ZipFile(BytesIO(media)) as archive:
            self.assertEqual(
                archive.read(data["messages"][0]["media_path"]), b"the photo"
            )

    def test_no_archive_without_media(self):
        self._run(fake_client([text("$t", "hello")]))
        self.assertEqual(self.export.state, models.ExportStates.COMPLETED)
        self.assertFalse(self.export.media_file)

    def test_a_failure_fails_the_export_without_its_content(self):
        self._run(failing_client(bot_export.ExportError("403 Not in room")))
        self.assertEqual(self.export.state, models.ExportStates.FAILED)
        self.assertIn("Not in room", self.export.error_message)

    def test_an_unexpected_error_is_reported_by_its_type_only(self):
        client = fake_client([text("$t", "secret message")])
        with mock.patch.object(
            tasks, "save_history_export", side_effect=ValueError("secret message")
        ):
            self._run(client)
        self.assertEqual(self.export.state, models.ExportStates.FAILED)
        self.assertEqual(self.export.error_message, "The export failed (ValueError).")

    def test_a_stop_puts_the_export_back(self):
        with self.assertRaises(asyncio.CancelledError):
            self._run(failing_client(asyncio.CancelledError))
        self.assertEqual(self.export.state, models.ExportStates.PENDING)
        self.assertIsNone(self.export.started_at)

    def test_an_export_gives_way_to_a_room_being_closed(self):
        closing_room = models.MatrixRoom.objects.create(
            room_id="!closing:matrix.example.com",
            state=models.RoomStates.DISABLING,
            content_type=self.room.content_type,
            object_id=self.room.object_id + 1000,
        )
        client = fake_client([text("$2", "two")], [text("$1", "one")])
        answers = client.export_pages.side_effect

        def close_a_room():
            models.MatrixHistoryExport.objects.get_or_create(
                room=closing_room, export_type=models.ExportTypes.ON_DELETION
            )

        async def close_a_room_meanwhile(*args):
            await sync_to_async(close_a_room)()
            return next(answers)

        client.export_pages.side_effect = close_a_room_meanwhile
        # Every slot is taken, so the room being closed has none to go to.
        self.bot._exports = {object(): None for _ in range(bot.EXPORT_SLOTS)}
        self._run(client)
        self.assertEqual(self.export.state, models.ExportStates.PENDING)
        self.assertIsNone(self.export.started_at)
        self.assertEqual(
            bot_state.claim_next_export().export_type,
            models.ExportTypes.ON_DELETION,
        )

    def _closing_meanwhile(self, client):
        """Have a room start closing while ``client``'s pages are read."""
        closing_room = models.MatrixRoom.objects.create(
            room_id="!closing:matrix.example.com",
            state=models.RoomStates.DISABLING,
            content_type=self.room.content_type,
            object_id=self.room.object_id + 1000,
        )
        answers = client.export_pages.side_effect

        def close_a_room():
            models.MatrixHistoryExport.objects.get_or_create(
                room=closing_room, export_type=models.ExportTypes.ON_DELETION
            )

        async def close_a_room_meanwhile(*args):
            await sync_to_async(close_a_room)()
            return next(answers)

        client.export_pages.side_effect = close_a_room_meanwhile
        return client

    def test_an_export_does_not_give_way_while_a_slot_is_free(self):
        self._run(
            self._closing_meanwhile(
                fake_client([text("$2", "two")], [text("$1", "one")])
            )
        )
        self.assertEqual(self.export.state, models.ExportStates.COMPLETED)

    def test_a_lost_claim_is_logged(self):
        with (
            mock.patch.object(tasks, "save_history_export", return_value=False),
            self.assertLogs(bot.logger, "WARNING") as logs,
        ):
            self._run(fake_client([text("$t", "hello")]))
        self.assertIn("no longer this process's", logs.output[0])

    def test_export_forever_fails_what_was_left_then_exports(self):
        left_behind = bot_state.claim_next_export()
        waiting = models.MatrixHistoryExport.objects.create(
            room=self.room, export_type=models.ExportTypes.MANUAL
        )
        self.bot._synced.set()
        exported = []

        async def export(claimed):
            exported.append(claimed.pk)
            raise asyncio.CancelledError

        with (
            mock.patch.object(self.bot, "export", export),
            self.assertRaises(asyncio.CancelledError),
        ):
            async_to_sync(self.bot.export_forever)()
        left_behind.refresh_from_db()
        self.assertEqual(left_behind.state, models.ExportStates.FAILED)
        self.assertEqual(exported, [waiting.pk])

    def test_an_export_taken_over_meanwhile_is_not_written(self):
        claimed = bot_state.claim_next_export()
        # Another process failed it, and claimed it again.
        bot_state.fail_interrupted_exports()
        models.MatrixHistoryExport.objects.filter(pk=claimed.pk).update(
            state=models.ExportStates.PENDING
        )
        again = bot_state.claim_next_export()
        files = async_to_sync(exporter_for(fake_client(), export_media=False).export)(
            ROOM_ID
        )
        self.assertTrue(tasks.save_history_export(again, files))
        self.assertFalse(tasks.save_history_export(claimed, files))


class ExportQueueTest(TestCase):
    def setUp(self):
        self.room = fixtures.MatrixChatFixture().matrix_room

    def _export(self, export_type=models.ExportTypes.PERIODIC):
        return models.MatrixHistoryExport.objects.create(
            room=self.room, export_type=export_type
        )

    def test_rooms_being_closed_go_first_then_people_then_the_daily_run(self):
        periodic = self._export()
        manual = self._export(models.ExportTypes.MANUAL)
        closing = self._export(models.ExportTypes.ON_DELETION)
        self.assertTrue(bot_state.deletion_export_waiting())
        self.assertEqual(bot_state.claim_next_export(), closing)
        self.assertFalse(bot_state.deletion_export_waiting())
        self.assertEqual(bot_state.claim_next_export(), manual)
        self.assertEqual(bot_state.claim_next_export(), periodic)
        self.assertIsNone(bot_state.claim_next_export())

    def test_an_unfinished_export_is_requested_once(self):
        first, created = bot_state.request_export(
            self.room, models.ExportTypes.PERIODIC
        )
        self.assertTrue(created)
        bot_state.claim_next_export()
        again, created = bot_state.request_export(
            self.room, models.ExportTypes.PERIODIC
        )
        self.assertEqual((again, created), (first, False))
        # Another kind of export is its own request.
        _, created = bot_state.request_export(self.room, models.ExportTypes.MANUAL)
        self.assertTrue(created)
        models.MatrixHistoryExport.objects.filter(pk=first.pk).update(
            state=models.ExportStates.COMPLETED
        )
        _, created = bot_state.request_export(self.room, models.ExportTypes.PERIODIC)
        self.assertTrue(created)

    def test_manual_exports_are_limited_per_day(self):
        for _ in range(bot_state.MANUAL_EXPORTS_PER_DAY):
            models.MatrixHistoryExport.objects.create(
                room=self.room,
                export_type=models.ExportTypes.MANUAL,
                state=models.ExportStates.COMPLETED,
            )
        with self.assertRaises(bot_state.ExportLimitReached):
            bot_state.request_export(self.room, models.ExportTypes.MANUAL)
        # The daily run and closing the room are never refused.
        bot_state.request_export(self.room, models.ExportTypes.PERIODIC)
        bot_state.request_export(self.room, models.ExportTypes.ON_DELETION)

    def test_an_export_is_claimed_once(self):
        self._export()
        claimed = bot_state.claim_next_export()
        claimed.refresh_from_db()
        self.assertEqual(claimed.state, models.ExportStates.EXPORTING)
        self.assertIsNotNone(claimed.started_at)
        self.assertIsNone(bot_state.claim_next_export())

    def test_interrupted_exports_fail(self):
        self._export()
        claimed = bot_state.claim_next_export()
        pending = self._export()
        self.assertEqual(bot_state.fail_interrupted_exports(), 1)
        claimed.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(claimed.state, models.ExportStates.FAILED)
        self.assertEqual(pending.state, models.ExportStates.PENDING)

    def test_wait_until_exported(self):
        export = self._export()
        with mock.patch.object(bot_state.time, "sleep"):
            self.assertIsNone(bot_state.wait_until_exported(export, 0))
        models.MatrixHistoryExport.objects.filter(pk=export.pk).update(
            state=models.ExportStates.COMPLETED
        )
        self.assertEqual(
            bot_state.wait_until_exported(export, 0), models.ExportStates.COMPLETED
        )


@mock.patch("waldur_mastermind.matrix_chat.tasks.config")
@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class DisableRoomExportTest(TestCase):
    def setUp(self):
        self.room = fixtures.MatrixChatFixture().matrix_room
        self.room.state = models.RoomStates.DISABLING
        self.room.save()

    def _disable(self, mock_client, mock_config, running=True, outcome=None):
        mock_client.is_enabled.return_value = True
        mock_config.MATRIX_HISTORY_EXPORT_ENABLED = True

        def finish(export, timeout_seconds):
            if outcome is not None:
                models.MatrixHistoryExport.objects.filter(pk=export.pk).update(
                    state=outcome, error_message="Not in room"
                )
                export.refresh_from_db()
            return outcome

        with (
            mock.patch.object(tasks.bot_state, "is_bot_running", return_value=running),
            mock.patch.object(tasks.bot_state, "wait_until_sent", return_value=True),
            mock.patch.object(
                tasks.bot_state, "wait_until_exported", side_effect=finish
            ) as wait,
        ):
            tasks.disable_room(str(self.room.uuid))
        self.room.refresh_from_db()
        return wait

    def test_the_room_is_archived_once_exported(self, mock_client, mock_config):
        wait = self._disable(
            mock_client, mock_config, outcome=models.ExportStates.COMPLETED
        )
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
        (export,) = self.room.exports.all()
        self.assertEqual(export.export_type, models.ExportTypes.ON_DELETION)
        wait.assert_called_once_with(export, tasks.EXPORT_WAIT_SECONDS)

    def test_no_running_bot_leaves_the_room_unarchived(self, mock_client, mock_config):
        wait = self._disable(mock_client, mock_config, running=False)
        wait.assert_not_called()
        self.assertEqual(self.room.state, models.RoomStates.ERROR)
        self.assertIn("bot is not running", self.room.error_message)
        # The request stays for the bot to fulfil once it runs.
        (export,) = self.room.exports.all()
        self.assertEqual(export.state, models.ExportStates.PENDING)

    def test_a_failed_export_leaves_the_room_unarchived(self, mock_client, mock_config):
        self._disable(mock_client, mock_config, outcome=models.ExportStates.FAILED)
        self.assertEqual(self.room.state, models.RoomStates.ERROR)
        self.assertIn("Not in room", self.room.error_message)

    def test_an_export_not_done_in_time_leaves_the_room_unarchived(
        self, mock_client, mock_config
    ):
        self._disable(mock_client, mock_config, outcome=None)
        self.assertEqual(self.room.state, models.RoomStates.ERROR)
        self.assertIn("in time", self.room.error_message)

    def test_an_export_the_bot_finished_meanwhile_is_used_not_asked_again(
        self, mock_client, mock_config
    ):
        # The bot was down at the first attempt and made the export later.
        self._disable(mock_client, mock_config, running=False)
        (export,) = self.room.exports.all()
        models.MatrixHistoryExport.objects.filter(pk=export.pk).update(
            state=models.ExportStates.COMPLETED
        )
        self.room.begin_disabling()
        self.room.save()
        wait = self._disable(mock_client, mock_config)
        wait.assert_not_called()
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
        self.assertEqual(self.room.exports.count(), 1)
        # The error of the first attempt does not outlive the archive, nor
        # does the export count for the next disable.
        self.assertEqual(self.room.error_message, "")
        self.assertIsNone(self.room.closing_export)

    def _redisable_from_active(self):
        self.room.refresh_from_db()
        self.room.begin_disabling()
        self.room.save(update_fields=["state", "closing_export"])

    def test_a_disable_after_a_retried_room_exports_again(
        self, mock_client, mock_config
    ):
        # The bot is down: the room goes to error with its export pending.
        self._disable(mock_client, mock_config, running=False)
        (old,) = self.room.exports.all()
        # Staff retry the room instead, and the bot makes the old export later.
        self.room.retry_creating()
        self.room.set_active()
        self.room.save()
        models.MatrixHistoryExport.objects.filter(pk=old.pk).update(
            state=models.ExportStates.COMPLETED
        )
        # Weeks later, it is disabled again: everything since must be exported.
        self._redisable_from_active()
        wait = self._disable(
            mock_client, mock_config, outcome=models.ExportStates.COMPLETED
        )
        wait.assert_called_once()
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
        self.assertEqual(self.room.exports.count(), 2)
        self.assertNotEqual(wait.call_args.args[0].pk, old.pk)

    def test_a_reactivated_room_drops_a_left_over_export(
        self, mock_client, mock_config
    ):
        # A second disable task left its export on the room after the first
        # one archived it.
        stale = models.MatrixHistoryExport.objects.create(
            room=self.room,
            export_type=models.ExportTypes.ON_DELETION,
            state=models.ExportStates.COMPLETED,
        )
        self.room.state = models.RoomStates.ARCHIVED
        self.room.closing_export = stale
        self.room.save()
        self.room.reactivate()
        self.room.save()
        self._redisable_from_active()
        self.assertIsNone(self.room.closing_export)
        wait = self._disable(
            mock_client, mock_config, outcome=models.ExportStates.COMPLETED
        )
        self.assertNotEqual(wait.call_args.args[0].pk, stale.pk)

    def test_a_disable_retried_from_error_keeps_its_export(
        self, mock_client, mock_config
    ):
        self._disable(mock_client, mock_config, running=False)
        (export,) = self.room.exports.all()
        self.room.begin_disabling()
        self.room.save(update_fields=["state", "closing_export"])
        self.room.refresh_from_db()
        self.assertEqual(self.room.closing_export, export)

    def _disable_while(self, mock_client, mock_config, meanwhile, outcome):
        """Disable the room while ``meanwhile(room)`` happens to it during the
        wait for its export, which then ends as ``outcome``."""

        def wait(export, timeout_seconds):
            meanwhile(models.MatrixRoom.objects.filter(pk=self.room.pk))
            models.MatrixHistoryExport.objects.filter(pk=export.pk).update(
                state=outcome, error_message="Not in room"
            )
            export.refresh_from_db()
            return outcome

        mock_client.is_enabled.return_value = True
        mock_config.MATRIX_HISTORY_EXPORT_ENABLED = True
        with (
            mock.patch.object(tasks.bot_state, "is_bot_running", return_value=True),
            mock.patch.object(tasks.bot_state, "wait_until_sent", return_value=True),
            mock.patch.object(tasks.bot_state, "wait_until_exported", side_effect=wait),
        ):
            tasks.disable_room(str(self.room.uuid))
        self.room.refresh_from_db()

    def test_a_failure_after_another_disable_archived_the_room_leaves_it(
        self, mock_client, mock_config
    ):
        self._disable_while(
            mock_client,
            mock_config,
            lambda rooms: rooms.update(state=models.RoomStates.ARCHIVED),
            models.ExportStates.FAILED,
        )
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
        self.assertEqual(self.room.error_message, "")

    def test_an_export_finished_after_another_disable_failed_archives(
        self, mock_client, mock_config
    ):
        self._disable_while(
            mock_client,
            mock_config,
            lambda rooms: rooms.update(
                state=models.RoomStates.ERROR, error_message="the other attempt"
            ),
            models.ExportStates.COMPLETED,
        )
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
        self.assertEqual(self.room.error_message, "")
        self.assertIsNone(self.room.closing_export)

    def test_an_export_finished_after_a_retry_moved_on_does_not_archive(
        self, mock_client, mock_config
    ):
        # Another attempt fails; staff retry the room, its creation fails too;
        # then this attempt's export completes.
        def meanwhile(rooms):
            rooms.update(state=models.RoomStates.ERROR, error_message="other")
            room = rooms.get()
            room.retry_creating()
            room.save(update_fields=["state", "error_message", "closing_export"])
            rooms.update(state=models.RoomStates.ERROR, error_message="creation failed")

        modified = self.room.modified
        self._disable_while(
            mock_client, mock_config, meanwhile, models.ExportStates.COMPLETED
        )
        self.assertEqual(self.room.state, models.RoomStates.ERROR)
        self.assertEqual(self.room.error_message, "creation failed")
        self.assertIsNone(self.room.closing_export)
        self.assertGreaterEqual(self.room.modified, modified)

    def test_an_export_is_not_left_on_a_room_retried_meanwhile(
        self, mock_client, mock_config
    ):
        # This attempt still sees the room disabling; staff have retried it to
        # creating since, before the attempt records its export.
        room = models.MatrixRoom.objects.get(pk=self.room.pk)
        models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=models.RoomStates.CREATING
        )
        mock_client.is_enabled.return_value = False
        export = tasks._export_before_closing(room)
        self.assertEqual(export.export_type, models.ExportTypes.ON_DELETION)
        self.room.refresh_from_db()
        self.assertIsNone(self.room.closing_export)

    def _holding(self, state):
        """The room holding an export of this disable already, so nothing but
        the outcome writes the room."""
        export = models.MatrixHistoryExport.objects.create(
            room=self.room, export_type=models.ExportTypes.ON_DELETION, state=state
        )
        models.MatrixRoom.objects.filter(pk=self.room.pk).update(closing_export=export)
        self.room.refresh_from_db()
        return self.room.modified

    def test_archiving_marks_the_room_modified(self, mock_client, mock_config):
        before = self._holding(models.ExportStates.COMPLETED)
        self._disable(mock_client, mock_config)
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
        self.assertGreater(self.room.modified, before)

    def test_failing_marks_the_room_modified(self, mock_client, mock_config):
        before = self._holding(models.ExportStates.PENDING)
        self._disable(mock_client, mock_config, running=False)
        self.assertEqual(self.room.state, models.RoomStates.ERROR)
        self.assertGreater(self.room.modified, before)

    def test_a_disable_after_a_failed_reprovision_exports_again(
        self, mock_client, mock_config
    ):
        old = models.MatrixHistoryExport.objects.create(
            room=self.room,
            export_type=models.ExportTypes.ON_DELETION,
            state=models.ExportStates.COMPLETED,
        )
        models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=models.RoomStates.ACTIVE, closing_export=old
        )
        with mock.patch.object(tasks, "create_room"):
            tasks.reprovision_rooms()
        self.room.refresh_from_db()
        self.assertEqual(self.room.state, models.RoomStates.CREATING)
        self.assertIsNone(self.room.closing_export)
        # Creating the room fails, and it is disabled from the error.
        self.room.set_erred()
        self.room.save()
        self.room.begin_disabling()
        self.room.save(update_fields=["state", "closing_export"])
        wait = self._disable(
            mock_client, mock_config, outcome=models.ExportStates.COMPLETED
        )
        wait.assert_called_once()
        self.assertNotEqual(wait.call_args.args[0].pk, old.pk)

    def test_every_way_back_to_an_active_room_drops_the_export(
        self, mock_client, mock_config
    ):
        old = models.MatrixHistoryExport.objects.create(
            room=self.room, export_type=models.ExportTypes.ON_DELETION
        )
        for state, transition in (
            (models.RoomStates.ERROR, "retry_creating"),
            (models.RoomStates.ARCHIVED, "reactivate"),
            (models.RoomStates.ACTIVE, "begin_reprovisioning"),
        ):
            with self.subTest(transition):
                self.room.state = state
                self.room.closing_export = old
                getattr(self.room, transition)()
                self.assertIsNone(self.room.closing_export)

    def test_a_failed_export_is_asked_again(self, mock_client, mock_config):
        self._disable(mock_client, mock_config, outcome=models.ExportStates.FAILED)
        self.room.begin_disabling()
        self.room.save()
        self._disable(mock_client, mock_config, outcome=models.ExportStates.COMPLETED)
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
        self.assertEqual(self.room.exports.count(), 2)

    def test_the_next_disable_after_a_reactivation_exports_again(
        self, mock_client, mock_config
    ):
        self._disable(mock_client, mock_config, outcome=models.ExportStates.COMPLETED)
        self.room.reactivate()
        self.room.save()
        self.room.begin_disabling()
        self.room.save()
        self._disable(mock_client, mock_config, outcome=models.ExportStates.COMPLETED)
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
        self.assertEqual(self.room.exports.count(), 2)

    def test_disabling_again_waits_for_the_export_already_asked_for(
        self, mock_client, mock_config
    ):
        self._disable(mock_client, mock_config, running=False)
        self.room.begin_disabling()
        self.room.save()
        self._disable(mock_client, mock_config, outcome=models.ExportStates.COMPLETED)
        self.assertEqual(self.room.state, models.RoomStates.ARCHIVED)
        self.assertEqual(self.room.exports.count(), 1)


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class ExportRoomHistoryTaskTest(TestCase):
    def test_leaves_the_export_to_the_bot(self, mock_client):
        mock_client.is_enabled.return_value = True
        export = models.MatrixHistoryExport.objects.create(
            room=fixtures.MatrixChatFixture().matrix_room
        )
        tasks.export_room_history(str(export.uuid))
        export.refresh_from_db()
        self.assertEqual(export.state, models.ExportStates.PENDING)


class ArchiveNameTest(TestCase):
    def test_keeps_the_extension_and_cuts_the_rest(self):
        name = bot_export.archive_name("$event:hs", "x" * 500 + ".pdf")
        self.assertTrue(name.startswith("_event_hs_x"))
        self.assertTrue(name.endswith(".pdf"))
        self.assertEqual(len(name), bot_export.MAX_ARCHIVE_NAME + len(".pdf"))

    def test_sanitises(self):
        self.assertEqual(
            bot_export.archive_name("$e", "../../etc/pass wd"), "_e_.._.._etc_pass_wd"
        )
        self.assertEqual(bot_export.archive_name("$e", None), "_e_file")


class PageTest(TestCase):
    def _page(self, handler, start=""):
        real_client = httpx.AsyncClient
        seen = []

        def record(request):
            seen.append(request)
            return handler(request)

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(record), **kwargs)

        exporter = bot_export.RoomExporter(
            mock.Mock(access_token="bot-token"), HOMESERVER, export_media=False
        )
        with mock.patch.object(bot_export.httpx, "AsyncClient", client):
            result = async_to_sync(exporter._page)(ROOM_ID, start)
        return result, seen

    def test_reads_a_page_backwards(self):
        (chunk, end), seen = self._page(
            lambda request: httpx.Response(200, json={"chunk": [{}], "end": "t2"}),
            start="t1",
        )
        self.assertEqual((chunk, end), ([{}], "t2"))
        self.assertEqual(
            seen[0].url.path,
            "/_matrix/client/v3/rooms/!test_room:matrix.example.com/messages",
        )
        self.assertEqual(
            dict(seen[0].url.params), {"dir": "b", "limit": "100", "from": "t1"}
        )
        self.assertEqual(seen[0].headers["authorization"], "Bearer bot-token")

    def test_an_error_fails_the_export(self):
        with self.assertRaisesRegex(bot_export.ExportError, "403 M_FORBIDDEN"):
            self._page(
                lambda request: httpx.Response(
                    403, json={"errcode": "M_FORBIDDEN", "error": "Not in room"}
                )
            )

    def test_the_homeserver_s_message_is_not_kept(self):
        with self.assertRaises(bot_export.ExportError) as caught:
            self._page(
                lambda request: httpx.Response(
                    403, json={"errcode": "M_FORBIDDEN", "error": "Not in room"}
                )
            )
        self.assertNotIn("Not in room", str(caught.exception))

    def test_a_rate_limited_or_failed_page_is_asked_for_once_more(self):
        for first in (
            httpx.Response(
                429, json={"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 1500}
            ),
            httpx.Response(502),
        ):
            with self.subTest(status=first.status_code):
                answers = iter([first, httpx.Response(200, json={"chunk": []})])
                with mock.patch.object(
                    bot_export.asyncio, "sleep", mock.AsyncMock()
                ) as sleep:
                    (chunk, _), seen = self._page(lambda request: next(answers))
                self.assertEqual((chunk, len(seen)), ([], 2))
                sleep.assert_awaited_once()

    def test_the_wait_follows_what_the_homeserver_asks(self):
        cases = [
            (httpx.Response(429, headers={"Retry-After": "7"}), 7),
            (httpx.Response(429, json={"retry_after_ms": 1500}), 1.5),
            (httpx.Response(429, json={"retry_after_ms": True}), 2),
            (httpx.Response(429, json={"retry_after_ms": 10**9}), 30),
            (httpx.Response(429, headers={"Retry-After": "soon"}), 2),
        ]
        for first, delay in cases:
            with self.subTest(delay=delay):
                answers = iter([first, httpx.Response(200, json={"chunk": []})])
                with mock.patch.object(
                    bot_export.asyncio, "sleep", mock.AsyncMock()
                ) as sleep:
                    self._page(lambda request: next(answers))
                sleep.assert_awaited_once_with(delay)

    def test_an_odd_retry_after_falls_back_to_the_default(self):
        for value in ("\u0663", "9" * 5000, "-1", "0", ""):
            with self.subTest(value=value[:10]):
                self.assertEqual(
                    bot_export._retry_delay({}, {"retry-after": value}),
                    bot_export.PAGE_RETRY_SECONDS,
                )

    def test_a_page_is_asked_for_twice_at_most(self):
        with (
            mock.patch.object(bot_export.asyncio, "sleep", mock.AsyncMock()),
            self.assertRaisesRegex(bot_export.ExportError, "503"),
        ):
            self._page(lambda request: httpx.Response(503))

    def test_an_unreachable_homeserver_fails_the_export(self):
        def fail(request):
            raise httpx.ConnectError("unreachable")

        with self.assertRaisesRegex(bot_export.ExportError, "could not be reached"):
            self._page(fail)


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
@mock.patch("waldur_mastermind.matrix_chat.tasks.config")
class PeriodicExportTest(TestCase):
    def test_a_room_with_an_export_waiting_gets_no_other(
        self, mock_config, mock_client
    ):
        mock_config.MATRIX_HISTORY_EXPORT_ENABLED = True
        mock_client.is_enabled.return_value = True
        room = fixtures.MatrixChatFixture().matrix_room
        with mock.patch.object(tasks, "export_room_history") as task:
            tasks.periodic_history_export()
            tasks.periodic_history_export()
        self.assertEqual(room.exports.count(), 1)
        task.delay.assert_called_once()


class NioLogTest(TestCase):
    def test_validation_details_are_withheld(self):
        for name in bot.NIO_VALIDATION_LOGGERS:
            with self.subTest(name), self.assertLogs(name, "WARNING") as logs:
                logging.getLogger(name).warning(
                    "Error validating event: 'url' is a required property\n"
                    "On instance['content']: {'body': 'a decrypted secret'}"
                )
            self.assertNotIn("a decrypted secret", logs.output[0])
            self.assertIn("details withheld", logs.output[0])

    def test_a_malformed_decrypted_event_logs_no_content(self):
        with self.assertLogs("nio.events.misc", "WARNING") as logs:
            Event.parse_decrypted_event(
                raw("$1", {"msgtype": "m.image", "body": "a decrypted secret"})
            )
        self.assertNotIn("a decrypted secret", "\n".join(logs.output))


class MediaArchiveCleanupTest(TestCase):
    def test_the_archive_is_closed_when_the_export_gives_way(self):
        client = fake_client([image("$img")])
        exporter = exporter_for(
            client,
            export_media=True,
            should_yield=mock.AsyncMock(side_effect=[False, False, True]),
        )
        archives = []
        real = bot_export.tempfile.SpooledTemporaryFile

        def spooled(**kwargs):
            archives.append(real(**kwargs))
            return archives[-1]

        with (
            mock.patch.object(bot_export.tempfile, "SpooledTemporaryFile", spooled),
            self.assertRaises(bot_export.Preempted),
        ):
            async_to_sync(exporter.export)(ROOM_ID)
        self.assertTrue(archives)
        self.assertTrue(all(archive.closed for archive in archives))


class ExportSlotsTest(TestCase):
    """Exports run side by side in bot.EXPORT_SLOTS slots."""

    def setUp(self):
        project_type = fixtures.MatrixChatFixture().matrix_room.content_type
        self.rooms = [
            models.MatrixRoom.objects.create(
                room_id=f"!room{i}:matrix.example.com",
                state=models.RoomStates.DISABLING,
                content_type=project_type,
                object_id=10_000 + i,
            )
            for i in range(6)
        ]
        self.bot = bot.MatrixBot("holder", _settings())
        self.bot._renew_lease = mock.AsyncMock()
        self.bot._synced.set()

    def _queue(self, *export_types):
        return [
            models.MatrixHistoryExport.objects.create(room=room, export_type=kind)
            for room, kind in zip(self.rooms, export_types)
        ]

    def _drive(self, script):
        """Run the slots with exports that each run until the test ends them.

        ``script(slots)`` drives them through ``slots``: ``started`` (export
        types in the order they started), ``running`` (their release events,
        by start index) and ``settle()``, which waits for two whole rounds of
        claims, so that whatever could start has started.
        """
        slots = types.SimpleNamespace(started=[], running={})

        async def export(claimed):
            index = len(slots.started)
            slots.started.append(claimed.export_type)
            release = slots.running[index] = asyncio.Event()
            await release.wait()
            del slots.running[index]

        async def settle():
            # The slots renew the lease at the start of every round of claims:
            # three more renewals are two whole rounds since the last change.
            target = self.bot._renew_lease.await_count + 3
            while self.bot._renew_lease.await_count < target:
                await asyncio.sleep(0.001)

        slots.settle = settle

        async def run():
            task = asyncio.create_task(self.bot.export_forever())
            try:
                await script(slots)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with (
            mock.patch.object(self.bot, "export", export),
            mock.patch.object(bot, "EXPORT_POLL_SECONDS", 0.01),
        ):
            async_to_sync(run)()

    def test_rooms_being_closed_are_exported_side_by_side(self):
        kinds = models.ExportTypes
        self._queue(kinds.PERIODIC, kinds.MANUAL, *[kinds.ON_DELETION] * 4)

        async def script(slots):
            await slots.settle()
            # Every slot goes to a room being closed, and only three run.
            self.assertEqual(slots.started, [kinds.ON_DELETION] * bot.EXPORT_SLOTS)
            slots.running[0].set()
            await slots.settle()
            self.assertEqual(slots.started[3], kinds.ON_DELETION)
            self.assertEqual(len(slots.running), bot.EXPORT_SLOTS)
            for release in list(slots.running.values()):
                release.set()
            await slots.settle()
            # Then the manual export, alone of its kind.
            self.assertEqual(slots.started[4:], [kinds.MANUAL])
            slots.running[4].set()
            await slots.settle()
            self.assertEqual(slots.started[5:], [kinds.PERIODIC])

        self._drive(script)

    def test_one_manual_or_daily_export_at_a_time(self):
        kinds = models.ExportTypes
        self._queue(kinds.PERIODIC, kinds.MANUAL, kinds.PERIODIC)

        async def script(slots):
            for expected in range(1, 4):
                await slots.settle()
                self.assertEqual(len(slots.started), expected)
                self.assertEqual(len(slots.running), 1)
                for release in list(slots.running.values()):
                    release.set()

        self._drive(script)

    def test_a_room_being_closed_runs_beside_a_manual_export(self):
        kinds = models.ExportTypes
        self._queue(kinds.MANUAL)

        async def script(slots):
            await slots.settle()
            self.assertEqual(slots.started, [kinds.MANUAL])
            await sync_to_async(self._queue_closing)()
            await slots.settle()
            self.assertEqual(slots.started, [kinds.MANUAL, kinds.ON_DELETION])
            self.assertEqual(len(slots.running), 2)

        self._drive(script)

    def _queue_closing(self):
        models.MatrixHistoryExport.objects.create(
            room=self.rooms[5], export_type=models.ExportTypes.ON_DELETION
        )

    def test_a_stop_puts_every_running_export_back(self):
        exports = self._queue(*[models.ExportTypes.ON_DELETION] * 3)

        async def export(claimed):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await sync_to_async(bot_state.requeue_export)(claimed)
                raise

        async def run():
            slots = asyncio.create_task(self.bot.export_forever())
            while len(self.bot._exports) < 3:
                await asyncio.sleep(0.01)
            slots.cancel()
            await asyncio.gather(slots, return_exceptions=True)

        with mock.patch.object(self.bot, "export", export):
            async_to_sync(run)()
        for export in exports:
            export.refresh_from_db()
            self.assertEqual(export.state, models.ExportStates.PENDING)
        self.assertEqual(self.bot._exports, {})

    def test_claims_for_rooms_being_closed_only(self):
        kinds = models.ExportTypes
        self._queue(kinds.MANUAL)
        self.assertIsNone(bot_state.claim_next_export(deletions_only=True))
        (closing,) = self._queue(kinds.ON_DELETION)
        self.assertEqual(bot_state.claim_next_export(deletions_only=True), closing)


class ExportMemoryTest(TestCase):
    def test_exports_side_by_side_hold_one_attachment_at_a_time(self):
        in_flight = []
        peak = []
        lock = bot_export.PriorityLock

        def exporter(name):
            client = fake_client([image(f"${name}1"), image(f"${name}2")])
            instance = bot_export.RoomExporter(
                client, HOMESERVER, export_media=True, memory_lock=shared[0]
            )
            instance._page = client.export_pages

            async def download(mxc, size_limit):
                in_flight.append(mxc)
                peak.append(len(in_flight))
                await asyncio.sleep(0.01)
                in_flight.remove(mxc)
                return b"jpeg"

            instance._download = download
            return instance

        async def run():
            shared.append(lock())
            results = await asyncio.gather(
                exporter("a").export(ROOM_ID), exporter("b").export(ROOM_ID)
            )
            for files in results:
                self.assertEqual(files.media_count, 2)
                files.close()

        shared = []
        async_to_sync(run)()
        self.assertEqual(max(peak), 1)

    def test_waiting_for_the_lock_does_not_count_against_the_deadline(self):
        clock = [0.0]
        exporter = exporter_for(
            fake_client([image("$img")]),
            {"mxc://matrix.example.com/plain": b"jpeg"},
            export_media=True,
            memory_lock=bot_export.PriorityLock(),
            now=lambda: clock[0],
        )

        @contextlib.asynccontextmanager
        async def hold(priority):
            # Another export holds the lock past this one's whole deadline.
            clock[0] += bot_export.EXPORT_TIMEOUT_SECONDS + 1
            yield

        exporter._memory_lock.hold = hold
        files = async_to_sync(exporter.export)(ROOM_ID)
        self.assertEqual(files.media_count, 1)
        files.close()


class PriorityLockTest(TestCase):
    def test_rooms_being_closed_go_first_then_in_order(self):
        order = []

        async def run():
            lock = bot_export.PriorityLock()
            first_in = asyncio.Event()
            release = asyncio.Event()

            async def holder():
                async with lock.hold(1):
                    first_in.set()
                    await release.wait()

            async def waiter(name, priority):
                async with lock.hold(priority):
                    order.append(name)

            holding = asyncio.create_task(holder())
            await first_in.wait()
            waiting = [
                asyncio.create_task(waiter("daily", 1)),
                asyncio.create_task(waiter("closing 1", 0)),
                asyncio.create_task(waiter("manual", 1)),
                asyncio.create_task(waiter("closing 2", 0)),
            ]
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(holding, *waiting)

        async_to_sync(run)()
        self.assertEqual(order, ["closing 1", "closing 2", "daily", "manual"])

    def test_a_cancelled_waiter_does_not_keep_the_lock(self):
        async def run():
            lock = bot_export.PriorityLock()
            release = asyncio.Event()
            entered = asyncio.Event()

            async def holder():
                async with lock.hold(0):
                    entered.set()
                    await release.wait()

            async def waiter():
                async with lock.hold(0):
                    pass

            holding = asyncio.create_task(holder())
            await entered.wait()
            cancelled = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            cancelled.cancel()
            release.set()
            await asyncio.gather(holding, cancelled, return_exceptions=True)
            async with lock.hold(1):
                return True

        self.assertTrue(async_to_sync(run)())


class ExportDocumentTest(TestCase):
    def test_streamed_json_is_what_json_dumps_writes(self):
        header = {
            "room_id": "!r:hs",
            "room_name": "Projekt «Õun»",
            "exported_at": "2026-10-10T00:00:00+00:00",
            "message_count": 2,
            "media_count": 0,
        }
        export_messages = [
            {"event_id": "$1", "body": "line one\nline two", "relation": {"a": [1]}},
            {"event_id": "$2", "media_info": {}, "verified": True},
        ]
        for messages in (export_messages, []):
            with self.subTest(count=len(messages)):
                written = BytesIO()
                bot_export.write_export_document(written, header, iter(messages))
                self.assertEqual(
                    written.getvalue().decode(),
                    json.dumps({**header, "messages": messages}, indent=2, default=str),
                )

    def test_nothing_decrypted_reaches_a_working_file(self):
        written = []

        class Recording(BytesIO):
            def write(self, data):
                written.append(bytes(data))
                return super().write(data)

        client = fake_client([text("$1", "a decrypted secret"), image("$img")])
        exporter = exporter_for(
            client,
            {"mxc://matrix.example.com/plain": b"decrypted photo"},
            export_media=True,
        )
        with mock.patch.object(bot_export, "_spool", Recording):
            files = async_to_sync(exporter.export)(ROOM_ID)
        everything = b"".join(written)
        self.assertGreater(len(everything), 0)
        self.assertNotIn(b"a decrypted secret", everything)
        self.assertNotIn(b"decrypted photo", everything)
        self.assertNotIn(b"photo.jpg", everything)
        messages, _, _ = opened(files)
        self.assertEqual(messages[0]["body"], "a decrypted secret")


class ExportLimitsTest(TestCase):
    def test_the_key_is_not_in_the_files_repr(self):
        prepared_export = bot_export.ExportFiles(b"\x01" * 32, BytesIO(), None, 0, 0)
        self.assertNotIn(repr(b"\x01" * 32), repr(prepared_export))
        self.assertNotIn("data_key", repr(prepared_export))

    def test_a_manual_or_daily_export_has_a_hard_limit_waits_included(self):
        for priority, fails in ((1, True), (0, False)):
            with self.subTest(priority=priority):
                clock = [0.0]
                exporter = exporter_for(
                    fake_client([image("$img")]),
                    {"mxc://matrix.example.com/plain": b"jpeg"},
                    export_media=True,
                    priority=priority,
                    now=lambda: clock[0],
                )

                @contextlib.asynccontextmanager
                async def hold(priority):
                    # Others hold the lock past the hard limit.
                    clock[0] += bot_export.MAX_EXPORT_SECONDS + 1
                    yield

                exporter._memory_lock.hold = hold
                # Two attachments: the second checkpoint comes after the wait.
                exporter._page = fake_client([image("$a"), image("$b")]).export_pages
                if fails:
                    with self.assertRaisesRegex(bot_export.ExportError, "waiting"):
                        async_to_sync(exporter.export)(ROOM_ID)
                else:
                    async_to_sync(exporter.export)(ROOM_ID).close()


class SenderIdentityTest(TestCase):
    def _export(self, identity):
        asked = []

        async def identity_of(sender):
            asked.append(sender)
            return identity

        client = fake_client(
            [
                text("$3", "one"),
                text("$2", "two"),
                raw("$1", {"msgtype": "m.text", "body": "clear"}),
            ]
        )
        exporter = exporter_for(client, export_media=False, identity_of=identity_of)
        messages, _, _ = opened(async_to_sync(exporter.export)(ROOM_ID))
        return asked, messages

    def test_each_sender_is_asked_once_and_only_decrypted_entries_get_it(self):
        vouched = {"ALICEDEVICE": (ALICE_ED25519, "sender-curve-key")}
        asked, messages = self._export(("pinned", vouched))
        self.assertEqual(asked, ["@alice:matrix.example.com"])
        self.assertEqual(
            [m.get("sender_identity") for m in messages], ["pinned", "pinned", None]
        )
        self.assertFalse(any(bot_export.SIGNING_KEY in m for m in messages))

    def test_a_device_the_identity_does_not_sign_is_changed(self):
        # The homeserver had nio trust a device of its own; the identity Waldur
        # vouches for signs another one.
        vouched = {"ALICEDEVICE": ("another-ed25519", "sender-curve-key")}
        _, messages = self._export(("escrowed", vouched))
        self.assertEqual(messages[0]["sender_identity"], "changed")
        _, messages = self._export(("escrowed", {}))
        self.assertEqual(messages[0]["sender_identity"], "changed")

    def test_unknown_stays_unknown(self):
        _, messages = self._export(("unknown", {}))
        self.assertEqual(messages[0]["sender_identity"], "unknown")


ALICE = "@alice:matrix.example.com"


class FakeAccountData:
    """The user's account data, as the homeserver serves it."""

    def __init__(self, items):
        self.items = items

    async def __call__(self, matrix_user_id, kind):
        return self.items.get(kind)


def _escrowed_storage(stored_master=None):
    return FakeAccountData(
        {
            "m.secret_storage.default_key": {"key": "k1"},
            "m.secret_storage.key.k1": recovery_vectors.KEY_INFO,
            "m.cross_signing.master": {
                "encrypted": {"k1": stored_master or recovery_vectors.STORED_MASTER}
            },
        }
    )


class BotSenderIdentityTest(TestCase):
    """With real keys: the escrowed vectors of test_recovery_keys, whose master
    key's private seed is bytes(range(64, 96))."""

    def setUp(self):
        self.profile = fixtures.MatrixChatFixture().matrix_user_profile
        self.profile.matrix_user_id = ALICE
        self.profile.save()
        self.alice = Identity(ALICE)
        self.alice.master = cross_signing.SigningKey(bytes(range(64, 96)))
        assert self.alice.master.public_key == recovery_vectors.MASTER_PUBLIC
        device = self.alice.add_device("ALICEDEVICE")
        self.device = (
            device["keys"]["ed25519:ALICEDEVICE"],
            device["keys"]["curve25519:ALICEDEVICE"],
        )
        self.bot = bot.MatrixBot("holder", _settings())
        self.bot.query_keys = mock.AsyncMock(
            side_effect=lambda users: self.alice.response()
        )
        self.bot.history = mock.Mock(account_data=_escrowed_storage())

    def _identity(self, user_id=ALICE):
        return async_to_sync(self.bot.sender_identity)(user_id)

    def _set(self, **fields):
        for name, value in fields.items():
            setattr(self.profile, name, value)
        self.profile.save()

    def _label(self, device_id="ALICEDEVICE", keys=None):
        ed25519, curve25519 = keys or self.device
        return bot_export.event_identity(
            self._identity(), device_id, curve25519, ed25519
        )

    def test_escrowed_for_a_device_the_escrowed_identity_signs(self):
        self._set(recovery_key=recovery_vectors.RECOVERY_KEY)
        self.assertEqual(self._identity(), ("escrowed", {"ALICEDEVICE": self.device}))
        self.assertEqual(self._label(), "escrowed")

    def test_a_device_the_escrowed_identity_does_not_sign_is_changed(self):
        # What the homeserver can do: have nio trust a device signed by an
        # identity of its own, while it serves the genuine identity here.
        self._set(recovery_key=recovery_vectors.RECOVERY_KEY)
        unsigned = self.alice.add_device("MALLORYDEVICE", signed=False)
        self.assertEqual(
            self._label(
                "MALLORYDEVICE",
                (
                    unsigned["keys"]["ed25519:MALLORYDEVICE"],
                    unsigned["keys"]["curve25519:MALLORYDEVICE"],
                ),
            ),
            "changed",
        )
        # Nor may a signed device's id be borrowed with other keys.
        self.assertEqual(self._label(keys=("other-ed25519", self.device[1])), "changed")

    def test_a_secret_storage_key_that_fails_its_check_is_changed(self):
        # The stored master key itself is intact; the key description's MAC is
        # not the recovery key's.
        self._set(recovery_key=recovery_vectors.RECOVERY_KEY)
        storage = _escrowed_storage()
        storage.items["m.secret_storage.key.k1"] = {
            **recovery_vectors.KEY_INFO,
            "mac": recovery_vectors.OTHER_KEY_INFO["mac"],
        }
        self.bot.history = mock.Mock(account_data=storage)
        self.assertEqual(self._identity()[0], "changed")

    def test_another_recovery_key_or_master_key_is_changed(self):
        self._set(recovery_key=recovery_vectors.OTHER_RECOVERY_KEY)
        self.assertEqual(self._identity()[0], "changed")
        self._set(recovery_key=recovery_vectors.RECOVERY_KEY)
        self.alice.master = cross_signing.SigningKey.generate()
        self.assertEqual(self._identity()[0], "changed")

    def test_pinned(self):
        self._set(pinned_master_key=recovery_vectors.MASTER_PUBLIC)
        self.assertEqual(self._label(), "pinned")
        self._set(pinned_master_key="AnotherKey")
        self.assertEqual(self._label(), "changed")

    def test_the_bot_only_for_its_own_device(self):
        self.bot.client = mock.Mock(device_id="WALDURBOTDEVICE")
        self.bot.client.olm.account.identity_keys = {
            "ed25519": "bot-ed25519",
            "curve25519": "bot-curve25519",
        }
        identity = async_to_sync(self.bot.sender_identity)(bot_test.BOT_USER)
        self.assertEqual(
            bot_export.event_identity(
                identity, "WALDURBOTDEVICE", "bot-curve25519", "bot-ed25519"
            ),
            "bot",
        )
        self.assertEqual(
            bot_export.event_identity(
                identity, "OTHERBOTDEVICE", "bot-curve25519", "bot-ed25519"
            ),
            "changed",
        )

    def test_nothing_to_go_by(self):
        self.assertEqual(self._identity("@stranger:elsewhere"), ("unknown", {}))
        self.assertEqual(self._identity(), ("unknown", {}))
        self._set(pinned_master_key=recovery_vectors.MASTER_PUBLIC)
        self.bot.query_keys.side_effect = lambda users: {"master_keys": {}}
        self.assertEqual(self._identity(), ("unknown", {}))

    def test_a_failed_check_is_unknown_not_a_failed_export(self):
        self._set(recovery_key=recovery_vectors.RECOVERY_KEY)

        async def unreachable(matrix_user_id, kind):
            raise OSError("homeserver unreachable")

        self.bot.history = mock.Mock(account_data=unreachable)
        self.assertEqual(self._identity(), ("unknown", {}))
