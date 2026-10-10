import copy
import hashlib
import hmac
import json
from unittest import mock

import vodozemac
from asgiref.sync import async_to_sync
from constance.test import override_config
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from nio.crypto import (
    GroupSessionStore,
    InboundGroupSession,
    OlmDevice,
    OutboundGroupSession,
)
from rest_framework import test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.matrix_chat import (
    bot,
    crypto_setup,
    history_backfill,
    history_filler,
    matrix_client,
    models,
    tasks,
)
from waldur_mastermind.matrix_chat.crypto import cross_signing, key_backup
from waldur_mastermind.matrix_chat.tests import fixtures
from waldur_mastermind.matrix_chat.tests.test_cross_signing import Identity
from waldur_mastermind.matrix_chat.tests.test_recovery_keys import (
    KEY_INFO,
    RECOVERY_KEY,
)

ROOM = "!test_room:matrix.example.com"
USER = "@alice:matrix.example.com"
SENDER = "@bob:matrix.example.com"

# Written by matrix-js-sdk 40.2 (encryptAESSecretStorageItem): the backup private
# key bytes(range(32, 64)) stored as m.megolm_backup.v1 under the secret-storage
# key bytes(range(32)), which RECOVERY_KEY encodes and KEY_INFO describes.
BACKUP_SECRET = {
    "encrypted": {
        "KEYID": {
            "iv": "Y9H+JKsRsvBe/79qFl64+Q==",
            "ciphertext": "kYX9YLCusuPqJFBd+KqlqWJQFjwIbgSFS7sUcLoUgZORnM/ZuyqVOVnzaQ==",
            "mac": "IzIyhju+1zYhsP19DA9nslUMkL8c4S/v3yX6JSxXRMs=",
        }
    }
}
# The public half of bytes(range(32, 64)), as vodozemac derives it.
BACKUP_PUBLIC_KEY = "NYBy1jZYgNGu6jKa35EhODhR7SGijjt16WXQ0s0WYlQ"
BACKUP_PRIVATE_KEY = vodozemac.Curve25519SecretKey.from_bytes(bytes(range(32, 64)))


def _version(public_key=BACKUP_PUBLIC_KEY, version="1", signatures=None):
    return {
        "version": version,
        "algorithm": key_backup.BACKUP_ALGORITHM,
        "auth_data": {"public_key": public_key, "signatures": signatures or {}},
    }


def _room_session(room_id=ROOM):
    outbound = OutboundGroupSession()
    inbound = InboundGroupSession(
        outbound.session_key, "sender-ed25519", "sender-curve25519", room_id
    )
    return outbound, inbound


def _decrypt(backup_data, private_key=BACKUP_PRIVATE_KEY):
    data = backup_data["session_data"]
    message = vodozemac.Message(
        *(cross_signing.decode(data[k]) for k in ("ciphertext", "mac", "ephemeral"))
    )
    return json.loads(vodozemac.PkDecryption.from_key(private_key).decrypt(message))


