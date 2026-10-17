from unittest import mock

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from nio.exceptions import EncryptionError

from waldur_mastermind.matrix_chat import bot_export, history_reader

HOMESERVER = "https://matrix.example.com"
ROOM_ID = "!room:matrix.example.com"
BOT = "@waldur-bot:matrix.example.com"


def text(event_id, sender, body, ts=0, relates_to=None, **content):
    content = {"msgtype": "m.text", "body": body, **content}
    if relates_to:
        content["m.relates_to"] = relates_to
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "content": content,
    }


def encrypted(event_id, sender):
    return {
        "type": "m.room.encrypted",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": 0,
        "content": {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "AwgA",
            "sender_key": "key",
            "session_id": "session",
            "device_id": "DEVICE",
        },
    }


def reader():
    client = mock.Mock(access_token="bot-token")
    client.olm.decrypt_megolm_event.side_effect = EncryptionError("no session")
    return history_reader.HistoryReader(client, HOMESERVER, BOT)


class BeforeTest(SimpleTestCase):
    def _before(self, pages, limit=10, event=None):
        context = {
            "event": event or text("$cmd", "@a:x", "the event"),
            "start": "t0",
        }
        with (
            mock.patch.object(
                bot_export, "read_json", mock.AsyncMock(return_value=context)
            ) as read_json,
            mock.patch.object(
                bot_export, "read_page", mock.AsyncMock(side_effect=pages)
            ) as read_page,
        ):
            messages = async_to_sync(reader().before)(ROOM_ID, "$cmd", limit)
        return messages, read_json, read_page

    def test_reads_the_messages_before_the_event_oldest_first(self):
        messages, read_json, read_page = self._before(
            [([text("$2", "@b:x", "second"), text("$1", "@a:x", "first")], None)]
        )
        self.assertEqual([m.body for m in messages], ["first", "second", "the event"])
        self.assertEqual(
            read_json.call_args.args[2],
            "rooms/%21room%3Amatrix.example.com/context/%24cmd",
        )
        self.assertEqual(read_page.call_args.args[3], "t0")

    def test_keeps_the_latest_up_to_the_limit(self):
        page = [text(f"${i}", "@a:x", f"m{i}") for i in range(9, 0, -1)]
        messages, _, _ = self._before([(page, None)], limit=3)
        self.assertEqual([m.body for m in messages], ["m8", "m9", "the event"])

    def test_leaves_out_commands_the_bot_and_what_it_cannot_decrypt(self):
        messages, _, _ = self._before(
            [
                (
                    [
                        text("$4", BOT, "bot reply"),
                        encrypted("$3", "@a:x"),
                        text("$2", "@a:x", "!status"),
                        text("$1", "@a:x", "kept"),
                    ],
                    None,
                )
            ]
        )
        self.assertEqual([m.body for m in messages], ["kept", "the event"])

    def test_applies_edits_by_the_sender_only(self):
        edit = {"rel_type": "m.replace", "event_id": "$1"}
        messages, _, _ = self._before(
            [
                (
                    [
                        text(
                            "$3",
                            "@b:x",
                            "* forged",
                            relates_to=edit,
                            **{"m.new_content": {"body": "forged"}},
                        ),
                        text(
                            "$2",
                            "@a:x",
                            "* fixed",
                            relates_to=edit,
                            **{"m.new_content": {"body": "fixed"}},
                        ),
                        text("$1", "@a:x", "typo"),
                    ],
                    None,
                )
            ]
        )
        self.assertEqual([m.body for m in messages], ["fixed", "the event"])

    def test_takes_the_context_s_events_before(self):
        context = {
            "event": text("$cmd", "@a:x", "the event"),
            "events_before": [
                text("$2", "@b:x", "second"),
                text("$1", "@a:x", "first"),
            ],
            "start": "t0",
        }
        with (
            mock.patch.object(
                bot_export, "read_json", mock.AsyncMock(return_value=context)
            ),
            mock.patch.object(
                bot_export,
                "read_page",
                mock.AsyncMock(return_value=([text("$0", "@a:x", "zeroth")], None)),
            ),
        ):
            messages = async_to_sync(reader().before)(ROOM_ID, "$cmd", 10)
        self.assertEqual(
            [m.body for m in messages], ["zeroth", "first", "second", "the event"]
        )

    def test_message_bodies_stay_out_of_reprs(self):
        message = history_reader.HistoryMessage("$e", "@a:x", "secret plan", 0)
        self.assertNotIn("secret plan", repr(message))

    def test_strips_the_quote_of_a_reply(self):
        messages, _, _ = self._before(
            [([], None)],
            event=text(
                "$cmd",
                "@a:x",
                "> <@b:x> quoted\n\nmy answer",
                relates_to={"m.in_reply_to": {"event_id": "$0"}},
            ),
        )
        self.assertEqual([m.body for m in messages], ["my answer"])

    def test_decrypts_what_the_bot_holds_keys_for(self):
        client_reader = reader()
        decrypted = mock.Mock(spec=history_reader.RoomMessageText)
        decrypted.configure_mock(
            sender="@a:x",
            event_id="$e",
            body="secret plan",
            server_timestamp=5,
            source={"content": {"body": "secret plan"}},
        )
        client_reader.client.olm.decrypt_megolm_event.side_effect = None
        client_reader.client.olm.decrypt_megolm_event.return_value = decrypted
        messages = client_reader._messages(ROOM_ID, [encrypted("$e", "@a:x")], 5)
        self.assertEqual([m.body for m in messages], ["secret plan"])


class ThreadTest(SimpleTestCase):
    def test_reads_the_root_and_the_thread(self):
        answers = {
            "relations": {
                "chunk": [
                    text("$t2", "@b:x", "reply 2"),
                    text("$t1", "@a:x", "reply 1"),
                ]
            },
            "event": text("$root", "@a:x", "the root"),
        }

        versions = {}

        async def read_json(client, homeserver_url, path, params, version="v3"):
            kind = "relations" if "relations" in path else "event"
            versions[kind] = version
            return answers[kind]

        with mock.patch.object(bot_export, "read_json", read_json):
            messages = async_to_sync(reader().thread)(ROOM_ID, "$root", 10)
        self.assertEqual([m.body for m in messages], ["the root", "reply 1", "reply 2"])
        self.assertEqual(versions, {"relations": "v1", "event": "v3"})


class ReadJsonTest(SimpleTestCase):
    def test_errors_say_status_and_code_only(self):
        real_client = httpx.AsyncClient

        def client(**kwargs):
            return real_client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        403, json={"errcode": "M_FORBIDDEN", "error": "secret words"}
                    )
                ),
                **kwargs,
            )

        with (
            mock.patch.object(bot_export.httpx, "AsyncClient", client),
            self.assertRaises(bot_export.HistoryError) as caught,
        ):
            async_to_sync(bot_export.read_json)(
                mock.Mock(access_token="t"), HOMESERVER, "rooms/x/event/y", None
            )
        self.assertIn("403 M_FORBIDDEN", str(caught.exception))
        self.assertNotIn("secret words", str(caught.exception))
