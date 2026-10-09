import asyncio
from datetime import timedelta
from unittest import mock

import httpx
from asgiref.sync import async_to_sync
from django.db import OperationalError, ProgrammingError
from django.test import TestCase
from django.utils import timezone
from nio import KeysQueryResponse, OlmUnverifiedDeviceError, RoomSendResponse
from nio.crypto import OlmAccount, OlmDevice

from waldur_core.permissions.fixtures import ProjectRole
from waldur_mastermind.matrix_chat import bot, bot_state, models, tasks
from waldur_mastermind.matrix_chat.crypto import cross_signing
from waldur_mastermind.matrix_chat.management.commands import matrix_bot
from waldur_mastermind.matrix_chat.tests import fixtures
from waldur_mastermind.matrix_chat.tests.test_cross_signing import Identity

BOT_USER = "@waldur-bot:matrix.example.com"


def _settings():
    return bot.BotSettings(
        homeserver_url="https://matrix.example.com",
        as_token="as-token",
        localpart="waldur-bot",
        user_id=BOT_USER,
        display_name="Waldur Bot",
    )


class LeaseTest(TestCase):
    def test_one_holder_at_a_time(self):
        bot_state.acquire_lease(BOT_USER, "first")
        with self.assertRaises(bot_state.LeaseHeld):
            bot_state.acquire_lease(BOT_USER, "second")
        self.assertTrue(bot_state.is_bot_running(BOT_USER))

    def test_an_expired_lease_is_taken_over_and_its_holder_cannot_renew(self):
        bot_state.acquire_lease(BOT_USER, "first")
        # Leases run on the database's clock, so they are expired there.
        models.MatrixBotIdentity.objects.update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertFalse(bot_state.is_bot_running(BOT_USER))
        self.assertFalse(bot_state.renew_lease(BOT_USER, "first"))
        bot_state.acquire_lease(BOT_USER, "second")
        self.assertFalse(bot_state.renew_lease(BOT_USER, "first"))
        self.assertTrue(bot_state.renew_lease(BOT_USER, "second"))

    def test_release_lets_the_next_one_start(self):
        bot_state.acquire_lease(BOT_USER, "first")
        bot_state.release_lease(BOT_USER, "first")
        self.assertFalse(bot_state.is_bot_running(BOT_USER))
        bot_state.acquire_lease(BOT_USER, "second")

    def test_identity_is_created_once_with_its_own_secrets(self):
        first = bot_state.get_or_create_identity(BOT_USER)
        again = bot_state.get_or_create_identity(BOT_USER)
        self.assertEqual(first.pk, again.pk)
        self.assertTrue(first.device_id.startswith("WALDURBOT"))
        self.assertGreaterEqual(len(first.pickle_key), 32)

    def test_cross_signing_seeds_round_trip(self):
        identity = bot_state.get_or_create_identity(BOT_USER)
        self.assertEqual(bot_state.load_cross_signing_seeds(identity), {})
        seeds = {"master": b"\x01" * 32, "self_signing": b"\x02" * 32}
        bot_state.save_cross_signing_seeds(identity, seeds)
        identity.refresh_from_db()
        self.assertEqual(bot_state.load_cross_signing_seeds(identity), seeds)