class SessionBackupTest(SimpleTestCase):
    def test_the_member_decrypts_the_session_and_reads_old_messages(self):
        outbound, inbound = _room_session()
        outbound.mark_as_shared()
        ciphertext = outbound.encrypt("before you joined")

        backup = key_backup.session_backup(inbound, BACKUP_PUBLIC_KEY)

        self.assertEqual(backup["first_message_index"], 0)
        self.assertEqual(backup["forwarded_count"], 0)
        self.assertFalse(backup["is_verified"])
        for value in backup["session_data"].values():
            self.assertFalse(value.endswith("="))
        session = _decrypt(backup)
        self.assertEqual(session["algorithm"], "m.megolm.v1.aes-sha2")
        self.assertEqual(session["sender_key"], "sender-curve25519")
        self.assertEqual(session["sender_claimed_keys"], {"ed25519": "sender-ed25519"})
        restored = vodozemac.InboundGroupSession.import_session(
            vodozemac.ExportedSessionKey(session["session_key"])
        )
        decrypted = restored.decrypt(vodozemac.MegolmMessage.from_base64(ciphertext))
        self.assertEqual(decrypted.plaintext, b"before you joined")

    def test_the_format_is_libolms_including_its_empty_mac(self):
        # Decrypted without vodozemac, as libolm defines the format: X25519,
        # HKDF-SHA256 (no salt, no info) into an AES-256-CBC key, an HMAC key
        # and an IV, PKCS#7 padding, and libolm's quirk: the MAC is HMAC-SHA256
        # over the EMPTY string, truncated to 8 bytes.
        _, inbound = _room_session()
        data = key_backup.session_backup(inbound, BACKUP_PUBLIC_KEY)["session_data"]
        ephemeral, ciphertext, mac = (
            cross_signing.decode(data[k]) for k in ("ephemeral", "ciphertext", "mac")
        )
        shared = X25519PrivateKey.from_private_bytes(bytes(range(32, 64))).exchange(
            X25519PublicKey.from_public_bytes(ephemeral)
        )
        derived = HKDF(algorithm=hashes.SHA256(), length=80, salt=b"", info=b"").derive(
            shared
        )
        aes_key, mac_key, iv = derived[:32], derived[32:64], derived[64:]
        self.assertEqual(mac, hmac.new(mac_key, b"", hashlib.sha256).digest()[:8])
        self.assertNotEqual(
            mac, hmac.new(mac_key, ciphertext, hashlib.sha256).digest()[:8]
        )
        decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        session = json.loads(unpadder.update(padded) + unpadder.finalize())
        self.assertEqual(session["sender_key"], "sender-curve25519")
        self.assertEqual(
            session["session_key"],
            inbound.export_session(inbound.first_known_index),
        )

    def test_another_key_cannot_read_it(self):
        _, inbound = _room_session()
        backup = key_backup.session_backup(inbound, BACKUP_PUBLIC_KEY)
        with self.assertRaises(Exception):
            _decrypt(backup, vodozemac.Curve25519SecretKey())


class BackupTrustTest(SimpleTestCase):
    def test_the_recovery_key_reveals_the_backup_key_in_secret_storage(self):
        self.assertEqual(
            key_backup.key_in_secret_storage(
                RECOVERY_KEY, BACKUP_SECRET, {"KEYID": KEY_INFO}
            ),
            BACKUP_PUBLIC_KEY,
        )

    def test_a_key_that_opens_nothing_reveals_nothing(self):
        other = {**KEY_INFO, "mac": "AyiqcamvFdehcNjzzGERKt32yZxP1qY4fG+rEOH/Yio"}
        tampered = copy.deepcopy(BACKUP_SECRET)
        tampered["encrypted"]["KEYID"]["ciphertext"] = (
            "AAAA" + tampered["encrypted"]["KEYID"]["ciphertext"][4:]
        )
        for secret, descriptions, recovery_key in (
            (BACKUP_SECRET, {"KEYID": other}, RECOVERY_KEY),
            (BACKUP_SECRET, {}, RECOVERY_KEY),
            (tampered, {"KEYID": KEY_INFO}, RECOVERY_KEY),
            (None, {"KEYID": KEY_INFO}, RECOVERY_KEY),
            (BACKUP_SECRET, {"KEYID": KEY_INFO}, ""),
            (BACKUP_SECRET, {"KEYID": KEY_INFO}, "not a key"),
        ):
            with self.subTest(descriptions=descriptions, recovery_key=recovery_key):
                self.assertIsNone(
                    key_backup.key_in_secret_storage(recovery_key, secret, descriptions)
                )

    def test_signed_by_the_master_key(self):
        identity = Identity(USER)
        auth_data = {"public_key": BACKUP_PUBLIC_KEY}
        stranger = cross_signing.SigningKey.generate()
        for signed, expected in (
            (identity.master.sign(auth_data, USER), True),
            (auth_data, False),
            (stranger.sign(auth_data, USER), False),
        ):
            with self.subTest(signed=signed):
                self.assertEqual(
                    key_backup.signed_by_owner(
                        {"auth_data": signed}, USER, identity.response()
                    ),
                    expected,
                )

    def test_signed_by_a_device_the_identity_vouches_for(self):
        identity = Identity(USER)
        identity.add_device("DEVICE")
        identity.add_device("ROGUE", signed=False)
        # A device signs with its own Ed25519 key, under its device id.
        device_key = cross_signing.SigningKey.generate()
        rogue_key = cross_signing.SigningKey.generate()
        for device_id, key in (("DEVICE", device_key), ("ROGUE", rogue_key)):
            keys = {
                "user_id": USER,
                "device_id": device_id,
                "algorithms": [],
                "keys": {f"ed25519:{device_id}": key.public_key},
            }
            if device_id == "DEVICE":
                keys = identity.self_signing.sign(keys, USER)
            identity.devices[device_id] = keys

        def signed_by(key, device_id):
            signature = key.sign({"public_key": BACKUP_PUBLIC_KEY}, USER)["signatures"][
                USER
            ][key.key_id]
            return {
                "public_key": BACKUP_PUBLIC_KEY,
                "signatures": {USER: {f"ed25519:{device_id}": signature}},
            }

        self.assertTrue(
            key_backup.signed_by_owner(
                {"auth_data": signed_by(device_key, "DEVICE")},
                USER,
                identity.response(),
            )
        )
        self.assertFalse(
            key_backup.signed_by_owner(
                {"auth_data": signed_by(rogue_key, "ROGUE")},
                USER,
                identity.response(),
            )
        )

    def test_only_curve25519_backups_are_written_to(self):
        self.assertEqual(key_backup.backup_public_key(_version()), BACKUP_PUBLIC_KEY)
        for info in (
            None,
            {**_version(), "algorithm": "org.matrix.msc3270.v1.aes-hmac-sha2"},
            _version(public_key="short"),
            {**_version(), "auth_data": None},
        ):
            with self.subTest(info=info):
                self.assertIsNone(key_backup.backup_public_key(info))


