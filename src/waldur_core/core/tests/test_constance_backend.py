import importlib
from io import StringIO

from constance import config
from constance.codecs import dumps, loads
from constance.models import Constance
from cryptography.fernet import Fernet
from django.apps import apps
from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.test.utils import override_settings

from waldur_core.core import constance_backend, encryption, views
from waldur_mastermind.matrix_chat import views as matrix_views

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()
LOST_KEY = Fernet.generate_key().decode()

SECRET = "MATRIX_APPSERVICE_AS_TOKEN"
PLAIN = "SITE_NAME"

migration = importlib.import_module(
    "waldur_core.core.migrations.0053_encrypt_constance_secrets"
)


def stored(key):
    return loads(Constance.objects.get(key=key).value)


def store_raw(key, value):
    Constance.objects.update_or_create(key=key, defaults={"value": dumps(value)})


class SecretKeysTest(TestCase):
    def test_secret_keys_are_the_secret_fields(self):
        keys = constance_backend.secret_keys()
        self.assertIn(SECRET, keys)
        self.assertIn("MATRIX_APPSERVICE_HS_TOKEN", keys)
        self.assertNotIn(PLAIN, keys)


class EncryptedDatabaseBackendTest(TestCase):
    def test_secret_is_stored_encrypted_and_read_back_in_clear(self):
        config.MATRIX_APPSERVICE_AS_TOKEN = "as-secret"

        self.assertTrue(encryption.is_encrypted(stored(SECRET)))
        self.assertNotIn("as-secret", Constance.objects.get(key=SECRET).value)
        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "as-secret")

    def test_unset_secret_reads_as_its_default(self):
        # Several checks treat an empty secret as "not configured, skip".
        self.assertFalse(Constance.objects.filter(key=SECRET).exists())
        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "")

    def test_non_secret_is_stored_in_clear(self):
        config.SITE_NAME = "Portal"

        self.assertEqual(stored(PLAIN), "Portal")
        self.assertEqual(config.SITE_NAME, "Portal")

    def test_empty_secret_stays_empty(self):
        config.MATRIX_APPSERVICE_AS_TOKEN = ""

        self.assertEqual(stored(SECRET), "")
        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "")

    def test_token_shaped_value_is_wrapped_not_trusted(self):
        """A caller can't plant a ciphertext and have it decrypted on read."""
        token = encryption.encrypt_value("someone else's secret")
        config.MATRIX_APPSERVICE_AS_TOKEN = token

        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, token)

    def test_settings_api_values_are_decrypted(self):
        config.MATRIX_APPSERVICE_AS_TOKEN = "as-secret"

        self.assertEqual(views._safe_get_constance_values()[SECRET], "as-secret")

    def test_plaintext_left_by_an_older_release_is_still_readable(self):
        store_raw(SECRET, "legacy-plain")

        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "legacy-plain")

    def _lose_the_key_of(self, setting, value):
        with override_settings(
            FIELD_ENCRYPTION_KEY=LOST_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            setattr(config, setting, value)
        return stored(setting)

    def test_undecryptable_secret_does_not_break_other_settings(self):
        self._lose_the_key_of(SECRET, "gone")
        config.SITE_NAME = "Portal"

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            self.assertEqual(config.SITE_NAME, "Portal")
            self.assertEqual(
                config.MATRIX_APPSERVICE_AS_TOKEN,
                constance_backend.undecryptable_value(SECRET),
            )

    def test_undecryptable_secret_never_reads_as_its_ciphertext(self):
        # A database dump holds the ciphertext; it must not work as the secret.
        ciphertext = self._lose_the_key_of("MATRIX_APPSERVICE_HS_TOKEN", "hs-secret")

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            hs_token = config.MATRIX_APPSERVICE_HS_TOKEN
        self.assertNotEqual(hs_token, ciphertext)
        # Not empty either: some checks treat an empty secret as "no check".
        self.assertTrue(hs_token)

    def test_each_undecryptable_secret_reads_differently(self):
        # Outbound secrets reach third parties; their stand-in must not pass a
        # check made against another secret.
        self._lose_the_key_of(SECRET, "as-secret")
        self._lose_the_key_of("MATRIX_APPSERVICE_HS_TOKEN", "hs-secret")

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            as_token = config.MATRIX_APPSERVICE_AS_TOKEN
            hs_token = config.MATRIX_APPSERVICE_HS_TOKEN
            request = RequestFactory().put("/", HTTP_AUTHORIZATION=f"Bearer {as_token}")
            self.assertNotEqual(as_token, hs_token)
            self.assertFalse(matrix_views._is_from_homeserver(request))

    def test_writing_the_stand_in_back_keeps_the_stored_secret(self):
        before = self._lose_the_key_of(SECRET, "as-secret")

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            config.MATRIX_APPSERVICE_AS_TOKEN = config.MATRIX_APPSERVICE_AS_TOKEN

        self.assertEqual(stored(SECRET), before)

    def test_homeserver_check_rejects_the_ciphertext(self):
        ciphertext = self._lose_the_key_of("MATRIX_APPSERVICE_HS_TOKEN", "hs-secret")
        request = RequestFactory().put("/", HTTP_AUTHORIZATION=f"Bearer {ciphertext}")

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            self.assertFalse(matrix_views._is_from_homeserver(request))


class EncryptConstanceSecretsMigrationTest(TestCase):
    def test_encrypts_plaintext_secrets_once(self):
        store_raw(SECRET, "legacy-plain")
        store_raw(PLAIN, "Portal")

        migration.encrypt_secrets(apps, None)
        first = stored(SECRET)
        migration.encrypt_secrets(apps, None)

        self.assertTrue(encryption.is_encrypted(first))
        self.assertEqual(stored(SECRET), first)
        self.assertEqual(stored(PLAIN), "Portal")
        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "legacy-plain")

    def test_reverse_keeps_a_secret_it_cannot_decrypt(self):
        with override_settings(
            FIELD_ENCRYPTION_KEY=LOST_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            config.MATRIX_APPSERVICE_AS_TOKEN = "gone"
        before = stored(SECRET)

        migration.decrypt_secrets(apps, None)

        self.assertEqual(stored(SECRET), before)

    def test_reverse_restores_plaintext(self):
        config.MATRIX_APPSERVICE_AS_TOKEN = "as-secret"

        migration.decrypt_secrets(apps, None)

        self.assertEqual(stored(SECRET), "as-secret")


class ReencryptConstanceSecretsTest(TestCase):
    def test_secrets_rotate_to_the_new_key(self):
        with override_settings(
            FIELD_ENCRYPTION_KEY=OLD_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            config.MATRIX_APPSERVICE_AS_TOKEN = "as-secret"

        out = StringIO()
        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]
        ):
            call_command("reencrypt_fields", stdout=out)

        self.assertIn("re-encrypted 1 row(s)", out.getvalue())
        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "as-secret")

    def test_plaintext_and_undecryptable_secrets_are_reported(self):
        store_raw(SECRET, "legacy-plain")
        with override_settings(
            FIELD_ENCRYPTION_KEY=LOST_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            config.MATRIX_APPSERVICE_HS_TOKEN = "gone"
        before = stored("MATRIX_APPSERVICE_HS_TOKEN")

        out = StringIO()
        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            call_command("reencrypt_fields", stdout=out)

        self.assertIn(f"{SECRET} is not encrypted", out.getvalue())
        self.assertIn("1 row(s) cannot be decrypted", out.getvalue())
        self.assertEqual(stored(SECRET), "legacy-plain")
        self.assertEqual(stored("MATRIX_APPSERVICE_HS_TOKEN"), before)

    def test_plaintext_secrets_are_encrypted_on_request(self):
        store_raw(SECRET, "saved-by-an-old-pod")

        out = StringIO()
        call_command("reencrypt_fields", "--encrypt-plaintext-settings", stdout=out)

        self.assertTrue(encryption.is_encrypted(stored(SECRET)))
        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "saved-by-an-old-pod")