class OutboxTest(TestCase):
    def setUp(self):
        self.room = fixtures.MatrixChatFixture().matrix_room

    def test_a_reply_to_an_event_is_queued_once(self):
        first = bot_state.enqueue(self.room, "one", reply_to="$event")
        second = bot_state.enqueue(self.room, "two", reply_to="$event")
        self.assertEqual(first.pk, second.pk)
        bot_state.enqueue(self.room, "plain")
        bot_state.enqueue(self.room, "plain")
        self.assertEqual(models.MatrixOutboxMessage.objects.count(), 3)

    def test_failures_back_off_then_give_up(self):
        message = bot_state.enqueue(self.room, "hello")
        for attempt in range(1, bot_state.OUTBOX_MAX_ATTEMPTS):
            bot_state.mark_failed_attempt(message, "boom")
            message.refresh_from_db()
            self.assertEqual(message.state, models.OutboxStates.PENDING)
            self.assertGreater(message.next_attempt_at, timezone.now())
            self.assertNotIn(message, bot_state.due_messages())
            self.assertEqual(message.attempts, attempt)
        bot_state.mark_failed_attempt(message, "boom")
        message.refresh_from_db()
        self.assertEqual(message.state, models.OutboxStates.FAILED)

    def test_wait_until_sent(self):
        message = bot_state.enqueue(self.room, "hello")
        bot_state.mark_sent(message, "$sent")
        self.assertTrue(bot_state.wait_until_sent(message, 1))
        pending = bot_state.enqueue(self.room, "later")
        with mock.patch.object(bot_state.time, "sleep"):
            self.assertFalse(bot_state.wait_until_sent(pending, 0))

    def test_notices_go_before_command_replies(self):
        reply = bot_state.enqueue(self.room, "reply", reply_to="$event")
        notice = bot_state.enqueue(self.room, "notice")
        self.assertEqual(bot_state.due_messages(), [notice, reply])

    def test_command_replies_are_limited_per_room(self):
        for i in range(bot_state.REPLY_LIMIT_PER_MINUTE):
            bot_state.enqueue(self.room, "reply", reply_to=f"$event{i}")
        bot_state.enqueue(self.room, "notice")
        self.assertFalse(bot_state.may_reply(self.room))

    def test_cleanup_keeps_pending_and_recent_messages(self):
        old_sent = bot_state.enqueue(self.room, "old")
        bot_state.mark_sent(old_sent, "$1")
        pending = bot_state.enqueue(self.room, "pending")
        models.MatrixOutboxMessage.objects.update(
            modified=timezone.now() - timedelta(days=tasks.OUTBOX_RETENTION_DAYS + 1)
        )
        recent = bot_state.enqueue(self.room, "recent")
        bot_state.mark_sent(recent, "$2")

        tasks.cleanup_old_outbox_messages()

        self.assertEqual(
            set(models.MatrixOutboxMessage.objects.values_list("pk", flat=True)),
            {pending.pk, recent.pk},
        )


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class PostAsBotTest(TestCase):
    def setUp(self):
        self.room = fixtures.MatrixChatFixture().matrix_room

    def test_queues_for_a_running_bot(self, matrix_client):
        matrix_client.get_bot_user_id.return_value = BOT_USER
        bot_state.acquire_lease(BOT_USER, "holder")

        tasks.post_as_bot(self.room, "hello")

        matrix_client.send_message.assert_not_called()
        self.assertEqual(
            list(models.MatrixOutboxMessage.objects.values_list("body", flat=True)),
            ["hello"],
        )

    def test_queues_with_no_bot_running_too(self, matrix_client):
        # Rooms are encrypted: nothing but the bot may post in them, so a
        # message waits for it rather than going out in clear.
        tasks.post_as_bot(self.room, "hello")

        self.assertEqual(
            list(models.MatrixOutboxMessage.objects.values_list("body", flat=True)),
            ["hello"],
        )


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class AnswerCommandTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room
        self.member = self.fixture.user
        self.fixture.project.add_user(self.member, ProjectRole.MEMBER)
        models.MatrixUserProfile.objects.create(
            user=self.member, matrix_user_id="@member:matrix.example.com"
        )

    def _reply(self):
        return models.MatrixOutboxMessage.objects.get(room=self.room)

    def test_answers_a_project_member(self, matrix_client):
        matrix_client.is_enabled.return_value = True
        tasks.answer_command(
            self.room.room_id, "@member:matrix.example.com", "$event", "!help"
        )
        reply = self._reply()
        self.assertEqual(reply.reply_to, "$event")
        self.assertIn("Available commands", reply.body)

    def test_refuses_a_sender_without_a_role(self, matrix_client):
        matrix_client.is_enabled.return_value = True
        tasks.answer_command(
            self.room.room_id, "@stranger:other.example.com", "$event", "!status"
        )
        self.assertEqual(self._reply().body, tasks.ACCESS_DENIED_REPLY)

    def test_ignores_rooms_waldur_does_not_know(self, matrix_client):
        matrix_client.is_enabled.return_value = True
        tasks.answer_command(
            "!elsewhere:matrix.example.com", "@member:matrix.example.com", "$e", "!help"
        )
        self.assertFalse(models.MatrixOutboxMessage.objects.exists())


def _device(user_id, device_id, ed25519):
    return OlmDevice(user_id, device_id, {"ed25519": ed25519, "curve25519": "c"})


class FakeDeviceStore:
    def __init__(self, devices):
        self.devices = devices

    @property
    def users(self):
        return {device.user_id for device in self.devices}

    def active_user_devices(self, user_id):
        return [d for d in self.devices if d.user_id == user_id]