class RequestTest(TestCase):
    """Which changes of membership ask the bot for a fill."""

    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room
        self.user = self.fixture.admin

    def _member(self):
        return models.MatrixRoomMember.objects.get(room=self.room, user=self.user)

    def _record(self, state, **extra):
        tasks._record_membership(
            self.room,
            self.user,
            {"matrix_user_id": USER, "membership_state": state, **extra},
        )
        return self._member()

    def test_a_new_member_is_due_a_fill_once(self):
        member = self._record(models.MembershipStates.JOINED)
        self.assertIsNotNone(member.history_due_at)
        history_backfill.mark_filled(member.pk, member.history_due_at, "1")

        member = self._record(models.MembershipStates.JOINED)
        self.assertIsNone(member.history_due_at)

    def test_joining_after_an_invite_and_rejoining_ask_again(self):
        member = self._record(models.MembershipStates.INVITED)
        history_backfill.mark_filled(member.pk, member.history_due_at, "1")
        self.assertIsNone(self._record(models.MembershipStates.INVITED).history_due_at)
        self.assertIsNotNone(
            self._record(models.MembershipStates.JOINED).history_due_at
        )

        member = self._member()
        history_backfill.mark_filled(member.pk, member.history_due_at, "1")
        self._record(models.MembershipStates.LEFT)
        self.assertIsNotNone(
            self._record(models.MembershipStates.JOINED).history_due_at
        )

    def test_a_member_who_left_is_not_due(self):
        self.assertIsNone(self._record(models.MembershipStates.LEFT).history_due_at)

    def test_an_inactive_room_is_not_due(self):
        models.MatrixRoom.objects.filter(pk=self.room.pk).update(
            state=models.RoomStates.ARCHIVED
        )
        self.assertIsNone(self._record(models.MembershipStates.JOINED).history_due_at)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
    def test_staff_join_is_due_a_fill(self, matrix_client):
        matrix_client.is_enabled.return_value = True
        matrix_client.ensure_user_exists.return_value = USER
        staff = UserFactory(is_staff=True)
        tasks.staff_join_room(self.room.uuid.hex, staff.uuid.hex)
        member = models.MatrixRoomMember.objects.get(room=self.room, user=staff)
        self.assertTrue(member.manually_joined)
        self.assertIsNotNone(member.history_due_at)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
    def test_a_role_grant_is_due_a_fill(self, matrix_client):
        matrix_client.is_enabled.return_value = True
        matrix_client.ensure_user_exists.return_value = USER
        matrix_client.get_power_level_for_scope.return_value = 0
        tasks.invite_user_to_room(self.room.uuid.hex, self.user.uuid.hex)
        self.assertIsNotNone(self._member().history_due_at)

    def test_a_retried_fill_backs_off_then_is_dropped(self):
        member = self._record(models.MembershipStates.JOINED)
        for _ in history_backfill.RETRY_DELAYS:
            due_at = member.history_due_at
            history_backfill.mark_retry(member.pk, due_at)
            member.refresh_from_db()
            self.assertGreater(member.history_due_at, timezone.now())
        history_backfill.mark_retry(member.pk, member.history_due_at)
        member.refresh_from_db()
        self.assertIsNone(member.history_due_at)

    def test_a_newer_request_survives_the_outcome_of_an_older_one(self):
        member = self._record(models.MembershipStates.JOINED)
        old_due_at = member.history_due_at
        history_backfill.request_for_user(self.user)
        history_backfill.mark_filled(member.pk, old_due_at, "1")
        member.refresh_from_db()
        self.assertIsNotNone(member.history_due_at)

    def test_a_changed_backup_version_asks_again(self):
        member = self._record(models.MembershipStates.JOINED)
        history_backfill.mark_filled(member.pk, member.history_due_at, "1")
        history_backfill.request_if_version_changed(self.user.pk, "1")
        self.assertIsNone(self._member().history_due_at)
        history_backfill.request_if_version_changed(self.user.pk, "2")
        self.assertIsNotNone(self._member().history_due_at)


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="as-token",
)
@mock.patch.object(matrix_client, "join_room_as_user")
class ViewRequestTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room
        self.user = self.fixture.admin
        self.profile = models.MatrixUserProfile.objects.create(
            user=self.user, matrix_user_id=USER, provisioned=True
        )
        self.member = models.MatrixRoomMember.objects.create(
            room=self.room,
            user=self.user,
            matrix_user_id=USER,
            membership_state=models.MembershipStates.INVITED,
        )
        self.client.force_authenticate(self.user)

    def test_opening_a_room_one_was_invited_to_is_due_a_fill(self, join):
        response = self.client.post(f"/api/matrix/rooms/{self.room.uuid.hex}/open/")
        self.assertEqual(response.status_code, 200, response.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.membership_state, models.MembershipStates.JOINED)
        self.assertIsNotNone(self.member.history_due_at)

    def test_releasing_a_lease_asks_for_every_room_and_keeps_the_pin(self, join):
        for kind in (models.CryptoLeaseKinds.BOOTSTRAP, models.CryptoLeaseKinds.RESET):
            with self.subTest(kind=kind):
                models.MatrixRoomMember.objects.update(history_due_at=None)
                models.MatrixUserProfile.objects.filter(pk=self.profile.pk).update(
                    crypto_lease="lease",
                    crypto_lease_kind=kind,
                    crypto_lease_expires_at=timezone.now() + crypto_setup.LEASE_TTL,
                    pinned_master_key="old-master",
                )
                with mock.patch.object(tasks, "scrub_temporary_matrix_password"):
                    response = self.client.post(
                        "/api/matrix/crypto/lease/release/", {"lease": "lease"}
                    )
                self.assertEqual(response.status_code, 204)
                self.member.refresh_from_db()
                self.assertIsNotNone(self.member.history_due_at)
                self.profile.refresh_from_db()
                # Nothing was escrowed (a cancelled or failed setup, say), so
                # the pin stays: only an escrowed key clears it.
                self.assertEqual(self.profile.pinned_master_key, "old-master")

    def test_escrowing_a_key_starts_the_pin_over_and_asks_again(self, join):
        # A key brought from another client (import) is escrowed through the
        # same view as a new one, so it clears the pin too.
        for kind in models.CryptoLeaseKinds.BOOTSTRAP, models.CryptoLeaseKinds.IMPORT:
            with self.subTest(kind=kind):
                models.MatrixRoomMember.objects.update(history_due_at=None)
                models.MatrixUserProfile.objects.filter(pk=self.profile.pk).update(
                    pinned_master_key="old-master"
                )
                with mock.patch.object(crypto_setup, "escrow", return_value=kind):
                    response = self.client.post(
                        "/api/matrix/crypto/escrow/",
                        {"lease": "lease", "recovery_key": RECOVERY_KEY},
                    )
                self.assertEqual(response.status_code, 204, response.data)
                self.profile.refresh_from_db()
                self.assertEqual(self.profile.pinned_master_key, "")
                self.member.refresh_from_db()
                self.assertIsNotNone(self.member.history_due_at)

    def test_an_import_lease_released_without_escrow_keeps_the_pin(self, join):
        models.MatrixUserProfile.objects.filter(pk=self.profile.pk).update(
            crypto_lease="lease",
            crypto_lease_kind=models.CryptoLeaseKinds.IMPORT,
            crypto_lease_expires_at=timezone.now() + crypto_setup.LEASE_TTL,
            pinned_master_key="old-master",
        )
        response = self.client.post(
            "/api/matrix/crypto/lease/release/", {"lease": "lease"}
        )
        self.assertEqual(response.status_code, 204)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pinned_master_key, "old-master")

    def test_releasing_a_lease_one_does_not_hold_asks_nothing(self, join):
        self.client.post("/api/matrix/crypto/lease/release/", {"lease": "other"})
        self.member.refresh_from_db()
        self.assertIsNone(self.member.history_due_at)


