import contextlib
import copy
import tempfile
from unittest import mock

import jsonschema
import vodozemac
from django.test import SimpleTestCase
from nio import schemas
from nio.crypto import Olm, OlmDevice, sessions
from nio.crypto.memorystores import SessionStore
from nio.events import OlmEvent, ToDeviceEvent
from nio.responses import SyncResponse
from nio.store import DefaultStore

from waldur_mastermind.matrix_chat.crypto import nio_compat

# An incremental sync, as Tuwunel 1.9.3 sends it, after the user left a room:
# the left room has a timeline but no ``state``. Its id is a room version 12
# one, without a server part.
V12_ROOM = "!fXCwzz5i4T8lgNGY_pERAGV1LRa10xhd-WxutZYGJ2Y"
INCREMENTAL_SYNC_AFTER_LEAVING = {
    "next_batch": "134",
    "rooms": {
        "leave": {
            V12_ROOM: {
                "timeline": {
                    "prev_batch": "129",
                    "events": [
                        {
                            "content": {"body": "hi", "msgtype": "m.text"},
                            "event_id": "$message",
                            "origin_server_ts": 1791492488937,
                            "sender": "@bot:test",
                            "type": "m.room.message",
                            "unsigned": {"age": 3},
                        },
                        {
                            "content": {"membership": "leave"},
                            "event_id": "$leave",
                            "origin_server_ts": 1791492488938,
                            "sender": "@bot:test",
                            "state_key": "@alice:test",
                            "type": "m.room.member",
                            "unsigned": {"age": 2},
                        },
                    ],
                },
                "account_data": {"events": []},
            }
        }
    },
    "device_one_time_keys_count": {"signed_curve25519": 0},
    "device_unused_fallback_key_types": [],
}

ALICE = ("@alice:test", "ALICEDEVICE")
BOT = ("@bot:test", "BOTDEVICE")


def setUpModule():
    nio_compat.apply()


def _first_pre_key_message(directory):
    """A bot device and the pre-key message that opens its first Olm session."""
    alice = Olm(*ALICE, DefaultStore(*ALICE, directory))
    bot = Olm(*BOT, DefaultStore(*BOT, directory))
    bot_device = OlmDevice(*BOT, bot.account.identity_keys)
    alice.device_store.add(bot_device)
    bot.device_store.add(OlmDevice(*ALICE, alice.account.identity_keys))

    bot.account.generate_one_time_keys(1)
    one_time_key = next(iter(bot.account.one_time_keys["curve25519"].values()))
    bot.account.mark_keys_as_published()
    bot.save_account()
    alice.create_session(one_time_key, bot_device.curve25519)
    _, to_device = alice.share_group_session(
        "!room:test", [BOT[0]], ignore_unverified_devices=True
    )
    event = ToDeviceEvent.parse_event(
        {
            "sender": ALICE[0],
            "type": "m.room.encrypted",
            "content": to_device["messages"][BOT[0]][BOT[1]],
        }
    )
    assert isinstance(event, OlmEvent)
    return bot, event


def _redelivered_pre_key_message():
    """A bot that already decrypted a pre-key message, and that message again.

    The bot's inbound session is gone, as if it had been lost, while its account
    no longer holds the one-time key the message was made with.
    """
    bot, event = _first_pre_key_message(tempfile.mkdtemp())
    assert bot.decrypt_event(event)
    bot.session_store = SessionStore()
    return bot, event


def _restarted_bot(directory):
    """The bot as it reopens its store after a restart, without its sessions."""
    bot = Olm(*BOT, DefaultStore(*BOT, directory))
    bot.session_store = SessionStore()
    return bot


class ConsumedOneTimeKeyTest(SimpleTestCase):
    def test_redelivery_marks_the_sender_for_unwedging(self):
        bot, event = _redelivered_pre_key_message()

        bot.decrypt_event(event)

        self.assertEqual([device.id for device in bot.wedged_devices], [ALICE[1]])

    def test_canary_unpatched_nio_lets_the_exception_escape(self):
        bot, event = _redelivered_pre_key_message()
        with mock.patch(
            "nio.crypto.sessions.InboundSession.decrypt",
            nio_compat.originals["InboundSession.decrypt"],
        ):
            with self.assertRaises(vodozemac.SessionCreationException):
                bot.decrypt_event(event)