class TrustTest(TestCase):
    def setUp(self):
        self.bot = bot.MatrixBot("holder", _settings())
        self.bot.identity = mock.Mock(device_id="BOTDEVICE")
        self.alice = Identity("@alice:test")
        signed = self.alice.add_device("SIGNED")
        self.alice.add_device("UNSIGNED", signed=False)
        self.signed = _device("@alice:test", "SIGNED", signed["keys"]["ed25519:SIGNED"])
        self.unsigned = _device("@alice:test", "UNSIGNED", "whatever")
        # Listed by nio under the signed id, but with other keys than the
        # homeserver's signed answer: a swapped device.
        self.swapped = _device(
            "@alice:test", "SIGNED", OlmAccount().identity_keys["ed25519"]
        )
        self.bot.client = mock.Mock()

    def _mark(self, devices):
        self.bot.client.device_store = FakeDeviceStore(devices)
        with mock.patch.object(
            self.bot, "query_keys", mock.AsyncMock(return_value=self.alice.response())
        ):
            async_to_sync(self.bot.mark_trust)({"@alice:test"})

    def test_verifies_signed_devices_and_blacklists_the_rest(self):
        self._mark([self.signed, self.unsigned])
        self.bot.client.verify_device.assert_called_once_with(self.signed)
        self.bot.client.blacklist_device.assert_called_once_with(self.unsigned)

    def test_a_device_whose_keys_differ_from_the_signed_ones_is_blacklisted(self):
        self._mark([self.swapped])
        self.bot.client.verify_device.assert_not_called()
        self.bot.client.blacklist_device.assert_called_once_with(self.swapped)

    def test_no_other_device_of_the_bot_user_is_trusted(self):
        bots_own = _device(BOT_USER, "BOTDEVICE", "own")
        signed_by_anyone = _device(BOT_USER, "SECOND", "second")
        self.bot.client.device_store = FakeDeviceStore([bots_own, signed_by_anyone])
        with mock.patch.object(self.bot, "query_keys", mock.AsyncMock()) as query:
            async_to_sync(self.bot.mark_trust)({BOT_USER})
        query.assert_not_called()
        self.bot.client.verify_device.assert_not_called()
        self.bot.client.blacklist_device.assert_called_once_with(signed_by_anyone)

    def test_every_keys_query_is_followed_by_a_judgement(self):
        response = KeysQueryResponse({"@alice:test": {}}, {})
        self.bot.client.keys_query = mock.AsyncMock(return_value=response)
        self.bot._judge_after_every_keys_query()
        with mock.patch.object(self.bot, "mark_trust", mock.AsyncMock()) as mark:
            self.assertIs(async_to_sync(self.bot.client.keys_query)(), response)
        mark.assert_awaited_once()
        self.assertEqual(set(mark.await_args.args[0]), {"@alice:test"})

    def test_a_failed_judgement_distrusts_and_queries_again(self):
        response = KeysQueryResponse({"@alice:test": {}}, {})
        self.bot.client.keys_query = mock.AsyncMock(return_value=response)
        self.bot.client.device_store = FakeDeviceStore([self.signed])
        self.bot.client.olm.users_for_key_query = set()
        self.bot._judge_after_every_keys_query()
        with mock.patch.object(
            self.bot, "query_keys", mock.AsyncMock(side_effect=bot.TransientError())
        ):
            with self.assertRaises(bot.TransientError):
                async_to_sync(self.bot.client.keys_query)()
        self.bot.client.blacklist_device.assert_called_once_with(self.signed)
        self.assertEqual(self.bot.client.olm.users_for_key_query, {"@alice:test"})

    def test_a_device_that_lost_its_signature_is_blacklisted(self):
        self.signed.trust_state = self.signed.trust_state.verified
        self.alice.devices["SIGNED"]["signatures"] = {}
        self._mark([self.signed])
        self.bot.client.blacklist_device.assert_called_once_with(self.signed)