class FakeHomeserver:
    """The few client-server endpoints the filler calls, for one user."""

    def __init__(self, version_info=None, joined=True, backed_up=()):
        self.version_info = version_info
        self.joined = joined
        # The status of the bot's membership read; 403 when it is not in the room.
        self.membership_status = 200
        # Answered to a write: another backup became current meanwhile.
        self.wrong_version = False
        self.backed_up = set(backed_up)
        self.account_data = {}
        self.written = {}
        self.calls = []

    async def request(self, method, path, token, params=None, json=None):
        self.calls.append((method, path, token, dict(params or {})))
        if path == "room_keys/version":
            return (200, self.version_info) if self.version_info else (404, {})
        if "/account_data/" in path:
            kind = path.rsplit("/", 1)[1].replace("%3A", ":")
            data = self.account_data.get(kind)
            return (200, data) if data is not None else (404, {})
        if path.startswith("rooms/"):
            if self.membership_status != 200:
                return self.membership_status, {"errcode": "M_FORBIDDEN"}
            return 200, {"membership": "join" if self.joined else "invite"}
        if path.startswith("room_keys/keys/") and method == "GET":
            return 200, {"sessions": {sid: {} for sid in self.backed_up}}
        if path.startswith("room_keys/keys/") and method == "PUT":
            if self.wrong_version:
                return 403, {"errcode": "M_WRONG_ROOM_KEYS_VERSION"}
            self.written.update(json["sessions"])
            return 200, {"count": len(self.written), "etag": "1"}
        raise AssertionError(f"unexpected {method} {path}")


