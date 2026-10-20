import importlib
from types import SimpleNamespace

from cryptography.fernet import Fernet
from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.test.utils import override_settings

from waldur_core.configuration import secrets
from waldur_core.configuration.migration_utils import (
    decode_constance,
    encode_constance,
)
from waldur_core.configuration.tests.utils import ConstanceTableMixin
from waldur_core.core import encryption

LOST_KEY = Fernet.generate_key().decode()

SECRET = "MATRIX_APPSERVICE_AS_TOKEN"
PLAIN = "SITE_NAME"

migration = importlib.import_module(
    "waldur_core.core.migrations.0053_encrypt_constance_secrets"
)
schema_editor = SimpleNamespace(connection=connection)


def stored(key):
    with connection.cursor() as cursor:
        cursor.execute("SELECT value FROM constance_constance WHERE key = %s", [key])
        return decode_constance(cursor.fetchone()[0])


def store_raw(key, value):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM constance_constance WHERE key = %s", [key])
        cursor.execute(
            "INSERT INTO constance_constance (key, value) VALUES (%s, %s)",
            [key, encode_constance(value)],
        )


class EncryptConstanceSecretsMigrationTest(ConstanceTableMixin, TestCase):
    def test_encrypts_plaintext_secrets_once(self):
        store_raw(SECRET, "legacy-plain")
        store_raw(PLAIN, "Portal")

        migration.encrypt_secrets(apps, schema_editor)
        first = stored(SECRET)
        migration.encrypt_secrets(apps, schema_editor)

        self.assertTrue(encryption.is_encrypted(first))
        self.assertEqual(stored(SECRET), first)
        self.assertEqual(stored(PLAIN), "Portal")
        self.assertEqual(secrets.decrypt_stored(first), "legacy-plain")

    def test_reverse_keeps_a_secret_it_cannot_decrypt(self):
        with override_settings(
            FIELD_ENCRYPTION_KEY=LOST_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            store_raw(SECRET, encryption.encrypt_value("gone"))
        before = stored(SECRET)

        migration.decrypt_secrets(apps, schema_editor)

        self.assertEqual(stored(SECRET), before)

    def test_reverse_restores_plaintext(self):
        store_raw(SECRET, encryption.encrypt_value("as-secret"))

        migration.decrypt_secrets(apps, schema_editor)

        self.assertEqual(stored(SECRET), "as-secret")