class SendTest(TestCase):
    def setUp(self):
        self.room = fixtures.MatrixChatFixture().matrix_room
        self.message = bot_state.enqueue(self.room, "hello", reply_to="$event")
        self.bot = bot.MatrixBot("holder", _settings())
        self.bot.client = mock.Mock()

    def _send(self):
        (message,) = bot_state.due_messages()
        async_to_sync(self.bot.send)(message)
        self.message.refresh_from_db()

    def test_sends_without_ignoring_unverified_devices(self):
        self.bot.client.room_send = mock.AsyncMock(
            return_value=RoomSendResponse("$sent", self.room.room_id)
        )
        self._send()
        self.assertEqual(self.message.state, models.OutboxStates.SENT)
        self.assertEqual(self.message.event_id, "$sent")
        args, kwargs = self.bot.client.room_send.call_args
        self.assertFalse(kwargs["ignore_unverified_devices"])
        self.assertEqual(
            args[2]["m.relates_to"], {"m.in_reply_to": {"event_id": "$event"}}
        )

    def test_an_unjudged_device_is_judged_and_the_message_retried(self):
        device = _device("@alice:test", "NEW", "key")
        self.bot.client.rooms = {
            self.room.room_id: mock.Mock(users={"@bob:test": None})
        }
        self.bot.client.room_send = mock.AsyncMock(
            side_effect=OlmUnverifiedDeviceError(device)
        )
        with mock.patch.object(self.bot, "mark_trust", mock.AsyncMock()) as mark:
            self._send()
        mark.assert_awaited_once_with({"@alice:test", "@bob:test"})
        self.assertEqual(self.message.state, models.OutboxStates.PENDING)
        self.assertEqual(self.message.attempts, 1)

    def test_a_transient_failure_is_retried_not_fatal(self):
        self.bot.client.room_send = mock.AsyncMock(side_effect=httpx.ConnectError("x"))
        self._send()
        self.assertEqual(self.message.state, models.OutboxStates.PENDING)
        self.assertEqual(self.message.attempts, 1)

    def test_a_room_not_encrypted_gets_encryption_and_nothing_in_clear(self):
        self.bot.client.rooms = {self.room.room_id: mock.Mock(encrypted=False)}
        self.bot.client.room_send = mock.AsyncMock()
        self.bot.client.room_put_state = mock.AsyncMock()
        self._send()
        self.bot.client.room_send.assert_not_called()
        self.bot.client.room_put_state.assert_awaited_once_with(
            self.room.room_id,
            "m.room.encryption",
            {"algorithm": "m.megolm.v1.aes-sha2"},
        )
        self.assertEqual(self.message.state, models.OutboxStates.PENDING)

    def test_an_archived_room_gets_nothing(self):
        models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=models.RoomStates.ARCHIVED
        )
        self.bot.client.room_send = mock.AsyncMock()
        self._send()
        self.bot.client.room_send.assert_not_called()
        self.assertEqual(self.message.state, models.OutboxStates.FAILED)


class CrossSigningBootstrapTest(TestCase):
    def setUp(self):
        self.bot = bot.MatrixBot("holder", _settings())
        self.bot.identity = bot_state.get_or_create_identity(BOT_USER)
        self.bot.client = mock.Mock(access_token="token")
        self.bot.client.olm.account.identity_keys = {
            "curve25519": "curvekey",
            "ed25519": "devicekey",
        }
        self.bot.client.olm._algorithms = ["m.olm.v1.curve25519-aes-sha2"]
        self.device = self.bot._own_device_keys()
        self.published = {}
        self.calls = []

    def _query(self, users):
        return {
            "device_keys": {
                BOT_USER: {
                    self.bot.identity.device_id: self.device,
                    **getattr(self, "extra_devices", {}),
                }
            },
            "master_keys": {BOT_USER: self.published["master_key"]}
            if self.published
            else {},
            "self_signing_keys": {BOT_USER: self.published["self_signing_key"]}
            if self.published
            else {},
        }

    async def _call(self, method, path, token, json=None):
        self.calls.append(path)
        if path.endswith("/keys/device_signing/upload"):
            self.published.update(json)
        if path.endswith("/keys/signatures/upload"):
            self.device = json[BOT_USER][self.bot.identity.device_id]
        return 200, {}

    def _ensure(self):
        with (
            mock.patch.object(self.bot, "query_keys", side_effect=self._async_query),
            mock.patch.object(self.bot, "_call", side_effect=self._call),
        ):
            async_to_sync(self.bot.ensure_cross_signed)()

    async def _async_query(self, users):
        return self._query(users)

    def test_first_start_publishes_keys_and_signs_the_device(self):
        self._ensure()
        seeds = bot_state.load_cross_signing_seeds(self.bot.identity)
        self_signing = cross_signing.SigningKey(seeds[cross_signing.SELF_SIGNING])
        self.assertTrue(
            cross_signing.has_signature(self.device, BOT_USER, self_signing.public_key)
        )
        self.assertEqual(
            cross_signing.self_signing_key(BOT_USER, self._query([])),
            self_signing.public_key,
        )

    def test_a_restart_changes_nothing(self):
        self._ensure()
        self.calls.clear()
        self._ensure()
        self.assertEqual(self.calls, [])

    def test_other_devices_of_the_bot_user_are_reported(self):
        self.extra_devices = {"STRANGER": {"device_id": "STRANGER"}}
        with self.assertLogs(bot.logger, "WARNING") as logs:
            self._ensure()
        self.assertIn("STRANGER", "\n".join(logs.output))

    def test_other_keys_listed_for_the_bots_device_are_never_signed(self):
        self.device = {
            **self.device,
            "keys": {f"ed25519:{self.bot.identity.device_id}": "attackerkey"},
        }
        with self.assertRaises(bot.BotError):
            self._ensure()
        self.assertNotIn("/_matrix/client/v3/keys/signatures/upload", self.calls)

    def test_keys_waldur_does_not_hold_stop_the_bot(self):
        stranger = Identity(BOT_USER).response()
        self.published = {
            "master_key": stranger["master_keys"][BOT_USER],
            "self_signing_key": stranger["self_signing_keys"][BOT_USER],
        }
        with self.assertRaises(bot.BotError):
            self._ensure()

    def test_keys_replaced_on_the_homeserver_stop_the_bot(self):
        self._ensure()
        stranger = Identity(BOT_USER).response()
        self.published = {
            "master_key": stranger["master_keys"][BOT_USER],
            "self_signing_key": stranger["self_signing_keys"][BOT_USER],
        }
        with self.assertRaises(bot.BotError):
            self._ensure()


