import contextlib
import secrets
import tempfile
import uuid
from unittest import mock

from django.db import connection, connections
from django.test import SimpleTestCase, TestCase
from nio.crypto import (
    InboundGroupSession,
    OlmAccount,
    OlmDevice,
    OutboundGroupSession,
)
from nio.crypto.key_request import OutgoingKeyRequest
from nio.crypto.sessions import OutboundSession
from nio.store import SqliteStore

from waldur_mastermind.matrix_chat.crypto import store as crypto_store

USER, DEVICE, ROOM = "@bot:test", "BOTDEVICE", "!room:test"


@contextlib.contextmanager
def nio_models_unqualified():
    """Point nio's global models back at no schema when the test is done."""
    try:
        yield
    finally:
        for model in crypto_store.PostgresStore.models:
            model._meta.schema = None


class PostgresStoreTestCase(TestCase):
    def setUp(self):
        self.schema = f"matrix_bot_test_{uuid.uuid4().hex[:12]}"
        self.store_class = type(
            "TestStore", (crypto_store.PostgresStore,), {"schema_name": self.schema}
        )
        self.pickle_key = secrets.token_urlsafe(32)
        self._stores = []
        self.addCleanup(self._drop_schema)
        self.enterContext(nio_models_unqualified())

    def _drop_schema(self):
        for store in self._stores:
            store.database.close()
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def open_store(self, pickle_key=None):
        store = self.store_class(
            USER, DEVICE, "", pickle_key=pickle_key or self.pickle_key
        )
        self._stores.append(store)
        return store

    def sql(self, query, params=()):
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()


def _exercise(open_store):
    """Drive every store operation, reopen, and report what reads back."""
    account = OlmAccount()
    peer = OlmAccount()
    peer.generate_one_time_keys(1)
    one_time_key = next(iter(peer.one_time_keys["curve25519"].values()))
    peer_key = peer.identity_keys["curve25519"]
    olm_session = OutboundSession(account, peer_key, one_time_key)

    outbound = OutboundGroupSession()
    session_key = outbound.session_key
    outbound.mark_as_shared()
    ciphertext = outbound.encrypt("top secret")
    inbound = InboundGroupSession(
        session_key,
        account.identity_keys["ed25519"],
        account.identity_keys["curve25519"],
        ROOM,
        forwarding_chain=["chain-1", "chain-2"],
    )
    devices = [
        OlmDevice(
            f"@user{i}:test",
            f"DEVICE{i}",
            {"ed25519": f"ed{i}", "curve25519": f"curve{i}"},
        )
        for i in range(5)
    ]
    key_request = OutgoingKeyRequest(
        "request-1", inbound.id, ROOM, "m.megolm.v1.aes-sha2"
    )

    store = open_store()
    # Each save twice: the second write goes through the REPLACE path.
    for _ in range(2):
        store.save_account(account)
        store.save_session(peer_key, olm_session)
        store.save_inbound_group_session(inbound)
        store.save_device_keys({d.user_id: {d.id: d} for d in devices})
        store.add_outgoing_key_request(key_request)
    store.verify_device(devices[0])
    store.blacklist_device(devices[1])
    store.ignore_device(devices[2])
    store.verify_device(devices[3])
    store.unverify_device(devices[3])
    store.ignore_devices(devices[3:])
    store.save_encrypted_rooms([ROOM, "!other:test"])
    store.delete_encrypted_room("!other:test")
    store.save_sync_token("s1")
    store.save_sync_token("s2")

    store = open_store()  # as after a restart
    group_session = store.load_inbound_group_sessions().get(
        ROOM, account.identity_keys["curve25519"], inbound.id
    )
    device_store = store.load_device_keys()

    def trust(device):
        for state in ("verified", "blacklisted", "ignored"):
            if getattr(store, f"is_device_{state}")(device):
                return state
        return "unset"

    return {
        "account reads back": store.load_account().identity_keys
        == account.identity_keys,
        "olm sessions for the peer": len(store.load_sessions()[peer_key]),
        "megolm decrypts": group_session.decrypt(ciphertext)[0],
        "forwarding chain": sorted(group_session.forwarding_chain),
        "devices": sorted(
            (d.user_id, d.id)
            for user in device_store.users
            for d in device_store.active_user_devices(user)
        ),
        "device keys": sorted(
            (d.id, d.ed25519, d.curve25519)
            for user in device_store.users
            for d in device_store.active_user_devices(user)
        ),
        "trust": {device.id: trust(device) for device in devices},
        "key requests": sorted(store.load_outgoing_key_requests()),
        "encrypted rooms": sorted(store.load_encrypted_rooms()),
        "sync token": store.load_sync_token(),
    }