@contextlib.contextmanager
def unpatched_room_pattern():
    sync_schema = schemas.Schemas.sync
    nio_compat.rename_room_pattern(
        sync_schema, nio_compat.ROOM_ID_PATTERN, nio_compat.originals["RoomRegex"]
    )
    try:
        yield
    finally:
        nio_compat.rename_room_pattern(
            sync_schema, nio_compat.originals["RoomRegex"], nio_compat.ROOM_ID_PATTERN
        )


def _invited_without_invite_state():
    return {"next_batch": "1", "rooms": {"invite": {V12_ROOM: {}}}}


class UsedOneTimeKeyStaysUsedTest(SimpleTestCase):
    def test_a_replay_after_a_restart_opens_no_second_session(self):
        directory = tempfile.mkdtemp()
        bot, event = _first_pre_key_message(directory)
        self.assertTrue(bot.decrypt_event(event))

        restarted = _restarted_bot(directory)
        restarted.device_store.add(bot.device_store[ALICE[0]][ALICE[1]])
        self.assertIsNone(restarted.decrypt_event(event))
        # Refused because the key is gone, not for some other reason.
        self.assertEqual([d.id for d in restarted.wedged_devices], [ALICE[1]])

    def test_canary_unpatched_nio_brings_the_key_back(self):
        directory = tempfile.mkdtemp()
        with (
            mock.patch.object(
                Olm,
                "_create_inbound_session",
                nio_compat.originals["Olm._create_inbound_session"],
            ),
            mock.patch.object(Olm, "decrypt", nio_compat.originals["Olm.decrypt"]),
        ):
            bot, event = _first_pre_key_message(directory)
            self.assertTrue(bot.decrypt_event(event))

            self.assertTrue(_restarted_bot(directory).decrypt_event(event))


class RoomVersion12IdsTest(SimpleTestCase):
    def test_a_left_room_without_state_parses_and_keeps_the_timeline(self):
        response = SyncResponse.from_dict(copy.deepcopy(INCREMENTAL_SYNC_AFTER_LEAVING))

        self.assertIsInstance(response, SyncResponse)
        room = response.rooms.leave[V12_ROOM]
        self.assertEqual(len(room.timeline.events), 2)
        self.assertEqual(room.state, [])

    def test_an_invited_room_without_invite_state_parses(self):
        response = SyncResponse.from_dict(_invited_without_invite_state())

        self.assertIsInstance(response, SyncResponse)
        self.assertEqual(response.rooms.invite[V12_ROOM].invite_state, [])

    def test_a_left_room_with_state_is_unchanged(self):
        data = copy.deepcopy(INCREMENTAL_SYNC_AFTER_LEAVING)
        member = data["rooms"]["leave"][V12_ROOM]["timeline"]["events"][1]
        data["rooms"]["leave"][V12_ROOM]["state"] = {"events": [member]}

        response = SyncResponse.from_dict(data)

        self.assertEqual(len(response.rooms.leave[V12_ROOM].state), 1)

    def test_canary_unpatched_nio_rejects_a_left_v12_room_without_state(self):
        self.assertEqual(nio_compat.originals["RoomRegex"], "^!.+:.+$")
        with unpatched_room_pattern():
            with self.assertRaises(jsonschema.ValidationError):
                SyncResponse.from_dict(copy.deepcopy(INCREMENTAL_SYNC_AFTER_LEAVING))

    def test_canary_unpatched_nio_rejects_an_invited_v12_room_without_invite_state(
        self,
    ):
        with unpatched_room_pattern():
            with self.assertRaises(jsonschema.ValidationError):
                SyncResponse.from_dict(_invited_without_invite_state())


class ApplyTest(SimpleTestCase):
    def test_refuses_another_nio_version(self):
        with (
            mock.patch.object(nio_compat, "_applied", False),
            mock.patch.object(
                nio_compat.importlib.metadata, "version", return_value="0.27.0"
            ),
        ):
            with self.assertRaises(nio_compat.UnsupportedNioVersion):
                nio_compat.apply()

    def test_applying_twice_patches_once(self):
        patched = sessions.InboundSession.decrypt
        nio_compat.apply()
        self.assertIs(sessions.InboundSession.decrypt, patched)
        for patterns in nio_compat.room_patterns(schemas.Schemas.sync):
            self.assertEqual(list(patterns), [nio_compat.ROOM_ID_PATTERN])
