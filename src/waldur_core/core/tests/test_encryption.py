from cryptography.fernet import Fernet, InvalidToken
from django.test import TestCase
from django.test.utils import override_settings

from waldur_core.core import encryption

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()


@override_settings(FIELD_ENCRYPTION_KEY=KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[])
class EncryptionTest(TestCase):
    def test_round_trip(self):
        self.assertEqual(
            encryption.decrypt_value(encryption.encrypt_value("sk-abc")), "sk-abc"
        )

    def test_ciphertext_is_opaque_fernet_token(self):
        token = encryption.encrypt_value("sk-abc")
        self.assertNotEqual(token, "sk-abc")
        self.assertTrue(token.startswith("gAAAA"))

    def test_is_encrypted_detection(self):
        self.assertTrue(encryption.is_encrypted(encryption.encrypt_value("x")))
        self.assertFalse(encryption.is_encrypted("sk-plaintext"))
        self.assertFalse(encryption.is_encrypted(""))

    def test_key_rotation_via_fallbacks(self):
        # A value encrypted under a previous primary key must stay readable
        # once that key is demoted to FIELD_ENCRYPTION_KEY_FALLBACKS.
        with override_settings(FIELD_ENCRYPTION_KEY=OTHER_KEY):
            token = encryption.encrypt_value("rotate-me")
        with override_settings(
            FIELD_ENCRYPTION_KEY=KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OTHER_KEY]
        ):
            self.assertEqual(encryption.decrypt_value(token), "rotate-me")

    def test_missing_key_falls_back_to_secret_key(self):
        # No dedicated key configured, but encryption must not hard-fail: it
        # derives a key from SECRET_KEY so the round-trip still works.
        with override_settings(
            FIELD_ENCRYPTION_KEY="", FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            token = encryption.encrypt_value("sk-fallback")
            self.assertTrue(token.startswith("gAAAA"))
            self.assertEqual(encryption.decrypt_value(token), "sk-fallback")

    def test_fallback_key_is_secret_key_derived_and_stable(self):
        # The fallback derives deterministically from SECRET_KEY, so a different
        # SECRET_KEY yields an unreadable token (keys are actually distinct).
        with override_settings(
            FIELD_ENCRYPTION_KEY="",
            FIELD_ENCRYPTION_KEY_FALLBACKS=[],
            SECRET_KEY="secret-one",
        ):
            token = encryption.encrypt_value("payload")
        with override_settings(
            FIELD_ENCRYPTION_KEY="",
            FIELD_ENCRYPTION_KEY_FALLBACKS=[],
            SECRET_KEY="secret-two",
        ):
            with self.assertRaises(InvalidToken):
                encryption.decrypt_value(token)

    def test_garbage_ciphertext_raises(self):
        with self.assertRaises(InvalidToken):
            encryption.decrypt_value("gAAAAnot-a-real-token")

    def test_rows_survive_introducing_a_dedicated_key(self):
        # A deployment may start on the SECRET_KEY-derived key and only later set
        # a dedicated FIELD_ENCRYPTION_KEY (as the warning tells operators to).
        # The derived key stays an implicit last-resort fallback, so rows written
        # before the switch must remain readable without any manual fallback
        # configuration.
        with override_settings(
            FIELD_ENCRYPTION_KEY="", FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            token = encryption.encrypt_value("pre-dedicated-key")
        with override_settings(
            FIELD_ENCRYPTION_KEY=KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            self.assertEqual(encryption.decrypt_value(token), "pre-dedicated-key")


@override_settings(FIELD_ENCRYPTION_KEY=KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[])
class RotateValueTest(TestCase):
    def test_rotation_moves_a_token_onto_the_new_primary(self):
        """This is what lets an old key be retired.

        A token written under the old key must survive being re-encrypted while both
        keys are configured, and then still decrypt once the old key is gone.
        """
        token = encryption.encrypt_value("sk-abc")

        with override_settings(
            FIELD_ENCRYPTION_KEY=OTHER_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[KEY]
        ):
            rotated = encryption.rotate_value(token)

        with override_settings(
            FIELD_ENCRYPTION_KEY=OTHER_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            self.assertEqual(encryption.decrypt_value(rotated), "sk-abc")
            # The original token is exactly what would now fail a reveal.
            with self.assertRaises(InvalidToken):
                encryption.decrypt_value(token)

    def test_rotation_rejects_a_token_no_configured_key_can_read(self):
        with override_settings(
            FIELD_ENCRYPTION_KEY=OTHER_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            foreign = encryption.encrypt_value("sk-abc")

        with self.assertRaises(InvalidToken):
            encryption.rotate_value(foreign)