class ParityTest(PostgresStoreTestCase):
    maxDiff = None

    def test_postgres_store_reads_back_what_sqlite_does(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = _exercise(
                lambda: SqliteStore(USER, DEVICE, directory, pickle_key=self.pickle_key)
            )
        actual = _exercise(self.open_store)

        self.assertEqual(actual, expected)
        self.assertEqual(actual["megolm decrypts"], "top secret")
        self.assertEqual(actual["sync token"], "s2")
        self.assertEqual(
            actual["trust"],
            {
                "DEVICE0": "verified",
                "DEVICE1": "blacklisted",
                "DEVICE2": "ignored",
                "DEVICE3": "ignored",
                "DEVICE4": "ignored",
            },
        )


class PostgresStoreTest(PostgresStoreTestCase):
    def test_tables_live_in_their_own_schema(self):
        self.open_store()
        tables = {
            row[0]
            for row in self.sql(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                [self.schema],
            )
        }
        self.assertTrue({"accounts", "megolminboundsessions", "devicekeys"} <= tables)
        self.assertFalse(
            self.sql(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'accounts'"
            )
        )

    def test_a_second_save_updates_the_row_in_place(self):
        store = self.open_store()
        store.save_account(OlmAccount())
        store.save_sync_token("s1")
        store.save_sync_token("s2")
        self.assertEqual(
            self.sql(f'SELECT token FROM "{self.schema}".synctokens'), [("s2",)]
        )

    def test_secrets_are_stored_pickled(self):
        store = self.open_store()
        account = OlmAccount()
        store.save_account(account)
        (stored,) = self.sql(f'SELECT account FROM "{self.schema}".accounts')[0]
        self.assertNotIn(account.identity_keys["ed25519"].encode(), bytes(stored))

    def test_a_wrong_pickle_key_cannot_load_the_account(self):
        self.open_store().save_account(OlmAccount())
        store = self.open_store(pickle_key=secrets.token_urlsafe(32))
        with self.assertRaises(Exception):
            store.load_account()

    def test_a_newer_store_version_is_refused(self):
        store = self.open_store()
        # Through the store's own connection: Django's test transaction would hide it.
        store.database.execute_sql(
            f'UPDATE "{self.schema}".storeversion SET version = 99'
        )
        with self.assertRaises(crypto_store.StoreVersionMismatch):
            self.open_store()

    def test_the_current_store_version_opens(self):
        self.open_store()
        self.open_store()
        self.assertEqual(
            self.sql(f'SELECT version FROM "{self.schema}".storeversion'),
            [(SqliteStore.store_version,)],
        )


class DatabaseSettingsTest(SimpleTestCase):
    def test_takes_djangos_connection_and_drops_django_only_options(self):
        settings = {
            "NAME": "waldur",
            "HOST": "db.example",
            "PORT": "5432",
            "USER": "waldur",
            "PASSWORD": "secret",
            "OPTIONS": {
                "prepare_threshold": None,
                "sslmode": "require",
                "isolation_level": 2,
                "server_side_binding": True,
            },
        }
        with mock.patch.object(connections["default"], "settings_dict", settings):
            name, kwargs = crypto_store.database_settings()
        self.assertEqual(name, "waldur")
        self.assertEqual(
            kwargs,
            {
                "prepare_threshold": None,
                "sslmode": "require",
                "host": "db.example",
                "port": "5432",
                "user": "waldur",
                "password": "secret",
            },
        )
