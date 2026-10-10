"""What the export says about a message's sender, with nio's own Olm machine.

The other export tests stand in for nio; these check that ``verified`` and the
markers mean what they say when the bot's real crypto decrypts the events.
"""

import tempfile
from unittest import mock

from django.test import SimpleTestCase
from nio.crypto import Olm, OlmDevice
from nio.store import DefaultStore

from waldur_mastermind.matrix_chat import bot_export
from waldur_mastermind.matrix_chat.crypto import nio_compat

ROOM_ID = "!room:test"
ALICE = ("@alice:test", "ALICEDEVICE")
BOT = ("@bot:test", "BOTDEVICE")


def setUpModule():
    nio_compat.apply()


class SenderTest(SimpleTestCase):
    def setUp(self):
        directory = tempfile.mkdtemp()
        self.alice = Olm(*ALICE, DefaultStore(*ALICE, directory))
        self.bot = Olm(*BOT, DefaultStore(*BOT, directory))
        self.alice_device = OlmDevice(*ALICE, self.alice.account.identity_keys)
        self.bot.device_store.add(self.alice_device)
        self.bot.verify_device(self.alice_device)
        self.alice.create_outbound_group_session(ROOM_ID)
        self.session = self.alice.outbound_group_sessions[ROOM_ID]
        self.session.shared = True
        # The room key, as Alice's device would share it with the bot.
        self.bot.create_group_session(
            self.alice.account.identity_keys["curve25519"],
            self.alice.account.identity_keys["ed25519"],
            ROOM_ID,
            self.session.id,
            self.session.session_key,
        )
        self.exporter = bot_export.RoomExporter(
            mock.Mock(olm=self.bot), "https://hs.test", export_media=False
        )

    def _sent(self, event_id, body, sender=ALICE[0], **envelope):
        content = self.alice.group_encrypt(
            ROOM_ID,
            {"type": "m.room.message", "content": {"msgtype": "m.text", "body": body}},
        )
        content.update(envelope)
        return {
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 1234567890,
            "type": "m.room.encrypted",
            "content": content,
        }

    def _export(self, event):
        export_message, _ = self.exporter._message(event, ROOM_ID)
        return export_message

    def test_a_message_from_a_verified_device_is_verified(self):
        exported = self._export(self._sent("$1", "hello"))
        self.assertEqual(exported["body"], "hello")
        self.assertIs(exported["verified"], True)
        self.assertEqual(exported["device_id"], ALICE[1])
        # The device key the Olm session vouches for, for the identity check.
        self.assertEqual(
            exported[bot_export.SIGNING_KEY],
            self.alice.account.identity_keys["ed25519"],
        )

    def _identity_of(self, event):
        """The ``sender_identity`` the real _message path gives ``event``, with
        Alice's device the one her identity signs."""
        exported = self._export(event)
        keys = self.alice.account.identity_keys
        return bot_export.event_identity(
            ("escrowed", {ALICE[1]: (keys["ed25519"], keys["curve25519"])}),
            exported["device_id"],
            exported["sender_key"],
            exported[bot_export.SIGNING_KEY],
        )

    def test_the_identity_holds_for_a_key_shared_by_the_device(self):
        self.assertEqual(self._identity_of(self._sent("$1", "hello")), "escrowed")

    def test_a_forwarded_key_vouches_for_no_device(self):
        session = self.bot.inbound_group_store.get(
            ROOM_ID, self.alice.account.identity_keys["curve25519"], self.session.id
        )
        session.forwarding_chain = ["curve-key-of-whoever-forwarded-it"]
        event = self._sent("$1", "hello")
        self.assertIsNone(self._export(event)[bot_export.SIGNING_KEY])
        self.assertEqual(self._identity_of(event), "changed")

    def test_a_message_from_a_device_not_verified_is_not(self):
        self.bot.unverify_device(self.alice_device)
        self.assertIs(self._export(self._sent("$1", "hello"))["verified"], False)

    def test_a_message_under_a_forwarded_key_is_not_verified(self):
        session = self.bot.inbound_group_store.get(
            ROOM_ID, self.alice.account.identity_keys["curve25519"], self.session.id
        )
        session.forwarding_chain = ["curve-key-of-whoever-forwarded-it"]
        self.assertIs(self._export(self._sent("$1", "hello"))["verified"], False)

    def test_a_message_put_under_another_sender_is_not_verified(self):
        exported = self._export(self._sent("$1", "hello", sender="@mallory:test"))
        self.assertEqual(exported["sender"], "@mallory:test")
        self.assertIs(exported["verified"], False)

    def test_a_message_claiming_another_verified_device_is_undecryptable(self):
        other = Olm(
            "@alice:test",
            "OTHERDEVICE",
            DefaultStore("@alice:test", "OTHERDEVICE", tempfile.mkdtemp()),
        )
        other_device = OlmDevice(
            "@alice:test", "OTHERDEVICE", other.account.identity_keys
        )
        self.bot.device_store.add(other_device)
        self.bot.verify_device(other_device)
        exported = self._export(self._sent("$1", "hello", device_id="OTHERDEVICE"))
        self.assertTrue(exported["undecryptable"])
        self.assertNotIn("body", exported)

    def test_a_replayed_message_is_marked_as_a_replay(self):
        original = self._sent("$1", "hello")
        self.assertEqual(self._export(original)["body"], "hello")
        # The same event again decrypts: the bot decrypted it on arrival too.
        self.assertEqual(self._export(original)["body"], "hello")
        replayed = {**original, "event_id": "$2"}
        exported = self._export(replayed)
        self.assertIs(exported["replay"], True)
        self.assertNotIn("undecryptable", exported)
        self.assertNotIn("body", exported)

    def test_a_message_without_its_key_is_undecryptable(self):
        self.alice.create_outbound_group_session(ROOM_ID)
        self.alice.outbound_group_sessions[ROOM_ID].shared = True
        exported = self._export(self._sent("$1", "hello"))
        self.assertTrue(exported["undecryptable"])
        self.assertNotIn("replay", exported)