class AccessTokenTest(TestCase):
    def setUp(self):
        self.bot = bot.MatrixBot("holder", _settings())
        self.bot.identity = bot_state.get_or_create_identity(BOT_USER)
        self.whoami = {"user_id": BOT_USER, "device_id": self.bot.identity.device_id}

    def _save(self, token, homeserver="https://matrix.example.com"):
        bot_state.save_access_token(self.bot.identity, token, homeserver)

    def _token(self, whoami_status, whoami=None):
        self.whoami_calls = 0

        async def call(method, path, token, json=None):
            self.assertTrue(path.endswith("/account/whoami"))
            self.whoami_calls += 1
            return whoami_status, whoami or self.whoami

        with (
            mock.patch.object(self.bot, "_call", side_effect=call),
            mock.patch.object(
                self.bot, "_login", mock.AsyncMock(return_value="new-token")
            ) as login,
        ):
            token = async_to_sync(self.bot._access_token)()
        return token, login

    def test_a_token_the_homeserver_accepts_is_reused(self):
        self._save("kept-token")
        token, login = self._token(200)
        self.assertEqual(token, "kept-token")
        login.assert_not_awaited()

    def test_a_rejected_token_is_replaced_and_kept(self):
        self._save("old-token")
        token, login = self._token(401, {"errcode": "M_UNKNOWN_TOKEN"})
        self.assertEqual(token, "new-token")
        self.bot.identity.refresh_from_db()
        self.assertEqual(self.bot.identity.access_token, "new-token")

    def test_a_token_for_another_device_is_not_used(self):
        self._save("other-device-token")
        self.whoami["device_id"] = "SOMEOTHER"
        token, _ = self._token(200)
        self.assertEqual(token, "new-token")

    def test_a_homeserver_error_keeps_the_token_and_signs_in_nowhere(self):
        self._save("kept-token")
        with self.assertRaises(bot.TransientError):
            self._token(502)

    def test_a_token_of_another_homeserver_is_never_sent(self):
        self._save("foreign-token", homeserver="https://old.example.com")
        token, _ = self._token(200)
        self.assertEqual(token, "new-token")
        self.assertEqual(self.whoami_calls, 0)


@mock.patch("waldur_mastermind.matrix_chat.tasks.bot_state.wait_until_sent")
class PostAsBotWaitTest(TestCase):
    def setUp(self):
        self.room = fixtures.MatrixChatFixture().matrix_room

    def test_waits_only_for_a_running_bot(self, wait_until_sent):
        tasks.post_as_bot(self.room, "notice", wait=True)
        wait_until_sent.assert_not_called()

        bot_state.acquire_lease(tasks.matrix_client.get_bot_user_id(), "holder")
        tasks.post_as_bot(self.room, "notice again", wait=True)
        wait_until_sent.assert_called_once()