class FakeDeviceStore:
    def __init__(self, devices):
        self.devices = devices

    def items(self):
        by_user = {}
        for device in self.devices:
            by_user.setdefault(device.user_id, {})[device.id] = device
        return by_user.items()


class FillerTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room
        self.user = self.fixture.admin
        self.fixture.project.add_user(self.user, ProjectRole.MEMBER)
        self.profile = models.MatrixUserProfile.objects.create(
            user=self.user,
            matrix_user_id=USER,
            provisioned=True,
            recovery_key=RECOVERY_KEY,
        )
        tasks._record_membership(
            self.room,
            self.user,
            {
                "matrix_user_id": USER,
                "membership_state": models.MembershipStates.JOINED,
            },
        )
        self.outbound, self.session = _room_session()
        self.other_room_session = _room_session("!other:matrix.example.com")[1]
        store = GroupSessionStore()
        store.add(self.session)
        store.add(self.other_room_session)
        self.bot = mock.Mock(
            homeserver_url="https://matrix.example.com", as_token="as-token"
        )
        self.bot.client.access_token = "bot-token"
        self.bot.client.olm.inbound_group_store = store
        self.bot.client.olm.account.identity_keys = {
            "curve25519": "bot-curve25519",
            "ed25519": "bot-ed25519",
        }
        self.bot.client.rooms = {}
        # The session's sender is a member who has left since: their device
        # still counts, as it sent while in the room.
        models.MatrixRoomMember.objects.create(
            room=self.room,
            user=UserFactory(),
            matrix_user_id=SENDER,
            membership_state=models.MembershipStates.LEFT,
        )
        self.bot.client.device_store = FakeDeviceStore(
            [
                OlmDevice(
                    SENDER,
                    "BOBDEVICE",
                    {"ed25519": "sender-ed25519", "curve25519": "sender-curve25519"},
                    deleted=True,
                )
            ]
        )
        self.bot.query_keys = mock.AsyncMock(return_value={})
        self.filler = history_filler.HistoryFiller(
            self.bot, bot._db, mock.AsyncMock(), bot.TRANSIENT_ERRORS
        )
        self.homeserver = FakeHomeserver(_version())
        self.homeserver.account_data = {
            "m.megolm_backup.v1": BACKUP_SECRET,
            "m.secret_storage.key.KEYID": KEY_INFO,
        }

    def _member(self):
        return models.MatrixRoomMember.objects.get(room=self.room, user=self.user)

    def _fill(self):
        with mock.patch.object(
            self.filler, "_request", side_effect=self.homeserver.request
        ):
            async_to_sync(self.filler.fill_due)()
        return self._member()

    def test_writes_the_rooms_sessions_into_the_members_backup(self):
        member = self._fill()

        self.assertEqual(set(self.homeserver.written), {self.session.id})
        session = _decrypt(self.homeserver.written[self.session.id])
        self.assertEqual(
            session["session_key"],
            self.session.export_session(self.session.first_known_index),
        )
        self.assertIsNone(member.history_due_at)
        self.assertEqual(member.history_backup_version, "1")
        # Everything about the member's backup is asked as the member.
        for method, path, token, params in self.homeserver.calls:
            if path.startswith("rooms/"):
                self.assertEqual(token, "bot-token")
            else:
                self.assertEqual((token, params["user_id"]), ("as-token", USER))
            if path.startswith("room_keys/keys/"):
                self.assertEqual(params["version"], "1")

    def test_sessions_already_backed_up_are_left_alone(self):
        self.homeserver.backed_up = {self.session.id}
        member = self._fill()
        self.assertEqual(self.homeserver.written, {})
        self.assertIsNone(member.history_due_at)

    def test_a_backup_the_recovery_key_contradicts_gets_nothing(self):
        self.homeserver.version_info = _version(
            public_key=vodozemac.PkDecryption().public_key.to_base64()
        )
        member = self._fill()
        self.assertEqual(self.homeserver.written, {})
        self.assertIsNone(member.history_due_at)
        self.assertEqual(member.history_backup_version, "1")

    def _without_recovery_key(self):
        models.MatrixUserProfile.objects.filter(pk=self.profile.pk).update(
            recovery_key=""
        )
        identity = Identity(USER)
        self.bot.query_keys.return_value = identity.response()
        return identity

    def _signed_by(self, signing_key):
        self.homeserver.version_info["auth_data"] = signing_key.sign(
            self.homeserver.version_info["auth_data"], USER
        )

    def test_without_a_key_waldur_holds_the_pinned_identity_decides(self):
        identity = self._without_recovery_key()
        member = self._fill()
        self.assertEqual(self.homeserver.written, {})
        self.assertIsNone(member.history_due_at)
        # The refused backup is recorded, so the version check won't ask again.
        self.assertEqual(member.history_backup_version, "1")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pinned_master_key, identity.master.public_key)

        history_backfill.request_for_user(self.user)
        self._signed_by(identity.master)
        self._fill()
        self.assertEqual(set(self.homeserver.written), {self.session.id})

    def test_a_master_key_other_than_the_pinned_one_gets_nothing(self):
        self._without_recovery_key()
        self._fill()  # pins the first identity seen
        impostor = Identity(USER)
        self.bot.query_keys.return_value = impostor.response()
        self._signed_by(impostor.master)
        history_backfill.request_for_user(self.user)
        member = self._fill()
        self.assertEqual(self.homeserver.written, {})
        self.assertIsNone(member.history_due_at)

    def test_with_an_escrowed_key_hidden_secret_storage_gets_nothing(self):
        # A homeserver hiding the secret can't make the bot fall back to
        # signatures, even its own master key's.
        self.homeserver.account_data = {}
        identity = Identity(USER)
        self.bot.query_keys.return_value = identity.response()
        self._signed_by(identity.master)
        member = self._fill()
        self.assertEqual(self.homeserver.written, {})
        self.assertIsNone(member.history_due_at)
        self.bot.query_keys.assert_not_called()
        # Not held against the backup: at escrow the drawer has not written
        # secret storage yet, and the same backup may check out later.
        self.assertEqual(member.history_backup_version, "")

    def test_an_unreadable_escrowed_key_or_account_data_gets_nothing(self):
        garbled = copy.deepcopy(BACKUP_SECRET)
        garbled["encrypted"]["KEYID"] = {"iv": 1, "ciphertext": None, "mac": []}
        cases = (
            ("garbled key", "not a recovery key", None, None),
            ("malformed secret", RECOVERY_KEY, {"encrypted": "nonsense"}, None),
            ("malformed entry", RECOVERY_KEY, garbled, None),
            ("malformed description", RECOVERY_KEY, None, {"iv": 5, "mac": {}}),
        )
        for name, recovery_key, secret, description in cases:
            with self.subTest(name):
                models.MatrixUserProfile.objects.filter(pk=self.profile.pk).update(
                    recovery_key=recovery_key
                )
                self.homeserver.account_data = {
                    "m.megolm_backup.v1": secret or BACKUP_SECRET,
                    "m.secret_storage.key.KEYID": description or KEY_INFO,
                }
                history_backfill.request_for_user(self.user)
                member = self._fill()
                self.assertEqual(self.homeserver.written, {})
                self.assertIsNone(member.history_due_at)
                self.bot.query_keys.assert_not_called()

    def test_without_a_profile_nothing_is_pinned_or_written(self):
        identity = Identity(USER)
        self.bot.query_keys.return_value = identity.response()
        self._signed_by(identity.master)
        self.profile.delete()
        member = self._fill()
        self.assertEqual(self.homeserver.written, {})
        self.assertIsNone(member.history_due_at)

    def test_only_sessions_of_devices_that_were_in_the_room_are_written(self):
        own = InboundGroupSession(
            OutboundGroupSession().session_key, "bot-ed25519", "bot-curve25519", ROOM
        )
        stranger = InboundGroupSession(
            OutboundGroupSession().session_key, "x-ed25519", "x-curve25519", ROOM
        )
        # Right curve key, wrong signing key: not the member's device either.
        forged = InboundGroupSession(
            OutboundGroupSession().session_key, "x-ed25519", "sender-curve25519", ROOM
        )
        for session in (own, stranger, forged):
            self.bot.client.olm.inbound_group_store.add(session)
        self._fill()
        self.assertEqual(set(self.homeserver.written), {self.session.id, own.id})

    def test_a_room_the_bot_is_not_in_is_retried(self):
        self.homeserver.membership_status = 403
        member = self._fill()
        self.assertEqual(self.homeserver.written, {})
        self.assertEqual(member.history_attempts, 1)
        self.assertGreater(member.history_due_at, timezone.now())

    def test_a_backup_replaced_during_the_write_is_retried_not_filled(self):
        self.homeserver.wrong_version = True
        member = self._fill()
        self.assertEqual(member.history_backup_version, "")
        self.assertEqual(member.history_attempts, 1)
        self.assertGreater(member.history_due_at, timezone.now())

    def test_staff_whose_join_no_longer_counts_get_nothing(self):
        staff = UserFactory(is_staff=True)
        models.MatrixUserProfile.objects.create(
            user=staff, matrix_user_id="@staff:matrix.example.com"
        )
        tasks._record_membership(
            self.room,
            staff,
            {
                "matrix_user_id": "@staff:matrix.example.com",
                "membership_state": models.MembershipStates.JOINED,
                "manually_joined": True,
            },
        )
        models.MatrixRoomMember.objects.filter(user=self.user).update(
            history_due_at=None
        )
        staff.is_staff = False
        staff.save(update_fields=["is_staff"])
        self._fill()
        member = models.MatrixRoomMember.objects.get(room=self.room, user=staff)
        self.assertEqual(self.homeserver.written, {})
        self.assertIsNone(member.history_due_at)

    def test_no_backup_drops_the_fill(self):
        self.homeserver.version_info = None
        member = self._fill()
        self.assertIsNone(member.history_due_at)
        self.assertEqual(self.homeserver.written, {})

    def test_a_member_removed_before_the_write_gets_nothing(self):
        self.fixture.project.remove_user(self.user)
        member = self._fill()
        self.assertEqual(self.homeserver.written, {})
        self.assertIsNone(member.history_due_at)

    def test_a_join_the_homeserver_has_not_seen_is_retried(self):
        self.homeserver.joined = False
        member = self._fill()
        self.assertEqual(self.homeserver.written, {})
        self.assertGreater(member.history_due_at, timezone.now())
        self.assertEqual(member.history_attempts, 1)

    def test_a_failing_homeserver_is_retried(self):
        async def broken(*args, **kwargs):
            return 502, {}

        with mock.patch.object(self.filler, "_request", side_effect=broken):
            async_to_sync(self.filler.fill_due)()
        member = self._member()
        self.assertEqual(member.history_attempts, 1)
        self.assertGreater(member.history_due_at, timezone.now())

    def _check(self):
        with mock.patch.object(
            self.filler, "_request", side_effect=self.homeserver.request
        ):
            async_to_sync(self.filler.check_versions)()

    def test_the_version_check_asks_for_a_fill_into_a_new_backup(self):
        self._fill()
        self._check()
        self.assertIsNone(self._member().history_due_at)
        self.homeserver.version_info = _version(version="2")
        self._check()  # ends the pass
        self._check()  # the next pass waits for its time
        self.assertIsNone(self._member().history_due_at)
        self.filler._check_started_at -= history_filler.VERSION_CHECK_SECONDS
        self._check()
        self.assertIsNotNone(self._member().history_due_at)

    def test_the_version_check_walks_members_a_batch_per_round(self):
        other = UserFactory()
        self.fixture.project.add_user(other, ProjectRole.MEMBER)
        tasks._record_membership(
            self.room,
            other,
            {
                "matrix_user_id": "@other:matrix.example.com",
                "membership_state": models.MembershipStates.JOINED,
            },
        )
        with mock.patch.object(history_filler, "VERSION_CHECK_BATCH", 1):
            self._check()
            self.assertEqual(len(self.homeserver.calls), 1)
            self._check()
            self.assertEqual(len(self.homeserver.calls), 2)

    def test_an_unexpected_failure_is_retried_without_stopping_the_round(self):
        with mock.patch.object(
            history_filler.key_backup, "session_backup", side_effect=ValueError
        ):
            member = self._fill()
        self.assertEqual(member.history_attempts, 1)
        self.assertGreater(member.history_due_at, timezone.now())