class EnsureRoomsEncryptedTest(TestCase):
    def test_turns_encryption_on_in_waldur_rooms_without_it(self):
        room = fixtures.MatrixChatFixture().matrix_room
        bot_instance = bot.MatrixBot("holder", _settings())
        bot_instance.client = mock.Mock()
        bot_instance.client.rooms = {
            room.room_id: mock.Mock(encrypted=False),
            "!not-waldurs:test": mock.Mock(encrypted=False),
        }
        bot_instance.client.room_put_state = mock.AsyncMock()

        async_to_sync(bot_instance.ensure_rooms_encrypted)()

        bot_instance.client.room_put_state.assert_awaited_once_with(
            room.room_id, "m.room.encryption", {"algorithm": "m.megolm.v1.aes-sha2"}
        )


class ConfigTest(TestCase):
    def test_the_same_bot_ignores_cosmetic_changes(self):
        settings = _settings()
        renamed = bot.BotSettings(**{**vars(settings), "display_name": "Other Bot"})
        rotated = bot.BotSettings(**{**vars(settings), "as_token": "rotated"})
        self.assertTrue(settings.identifies_the_same_bot(renamed))
        self.assertFalse(settings.identifies_the_same_bot(rotated))

    def test_a_rotated_token_stops_the_bot(self):
        bot_instance = bot.MatrixBot("holder", _settings())
        rotated = bot.BotSettings(**{**vars(_settings()), "as_token": "rotated"})
        with (
            mock.patch.object(bot, "CONFIG_CHECK_SECONDS", 0),
            mock.patch.object(bot.BotSettings, "from_config", return_value=rotated),
        ):
            with self.assertRaises(bot.ConfigChanged):
                async_to_sync(bot_instance.watch_config_forever)()


class CommandTest(TestCase):
    def _command(self):
        return matrix_bot, matrix_bot.Command()

    def test_waits_for_configuration_and_stops_on_request(self):
        matrix_bot, command = self._command()
        stopped = mock.Mock(is_set=mock.Mock(side_effect=[False, True]))
        with mock.patch.object(
            matrix_bot.matrix_client, "is_homeserver_configured", return_value=False
        ):
            self.assertIsNone(command._wait_for_configuration(stopped))
        stopped.wait.assert_called_once_with(matrix_bot.CONFIG_POLL_SECONDS)

    def test_waits_while_the_database_is_not_migrated(self):
        matrix_bot, command = self._command()
        stopped = mock.Mock(is_set=mock.Mock(side_effect=[False, False, False]))
        with (
            mock.patch.object(
                matrix_bot.matrix_client,
                "is_homeserver_configured",
                side_effect=[ProgrammingError("no such table"), True],
            ),
            mock.patch.object(
                matrix_bot.BotSettings, "from_config", return_value=_settings()
            ),
        ):
            self.assertEqual(command._wait_for_configuration(stopped), _settings())
        stopped.wait.assert_called_once()

    def test_an_unreachable_database_is_not_waited_out(self):
        matrix_bot, command = self._command()
        stopped = mock.Mock(is_set=mock.Mock(return_value=False))
        with mock.patch.object(
            matrix_bot.matrix_client,
            "is_homeserver_configured",
            side_effect=OperationalError("connection refused"),
        ):
            with self.assertRaises(OperationalError):
                command._wait_for_configuration(stopped)

    def test_starts_over_when_the_settings_change(self):
        matrix_bot, command = self._command()
        settings = _settings()
        runs = [bot.ConfigChanged(), None]

        async def run(holder, settings, stopped):
            outcome = runs.pop(0)
            if outcome:
                raise outcome

        with (
            mock.patch.object(
                command, "_wait_for_configuration", return_value=settings
            ),
            mock.patch.object(command, "_run", side_effect=run),
        ):
            command.handle()

        self.assertEqual(runs, [])
        self.assertFalse(bot_state.is_bot_running(settings.user_id))


class StopTest(TestCase):
    def _bot(self):
        bot_instance = bot.MatrixBot("holder", _settings())
        bot_instance.client = mock.Mock(close=mock.AsyncMock())
        bot_instance.start = mock.AsyncMock()
        return bot_instance

    def test_a_stop_wins_over_a_settings_change(self):
        bot_instance = self._bot()

        async def run():
            stop = asyncio.Event()
            stop.set()

            async def changed():
                raise bot.ConfigChanged()

            with (
                mock.patch.object(bot_instance, "sync_forever", changed),
                mock.patch.object(bot_instance, "drain_outbox_forever", changed),
                mock.patch.object(bot_instance, "renew_lease_forever", changed),
                mock.patch.object(bot_instance, "watch_config_forever", changed),
            ):
                await bot_instance.run(stop)

        async_to_sync(run)()  # returns instead of raising ConfigChanged

    def test_the_store_connection_is_closed(self):
        bot_instance = self._bot()

        async def run():
            stop = asyncio.Event()
            stop.set()
            await bot_instance.run(stop)

        with (
            mock.patch.object(bot_instance, "sync_forever", asyncio.Event().wait),
            mock.patch.object(
                bot_instance, "drain_outbox_forever", asyncio.Event().wait
            ),
            mock.patch.object(
                bot_instance, "renew_lease_forever", asyncio.Event().wait
            ),
            mock.patch.object(
                bot_instance, "watch_config_forever", asyncio.Event().wait
            ),
        ):
            async_to_sync(run)()
        bot_instance.client.store.database.close.assert_called_once()

    def _run_with(self, bot_instance, stop, sync, config):
        idle = asyncio.Event().wait

        async def run():
            with (
                mock.patch.object(bot_instance, "sync_forever", sync),
                mock.patch.object(bot_instance, "drain_outbox_forever", idle),
                mock.patch.object(bot_instance, "renew_lease_forever", idle),
                mock.patch.object(bot_instance, "watch_config_forever", config),
            ):
                await bot_instance.run(stop)

        return run

    def _sender(self, bot_instance, order, seconds=0.05):
        async def send():
            async with bot_instance._sending:
                order.append("sending")
                await asyncio.sleep(seconds)
                order.append("sent")
            await asyncio.Event().wait()

        return send

    def test_a_settings_change_waits_for_the_message_being_sent(self):
        bot_instance = self._bot()
        order = []

        async def changed():
            await asyncio.sleep(0.01)
            order.append("changed")
            raise bot.ConfigChanged()

        run = self._run_with(
            bot_instance, asyncio.Event(), self._sender(bot_instance, order), changed
        )
        with self.assertRaises(bot.ConfigChanged):
            async_to_sync(run)()
        self.assertEqual(order, ["sending", "changed", "sent"])

    def test_a_stop_waits_for_the_message_being_sent(self):
        bot_instance = self._bot()
        order = []

        async def stop_soon(stop):
            await asyncio.sleep(0.01)
            order.append("stop")
            stop.set()

        async def run():
            stop = asyncio.Event()
            asyncio.get_running_loop().create_task(stop_soon(stop))
            await self._run_with(
                bot_instance,
                stop,
                self._sender(bot_instance, order),
                asyncio.Event().wait,
            )()

        async_to_sync(run)()
        self.assertEqual(order, ["sending", "stop", "sent"])

    def test_a_hanging_send_does_not_keep_the_bot_from_stopping(self):
        bot_instance = self._bot()
        order = []

        async def changed():
            await asyncio.sleep(0.01)
            raise bot.ConfigChanged()

        run = self._run_with(
            bot_instance,
            asyncio.Event(),
            self._sender(bot_instance, order, seconds=3600),
            changed,
        )
        with (
            mock.patch.object(bot, "SEND_FINISH_SECONDS", 0.05),
            self.assertLogs(bot.logger, "WARNING"),
        ):
            with self.assertRaises(bot.ConfigChanged):
                async_to_sync(run)()
        self.assertEqual(order, ["sending"])


class SyncOutageTest(TestCase):
    def test_an_unreachable_homeserver_is_waited_out(self):
        bot_instance = bot.MatrixBot("holder", _settings())
        bot_instance.client = mock.Mock()
        attempts = []

        async def sync(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise httpx.ConnectError("homeserver down")
            raise asyncio.CancelledError  # ends the loop after the retry

        bot_instance.client.sync = sync
        with (
            mock.patch.object(bot_instance, "_renew_lease", mock.AsyncMock()),
            mock.patch.object(bot.asyncio, "sleep", mock.AsyncMock()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                async_to_sync(bot_instance.sync_forever)()
        self.assertEqual(len(attempts), 2)
